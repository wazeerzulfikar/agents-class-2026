"""Turn a weekly digest into validated newsletter copy through a replaceable model writer.

The model chooses which eligible builds to feature and writes the prose. Platform code
decides eligibility, enforces the highlight count and cooldown, and owns every link.
"""

from __future__ import annotations

import json
import re
from typing import Protocol

from openai import OpenAI, OpenAIError
from pydantic import SecretStr, ValidationError

from .models import NewsletterBranding, NewsletterCopy, ProjectEvidence, WeeklyDigest

NEWSLETTER_COPY_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "opening": {
            "type": "string",
            "description": "One headline-like sentence that opens the issue.",
        },
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "headline": {"type": "string"},
                    "summary": {"type": "string"},
                    "goal_link": {"type": "string"},
                },
                "required": ["project_id", "headline", "summary", "goal_link"],
                "additionalProperties": False,
            },
        },
        "closing": {"type": "string"},
    },
    "required": ["opening", "highlights", "closing"],
    "additionalProperties": False,
}
_LINK_LIKE = re.compile(r"https?://|www\.|@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", re.IGNORECASE)
# Brevity is part of the format; the model is re-prompted with the exact overrun.
WORD_LIMITS: dict[str, int] = {
    "headline": 8,
    "summary": 32,
    "goal_link": 28,
    "opening": 28,
    "closing": 28,
}


def _word_count(value: str) -> int:
    return len(value.split())


class NewsletterCompositionError(RuntimeError):
    """The model could not produce copy that satisfies the platform's rules."""


class NewsletterWriter(Protocol):
    """Returns JSON text matching `schema`; the platform parses and validates it."""

    def write(self, *, system_prompt: str, user_prompt: str, schema: dict[str, object]) -> str: ...


class OpenAINewsletterWriter:
    """Chat Completions writer that keeps the API key inside this process."""

    def __init__(
        self,
        *,
        model_id: str,
        api_key: SecretStr,
        api_base: str = "https://api.openai.com/v1",
    ) -> None:
        self.model_id = model_id
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
        content = completion.choices[0].message.content if completion.choices else None
        if not content:
            raise NewsletterCompositionError("The model returned no newsletter copy.")
        return content


def build_system_prompt(branding: NewsletterBranding) -> str:
    return (
        f'You write "{branding.newsletter_name}", the weekly newsletter of {branding.course_code} '
        f"{branding.course_title} at {branding.institution}, {branding.course_term}. Readers are "
        "the students and staff of the class, who skim it on their phones.\n\n"
        "Voice: warm, witty, and quick. Use clever puns and playful headlines, but keep every "
        "factual claim grounded in the evidence you are given. Never invent features, results, "
        "or names. Refer to each student by the label provided.\n\n"
        "Rules:\n"
        "- Feature exactly the requested number of highlights, each a different eligible project.\n"
        "- Prefer builds that are complete, documented, creative, and clearly meet the week's "
        "goal. Never feature a project marked ineligible.\n"
        "- Brevity is the format. Each highlight has: a pun-friendly headline of at most six "
        "words; a summary of exactly one sentence (at most 28 words) saying what the build is; "
        "and a goal_link of exactly one sentence (at most 24 words) stating specifically how it "
        "relates to the week's assignment goal. A screenshot of the build is shown above the "
        "text, so do not describe what the site looks like.\n"
        "- The opening is one sentence (at most 24 words) that reads like a headline for the "
        "week; the closing is one sentence (at most 24 words) handing off to the full list of "
        "builds and the quote.\n"
        "- Write plain text only: no Markdown, no bullet characters, no URLs, no email "
        "addresses, no emoji. Links, the full project list, the quote, and the footer are "
        "added by the platform.\n"
        "- Respond with JSON that matches the provided schema and nothing else."
    )


def _describe_project(project: ProjectEvidence, *, status: str) -> str:
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


