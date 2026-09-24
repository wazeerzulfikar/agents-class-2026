"""Model boundary for the newsletter: editorial, highlight copy, and image choice.

Platform code decides who is featured (see `score.py`), the order, every link, the quote,
and the footer. The model writes the prose from the per-project read the scoring pass
produced, scores projects one at a time, and picks among measured image captures.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx
from openai import OpenAI, OpenAIError
from pydantic import SecretStr, ValidationError

from .lecture import LectureNotes
from .models import (
    Highlight,
    NewsletterBranding,
    NewsletterCopy,
    ProjectEvidence,
    ProjectScore,
    WeeklyDigest,
)

EDITORIAL_MARKER = "You are the editor of"
HIGHLIGHTS_MARKER = "You write the highlights of"
EDITORIAL_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": "A punny headline about this week's assignment, at most 12 words.",
        },
        "editorial": {
            "type": "string",
            "description": (
                "Up to 150 words on how the class did against what was taught, in two short "
                "paragraphs. Reference links, if any, use Markdown [text](https://url) syntax."
            ),
        },
        "quote_choice": {
            "type": "integer",
            "description": (
                "1-based index of the most inspiring candidate quote from a student post, "
                "or 0 if none is good enough to close the issue."
            ),
        },
    },
    "required": ["headline", "editorial", "quote_choice"],
    "additionalProperties": False,
}
HIGHLIGHTS_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "headline": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["project_id", "headline", "description"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["highlights"],
    "additionalProperties": False,
}
_LINK_LIKE = re.compile(r"https?://|www\.|@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", re.IGNORECASE)
MARKDOWN_LINK = re.compile(r"\[([^\]\n]{1,120})\]\((https://[^\s)]+)\)")
_PARTICIPATION_COUNT = re.compile(
    r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
    r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|dozen|dozens|half|most|"
    r"majority)\s+(of\s+you|students?|submissions?|projects?|people|builds?|posts?|"
    r"of\s+the\s+class)\b",
    re.IGNORECASE,
)
MAX_EDITORIAL_LINKS = 2
# Staff vocabulary that must not leak into student-facing copy. Kept narrow: words like
# "score" are legitimate when describing a build that scores things.
_BANNED_WORDS = re.compile(r"\b(brief|rubric)\b", re.IGNORECASE)
# Brevity is part of the format; the model is re-prompted with the exact overrun.
WORD_LIMITS: dict[str, int] = {"headline": 12, "description": 48}
EDITORIAL_WORDS = (100, 165)


class LinkChecker(Protocol):
    """Confirms a reference URL actually resolves before it goes into the issue."""

    def __call__(self, url: str) -> bool: ...


def link_resolves(url: str, *, timeout_seconds: float = 10.0) -> bool:
    try:
        with httpx.Client(
            follow_redirects=True, max_redirects=5, timeout=timeout_seconds
        ) as client:
            response = client.head(url)
            if response.status_code in {403, 405}:
                response = client.get(url)
            return response.status_code < 400
    except httpx.HTTPError:
        return False


@dataclass(frozen=True)
class EditorialDraft:
    headline: str
    editorial: str
    quote_choice: int


_CHOICE = re.compile(r'"choice"\s*:\s*(\d+)')


class NewsletterCompositionError(RuntimeError):
    """The model could not produce copy that satisfies the platform's rules."""


class NewsletterWriter(Protocol):
    """Text and image-choice calls; the platform parses and validates every response."""

    def write(self, *, system_prompt: str, user_prompt: str, schema: dict[str, object]) -> str: ...

    def judge_images(self, *, prompt: str, images: Sequence[bytes]) -> int: ...


@dataclass
class ModelUsage:
    """Token totals across one command, for the instructor's cost awareness."""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, *, input_tokens: int | None, output_tokens: int | None) -> None:
        self.requests += 1
        self.input_tokens += input_tokens or 0
        self.output_tokens += output_tokens or 0


