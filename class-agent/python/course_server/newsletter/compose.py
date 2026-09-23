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

from openai import OpenAI, OpenAIError
from pydantic import SecretStr, ValidationError

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
            "description": "About 200 words on how the class did, in two or three paragraphs.",
        },
    },
    "required": ["headline", "editorial"],
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
# Staff vocabulary that must not leak into student-facing copy. Kept narrow: words like
# "score" are legitimate when describing a build that scores things.
_BANNED_WORDS = re.compile(r"\b(brief|rubric)\b", re.IGNORECASE)
# Brevity is part of the format; the model is re-prompted with the exact overrun.
WORD_LIMITS: dict[str, int] = {"headline": 12, "description": 48}
EDITORIAL_WORDS = (160, 230)
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
        "and editorial.\n"
        "- headline: at most 12 words, a pun or playful turn on this week's assignment itself "
        "(what the class was asked to build), not a generic line about highlights.\n"
        "- editorial: about 200 words (between 160 and 230) in two or three short paragraphs "
        "on how everyone did: what generally went well across the class, what people commonly "
        "struggled with or left unfinished, and one or two patterns worth noticing. Speak to "
        "the class directly and generously; name students only for genuinely notable positive "
        "points, never to single out weak work. Do not list projects; the highlights and the "
        "full list follow separately."
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


def build_editorial_user_prompt(
    digest: WeeklyDigest,
    scores: Sequence[ProjectScore],
    *,
    feedback: tuple[str, ...] = (),
) -> str:
    labels = {project.project_id: project.label for project in digest.projects}
    scored = {score.project_id: score for score in scores}
    active = [project for project in digest.projects if project.active]
    quiet = [project for project in digest.projects if not project.active]
    sections = _week_header(digest)
    sections.append(
        f"{len(digest.projects)} students; {len(active)} posted work this week; "
        f"{len(quiet)} have nothing beyond the starter template yet."
    )
    if feedback:
        sections.append(
            "Your previous attempt was rejected for these reasons; fix all of them:\n"
            + "\n".join(f"- {item}" for item in feedback)
        )
    sections.append("## Staff notes per student (label: what they built / went well / struggled)")
    for project in active:
        score = scored.get(project.project_id)
        if score is None:
            sections.append(f"- {labels[project.project_id]}: submission could not be assessed.")
            continue
        sections.append(
            f"- {labels[project.project_id]} (assessment {score.total:.1f}/10): "
            f"built: {score.built or 'unclear'} / went well: {score.went_well or 'n/a'} / "
            f"struggled: {score.struggled or 'n/a'}"
        )
    if quiet:
        sections.append(
            "Nothing posted yet: " + ", ".join(labels[item.project_id] for item in quiet)
        )
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


def validate_editorial(headline: str, editorial: str) -> tuple[str, ...]:
    problems = _text_problems(headline, field_name="headline")
    problems += _text_problems(editorial, field_name="editorial")
    if _word_count(headline) > WORD_LIMITS["headline"]:
        problems.append(
            f"headline has {_word_count(headline)} words; the limit is {WORD_LIMITS['headline']}"
        )
    low, high = EDITORIAL_WORDS
    count = _word_count(editorial)
    if not low <= count <= high:
        problems.append(f"editorial has {count} words; write between {low} and {high}")
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
    max_attempts: int = 2,
) -> tuple[str, str]:
    """Headline and editorial from the whole class's notes; re-prompted once on rule breaks."""

    system_prompt = build_editorial_system_prompt(branding)
    feedback: tuple[str, ...] = ()
    for _ in range(max(1, max_attempts)):
        payload = _parse(
            writer.write(
                system_prompt=system_prompt,
                user_prompt=build_editorial_user_prompt(digest, scores, feedback=feedback),
                schema=EDITORIAL_SCHEMA,
            )
        )
        headline = str(payload.get("headline", "")).strip()
        editorial = str(payload.get("editorial", "")).strip()
        if not headline or not editorial:
            raise NewsletterCompositionError("The model returned an empty headline or editorial.")
        feedback = validate_editorial(headline, editorial)
        if not feedback:
            return headline, editorial
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
) -> NewsletterCopy:
    headline, editorial = compose_editorial(digest, scores, writer, branding=branding)
    highlights = compose_highlights(digest, writer, selected=selected, branding=branding)
    return NewsletterCopy(headline=headline, editorial=editorial, highlights=highlights)