def build_user_prompt(digest: WeeklyDigest, *, feedback: tuple[str, ...] = ()) -> str:
    week = digest.week
    eligible = set(digest.eligible_project_ids())
    target = digest.target_highlight_count()
    sections = [
        f"# Week {week.number} (class on {week.class_date.isoformat()})",
        f"Lecture topic: {week.topic}",
        f"Hands-on assignment goal for the week: {week.tutorial}",
        f"Build window: {week.starts_at.date().isoformat()} to {week.ends_at.date().isoformat()}",
        f"Number of highlights to write: {target}",
        "Eligible project ids: " + (", ".join(sorted(eligible)) or "none"),
    ]
    if digest.cooldown_project_ids:
        sections.append(
            "Recently featured and therefore ineligible this week: "
            + ", ".join(sorted(digest.cooldown_project_ids))
        )
    if feedback:
        sections.append(
            "Your previous attempt was rejected for these reasons; fix all of them:\n"
            + "\n".join(f"- {item}" for item in feedback)
        )
    sections.append("## Evidence per project")
    for project in digest.projects:
        if project.project_id in eligible:
            status = "ELIGIBLE"
        elif project.project_id in digest.cooldown_project_ids:
            status = "INELIGIBLE (featured recently)"
        else:
            status = "INELIGIBLE (no build activity this week)"
        sections.append(_describe_project(project, status=status))
    return "\n\n".join(sections)


def parse_copy(raw: str) -> NewsletterCopy:
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise NewsletterCompositionError("The model returned invalid JSON.") from error
    try:
        return NewsletterCopy.model_validate(payload)
    except ValidationError as error:
        raise NewsletterCompositionError(
            f"The model copy did not match the schema: {error.error_count()} problem(s)."
        ) from error


def validate_copy(copy: NewsletterCopy, digest: WeeklyDigest) -> tuple[str, ...]:
    """Deterministic checks the model cannot override; returns problems to fix."""

    problems: list[str] = []
    eligible = set(digest.eligible_project_ids())
    target = digest.target_highlight_count()
    if len(copy.highlights) != target:
        problems.append(f"expected exactly {target} highlights, got {len(copy.highlights)}")
    seen: set[str] = set()
    for highlight in copy.highlights:
        if highlight.project_id in seen:
            problems.append(f"{highlight.project_id} is highlighted more than once")
        seen.add(highlight.project_id)
        if highlight.project_id in digest.cooldown_project_ids:
            problems.append(f"{highlight.project_id} was featured recently and is ineligible")
        elif highlight.project_id not in eligible:
            problems.append(f"{highlight.project_id} is not an eligible project id")
        for field_name in ("headline", "summary", "goal_link"):
            value = getattr(highlight, field_name)
            if _LINK_LIKE.search(value):
                problems.append(f"{highlight.project_id} {field_name} must not contain links")
            if _word_count(value) > WORD_LIMITS[field_name]:
                problems.append(
                    f"{highlight.project_id} {field_name} has {_word_count(value)} words; "
                    f"the limit is {WORD_LIMITS[field_name]}"
                )
    for field_name in ("opening", "closing"):
        value = getattr(copy, field_name)
        if _LINK_LIKE.search(value):
            problems.append(f"{field_name} must not contain links or addresses")
        if _word_count(value) > WORD_LIMITS[field_name]:
            problems.append(
                f"{field_name} has {_word_count(value)} words; "
                f"the limit is {WORD_LIMITS[field_name]}"
            )
    return tuple(problems)


def compose_newsletter(
    digest: WeeklyDigest,
    writer: NewsletterWriter,
    *,
    branding: NewsletterBranding,
    max_attempts: int = 2,
) -> NewsletterCopy:
    """Ask the writer for copy and re-prompt once with concrete problems if it breaks a rule."""

    if digest.target_highlight_count() == 0:
        raise NewsletterCompositionError("No eligible project has build activity this week.")
    system_prompt = build_system_prompt(branding)
    feedback: tuple[str, ...] = ()
    for _ in range(max(1, max_attempts)):
        raw = writer.write(
            system_prompt=system_prompt,
            user_prompt=build_user_prompt(digest, feedback=feedback),
            schema=NEWSLETTER_COPY_SCHEMA,
        )
        copy = parse_copy(raw)
        feedback = validate_copy(copy, digest)
        if not feedback:
            return copy
    raise NewsletterCompositionError("The model copy broke platform rules: " + "; ".join(feedback))