class OpenAINewsletterWriter:
    """OpenAI writer that keeps the API key inside this process."""

    def __init__(
        self,
        *,
        model_id: str,
        api_key: SecretStr,
        api_base: str = "https://api.openai.com/v1",
    ) -> None:
        self.model_id = model_id
        self.usage = ModelUsage()
        self._client = OpenAI(api_key=api_key.get_secret_value(), base_url=api_base)

    def write(self, *, system_prompt: str, user_prompt: str, schema: dict[str, object]) -> str:
        try:
            completion = self._client.chat.completions.create(
                model=self.model_id,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "newsletter_copy", "strict": True, "schema": schema},
                },
            )
        except OpenAIError as error:
            raise NewsletterCompositionError(
                f"The model request failed ({type(error).__name__})."
            ) from error
        if completion.usage is not None:
            self.usage.add(
                input_tokens=completion.usage.prompt_tokens,
                output_tokens=completion.usage.completion_tokens,
            )
        content = completion.choices[0].message.content if completion.choices else None
        if not content:
            raise NewsletterCompositionError("The model returned no newsletter copy.")
        return content

    def judge_images(self, *, prompt: str, images: Sequence[bytes]) -> int:
        content: list[dict[str, object]] = [{"type": "input_text", "text": prompt}]
        for index, png in enumerate(images, start=1):
            encoded = base64.b64encode(png).decode("ascii")
            content.append({"type": "input_text", "text": f"Image {index}:"})
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{encoded}",
                    "detail": "low",
                }
            )
        try:
            response = self._client.responses.create(
                model=self.model_id,
                input=cast(Any, [{"role": "user", "content": content}]),
                store=False,
            )
        except OpenAIError as error:
            raise NewsletterCompositionError(
                f"The image choice request failed ({type(error).__name__})."
            ) from error
        if response.usage is not None:
            self.usage.add(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        match = _CHOICE.search(response.output_text)
        if match is None:
            raise NewsletterCompositionError("The model returned no image choice.")
        return int(match.group(1))


def _voice(branding: NewsletterBranding) -> str:
    return (
        f'"{branding.newsletter_name}", the weekly newsletter of {branding.course_code} '
        f"{branding.course_title} at {branding.institution}, {branding.course_term}. Readers are "
        "the students and staff of the class, who skim it on their phones.\n\n"
        "Voice: warm, witty, and quick. Use clever puns and playful phrasing, but keep every "
        "factual claim grounded in the notes you are given. Never invent features, results, or "
        "names. Refer to each student by the label provided. Call the week's task \"the "
        'assignment"; never say "brief" or "rubric".\n'
        "Write plain text only: no Markdown, no bullet characters, no URLs, no email addresses, "
        "no emoji. Respond with JSON that matches the provided schema and nothing else."
    )


def build_editorial_system_prompt(branding: NewsletterBranding) -> str:
    return (
        f"{EDITORIAL_MARKER} {_voice(branding)}\n\n"
        "Task: from the staff notes on every student's submission, write the issue's headline "
        "and editorial, and pick the closing quote.\n"
        "- headline: at most 12 words, a pun or playful turn on this week's assignment itself "
        "(what the class was asked to build), not a generic line about highlights.\n"
        "- editorial: at most 150 words (count them; between 110 and 150), two short "
        "paragraphs, speaking "
        "to the class directly. Read the lecture slides for the week the assignment was given "
        "and judge the submissions against them. First: what generally went well, and which "
        "ideas from the lecture the class clearly absorbed. Second: the blind spots, meaning "
        "ideas the lecture emphasized that the submissions largely missed, skipped, or "
        "misapplied, plus the common blockers, all framed constructively in terms of the "
        "learning goals: what the gap teaches and what to practice next, not a complaint. Have "
        "fun with it and be concrete: point at actual builds by what they are (a rolling-ball "
        "physics world, a Downloads-folder renamer, a town of pixel townspeople), the odd "
        "failure modes that showed up, and the lecture's own phrases, so it reads like a note "
        "from someone who looked at everything. Whenever you refer to a specific build, wrap "
        "that phrase in a Markdown link to that submission's site URL from the notes, for "
        "example [a rolling-ball physics world](https://...), so readers can jump to it. "
        "Readability matters: short sentences of at most 20 words, one idea each, and never "
        "more than three items in a comma-separated run. Never name a student. Never state how "
        "many people submitted, posted, or struggled; no counts or proportions of the class at "
        "all. "
        "Do not single out the featured projects as such; the highlights and the full list "
        "follow separately. Do not reuse wording from the candidate closing quotes; the chosen "
        "quote closes the issue on its own. When a specific reference genuinely helps (a paper, "
        "documentation, or tutorial you are certain exists), add at most two links using "
        "Markdown [text](https://url) syntax; otherwise add none. Never invent a URL.\n"
        "- quote_choice: from the candidate quotes taken from students' own posts, choose the "
        "one with the most personality as a closing line: surprising, funny, candid, or vivid, "
        "complete enough to make sense to someone who has not read the post, and clearly about "
        "agents, AI, or human cognition and how agents augment it (the course's theme). A witty "
        "line about an unrelated subject loses to a plainer line about agents. Reject "
        "definitions, restatements of the assignment, fragments that depend on missing context, "
        "and anything a textbook could have said; answer 0 rather than pick a bland or "
        "off-topic one."
    )


def build_highlights_system_prompt(branding: NewsletterBranding) -> str:
    return (
        f"{HIGHLIGHTS_MARKER} {_voice(branding)}\n\n"
        "Rules:\n"
        "- Course staff already chose the featured builds and their order. Write one highlight "
        "per listed project, in the given order, using the given project ids.\n"
        "- Each highlight has a pun-friendly headline of at most six words and a description "
        "of exactly two sentences (at most 40 words in total). The first sentence says what "
        "the build is and what makes it interesting. The second sentence says specifically how "
        "this particular build does what the assignment asked: name its concrete mechanism "
        "(for example what it observes, which actions it chooses between, how it decides to "
        "stop, what it verified), so that the sentence could only be about this build. Never "
        "restate the assignment's wording and never reuse a sentence across highlights. An "
        "image of the build appears above the text, so do not describe what it looks like."
    )


def describe_project(project: ProjectEvidence, *, status: str) -> str:
    lines = [f"### {project.project_id} (label: {project.label}) — {status}"]
    lines.append(f"deployed site: {project.site_url or 'none'}")
    lines.append(
        f"activity: {project.week_file_count} files in this week's build folder, "
        f"{project.commit_count} commits in the window, {project.site_file_count} website files"
    )
    if project.commits:
        lines.append("commit subjects:")
        lines.extend(
            f"- {commit.committed_at.date().isoformat()} {commit.subject}"
            for commit in project.commits
        )
    for document in project.documents:
        suffix = " (truncated)" if document.truncated else ""
        lines.append(f"document {document.path}{suffix}:")
        lines.append(document.text)
    if project.site_text:
        lines.append("deployed site text:")
        lines.append(project.site_text)
    if project.notes:
        lines.append("collection notes: " + "; ".join(project.notes))
    return "\n".join(lines)


def _week_header(digest: WeeklyDigest) -> list[str]:
    week = digest.week
    return [
        f"# Week {week.number} (class on {week.class_date.isoformat()})",
        f"Lecture topic: {week.topic}",
        f"This week's assignment: {week.tutorial}",
        f"Build window: {week.starts_at.date().isoformat()} to {week.ends_at.date().isoformat()}",
    ]


def quote_candidates(
    digest: WeeklyDigest, scores: Sequence[ProjectScore]
) -> tuple[tuple[str, str, str], ...]:
    """(project_id, label, verified quote) for every line a student's post offered."""

    labels = {project.project_id: project.label for project in digest.projects}
    return tuple(
        (score.project_id, labels.get(score.project_id, score.project_id), quote)
        for score in scores
        for quote in score.quotes
    )


def build_editorial_user_prompt(
    digest: WeeklyDigest,
    scores: Sequence[ProjectScore],
    *,
    lecture: LectureNotes | None = None,
    feedback: tuple[str, ...] = (),
) -> str:
    """Notes are anonymized and uncounted so the editorial cannot name or tally students."""

    scored = {score.project_id: score for score in scores}
    active = [project for project in digest.projects if project.active]
    sections = _week_header(digest)
    if lecture is not None:
        suffix = " (excerpt)" if lecture.truncated else ""
        sections.append(
            f"## What was taught this week: {lecture.title}{suffix}\n{lecture.slides_text}"
        )
        if lecture.learning_goals:
            sections.append(f"## Course learning goals (syllabus)\n{lecture.learning_goals}")
    else:
        sections.append("## What was taught this week\nNo slide deck is published for this week.")
    if feedback:
        sections.append(
            "Your previous attempt was rejected for these reasons; fix all of them:\n"
            + "\n".join(f"- {item}" for item in feedback)
        )
    sections.append(
        "## Staff notes per submission (anonymous: what was built / went well / struggled)"
    )
    for index, project in enumerate(active, start=1):
        score = scored.get(project.project_id)
        if score is None:
            continue
        site = f" / site: {project.site_url}" if project.site_url else ""
        sections.append(
            f"- Submission {index}: built: {score.built or 'unclear'} / "
            f"went well: {score.went_well or 'n/a'} / struggled: {score.struggled or 'n/a'}{site}"
        )
    candidates = quote_candidates(digest, scores)
    if candidates:
        sections.append(
            "## Candidate closing quotes, verbatim from students' own posts\n"
            + "\n".join(
                f'{index}. "{text}" \u2014 {label}'
                for index, (_, label, text) in enumerate(candidates, start=1)
            )
        )
    else:
        sections.append("No candidate quotes were found this week; answer quote_choice 0.")
    return "\n\n".join(sections)


def build_highlights_user_prompt(
    digest: WeeklyDigest,
    *,
    selected: Sequence[str],
    feedback: tuple[str, ...] = (),
) -> str:
    by_id = {project.project_id: project for project in digest.projects}
    sections = _week_header(digest)
    sections.append("Featured projects, in order: " + ", ".join(selected))
    if feedback:
        sections.append(
            "Your previous attempt was rejected for these reasons; fix all of them:\n"
            + "\n".join(f"- {item}" for item in feedback)
        )
    sections.append("## Evidence for the featured projects")
    for position, project_id in enumerate(selected, start=1):
        project = by_id.get(project_id)
        if project is not None:
            sections.append(describe_project(project, status=f"FEATURED #{position}"))
    return "\n\n".join(sections)


def _parse(raw: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise NewsletterCompositionError("The model returned invalid JSON.") from error
    if not isinstance(payload, dict):
        raise NewsletterCompositionError("The model returned no JSON object.")
    return cast(dict[str, object], payload)


def _word_count(value: str) -> int:
    return len(value.split())


def _text_problems(value: str, *, field_name: str, owner: str = "") -> list[str]:
    prefix = f"{owner} {field_name}" if owner else field_name
    problems: list[str] = []
    if _LINK_LIKE.search(value):
        problems.append(f"{prefix} must not contain links or addresses")
    banned = _BANNED_WORDS.search(value)
    if banned is not None:
        problems.append(f'{prefix} must not use the word "{banned.group(0)}"')
    return problems


def _reused_phrase(editorial: str, quote: str, *, window: int = 5) -> str | None:
    """A run of `window` consecutive quote words that also appears in the editorial."""

    prose = " ".join(re.sub(r"[^\w\s]", " ", editorial).split()).casefold()
    words = re.sub(r"[^\w\s]", " ", quote).split()
    for start in range(0, max(0, len(words) - window + 1)):
        phrase = " ".join(words[start : start + window]).casefold()
        if phrase in prose:
            return " ".join(words[start : start + window])
    return None


def validate_editorial(
    headline: str,
    editorial: str,
    *,
    names: Sequence[str] = (),
    quotes: Sequence[str] = (),
    project_urls: Sequence[str] = (),
    link_checker: LinkChecker | None = None,
) -> tuple[str, ...]:
    """Project links (to roster sites) are unlimited; external references stay bounded."""

    problems = _text_problems(headline, field_name="headline")
    if _word_count(headline) > WORD_LIMITS["headline"]:
        problems.append(
            f"headline has {_word_count(headline)} words; the limit is {WORD_LIMITS['headline']}"
        )
    links = MARKDOWN_LINK.findall(editorial)
    prose = MARKDOWN_LINK.sub(r"\1", editorial)
    problems += _text_problems(prose, field_name="editorial")
    low, high = EDITORIAL_WORDS
    count = _word_count(prose)
    if not low <= count <= high:
        problems.append(f"editorial has {count} words; write between {low} and {high}")
    for name in names:
        if len(name) >= 4 and re.search(rf"\b{re.escape(name)}\b", prose, re.IGNORECASE):
            problems.append(f'editorial must not name students (found "{name}")')
    tally = _PARTICIPATION_COUNT.search(prose)
    if tally is not None:
        problems.append(f'editorial must not count participation (found "{tally.group(0)}")')
    for quote in quotes:
        reused = _reused_phrase(prose, quote)
        if reused is not None:
            problems.append(
                f'editorial must not reuse wording from a candidate quote (found "{reused}")'
            )
            break
    known = {url.rstrip("/") for url in project_urls}
    external = [url for _, url in links if url.rstrip("/") not in known]
    if len(external) > MAX_EDITORIAL_LINKS:
        problems.append(
            f"editorial has {len(external)} reference links; the limit is {MAX_EDITORIAL_LINKS} "
            "(links to students' own sites do not count)"
        )
    for url in external:
        if link_checker is not None and not link_checker(url):
            problems.append(f"the link {url} does not resolve; remove it or use a real one")
    return tuple(problems)


def validate_highlights(
    highlights: Sequence[Highlight], *, selected: Sequence[str]
) -> tuple[str, ...]:
    """Deterministic checks the model cannot override; returns problems to fix."""

    problems: list[str] = []
    written = [highlight.project_id for highlight in highlights]
    if written != list(selected):
        problems.append(
            "highlights must cover exactly these project ids in this order: "
            + ", ".join(selected)
            + f" (got: {', '.join(written) or 'none'})"
        )
    seen_sentences: dict[str, str] = {}
    for highlight in highlights:
        for field_name in ("headline", "description"):
            value = getattr(highlight, field_name)
            problems += _text_problems(value, field_name=field_name, owner=highlight.project_id)
            if _word_count(value) > WORD_LIMITS[field_name]:
                problems.append(
                    f"{highlight.project_id} {field_name} has {_word_count(value)} words; "
                    f"the limit is {WORD_LIMITS[field_name]}"
                )
        for sentence in _sentences(highlight.description):
            owner = seen_sentences.setdefault(sentence.casefold(), highlight.project_id)
            if owner != highlight.project_id:
                problems.append(
                    f'{highlight.project_id} repeats a sentence from {owner}: "{sentence}"; '
                    "each description must be specific to its own build"
                )
    return tuple(problems)


def _sentences(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", value) if part.strip()]


def compose_editorial(
    digest: WeeklyDigest,
    scores: Sequence[ProjectScore],
    writer: NewsletterWriter,
    *,
    branding: NewsletterBranding,
    lecture: LectureNotes | None = None,
    link_checker: LinkChecker | None = link_resolves,
    max_attempts: int = 4,
) -> EditorialDraft:
    """Headline, editorial, and quote choice from the class's notes; re-prompted on rule breaks."""

    system_prompt = build_editorial_system_prompt(branding)
    names = [project.label for project in digest.projects]
    candidate_count = len(quote_candidates(digest, scores))
    feedback: tuple[str, ...] = ()
    for _ in range(max(1, max_attempts)):
        payload = _parse(
            writer.write(
                system_prompt=system_prompt,
                user_prompt=build_editorial_user_prompt(
                    digest, scores, lecture=lecture, feedback=feedback
                ),
                schema=EDITORIAL_SCHEMA,
            )
        )
        headline = str(payload.get("headline", "")).strip()
        editorial = str(payload.get("editorial", "")).strip()
        raw_choice = payload.get("quote_choice", 0)
        choice = raw_choice if isinstance(raw_choice, int) else 0
        if not headline or not editorial:
            raise NewsletterCompositionError("The model returned an empty headline or editorial.")
        feedback = validate_editorial(
            headline,
            editorial,
            names=names,
            quotes=[text for _, _, text in quote_candidates(digest, scores)],
            project_urls=[
                project.site_url for project in digest.projects if project.site_url is not None
            ],
            link_checker=link_checker,
        )
        if not 0 <= choice <= candidate_count:
            feedback += (f"quote_choice must be between 0 and {candidate_count}",)
        if not feedback:
            return EditorialDraft(headline=headline, editorial=editorial, quote_choice=choice)
    raise NewsletterCompositionError("The editorial broke platform rules: " + "; ".join(feedback))


def compose_highlights(
    digest: WeeklyDigest,
    writer: NewsletterWriter,
    *,
    selected: Sequence[str],
    branding: NewsletterBranding,
    max_attempts: int = 3,
) -> tuple[Highlight, ...]:
    """Highlight copy for exactly the selected projects; re-prompted once on rule breaks."""

    if not selected:
        raise NewsletterCompositionError("No project was selected for highlights.")
    system_prompt = build_highlights_system_prompt(branding)
    feedback: tuple[str, ...] = ()
    for _ in range(max(1, max_attempts)):
        payload = _parse(
            writer.write(
                system_prompt=system_prompt,
                user_prompt=build_highlights_user_prompt(
                    digest, selected=selected, feedback=feedback
                ),
                schema=HIGHLIGHTS_SCHEMA,
            )
        )
        try:
            highlights = tuple(
                Highlight.model_validate(item)
                for item in cast(list[object], payload.get("highlights", []))
            )
        except (ValidationError, TypeError) as error:
            raise NewsletterCompositionError("The highlights did not match the schema.") from error
        feedback = validate_highlights(highlights, selected=selected)
        if not feedback:
            return highlights
    raise NewsletterCompositionError(
        "The highlight copy broke platform rules: " + "; ".join(feedback)
    )


def compose_newsletter(
    digest: WeeklyDigest,
    scores: Sequence[ProjectScore],
    writer: NewsletterWriter,
    *,
    selected: Sequence[str],
    branding: NewsletterBranding,
    lecture: LectureNotes | None = None,
    link_checker: LinkChecker | None = link_resolves,
) -> tuple[NewsletterCopy, tuple[str, str, str] | None]:
    """The issue's copy plus the chosen student quote as (project_id, label, text), if any."""

    draft = compose_editorial(
        digest, scores, writer, branding=branding, lecture=lecture, link_checker=link_checker
    )
    highlights = compose_highlights(digest, writer, selected=selected, branding=branding)
    candidates = quote_candidates(digest, scores)
    chosen = candidates[draft.quote_choice - 1] if draft.quote_choice > 0 else None
    return (
        NewsletterCopy(headline=draft.headline, editorial=draft.editorial, highlights=highlights),
        chosen,
    )
