"""Model boundary for the newsletter: rubric scores, copy, and image choice.

Platform code decides who is featured (see `score.py`), the order, every link, the quote,
and the footer. The model writes the prose for the already-selected builds, scores
projects one at a time, and picks among measured image candidates.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Sequence
from typing import Any, Protocol, cast

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
                    "description": {"type": "string"},
                },
                "required": ["project_id", "headline", "description"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["opening", "highlights"],
    "additionalProperties": False,
}
_LINK_LIKE = re.compile(r"https?://|www\.|@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", re.IGNORECASE)
# Brevity is part of the format; the model is re-prompted with the exact overrun.
WORD_LIMITS: dict[str, int] = {"headline": 8, "description": 48, "opening": 28}
_CHOICE = re.compile(r'"choice"\s*:\s*(\d+)')


class NewsletterCompositionError(RuntimeError):
    """The model could not produce copy that satisfies the platform's rules."""


class NewsletterWriter(Protocol):
    """Text and image-choice calls; the platform parses and validates every response."""

    def write(self, *, system_prompt: str, user_prompt: str, schema: dict[str, object]) -> str: ...

    def judge_images(self, *, prompt: str, images: Sequence[bytes]) -> int: ...


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
        match = _CHOICE.search(response.output_text)
        if match is None:
            raise NewsletterCompositionError("The model returned no image choice.")
        return int(match.group(1))


def build_system_prompt(branding: NewsletterBranding) -> str:
    return (
        f'You write "{branding.newsletter_name}", the weekly newsletter of {branding.course_code} '
        f"{branding.course_title} at {branding.institution}, {branding.course_term}. Readers are "
        "the students and staff of the class, who skim it on their phones.\n\n"
        "Voice: warm, witty, and quick. Use clever puns and playful headlines, but keep every "
        "factual claim grounded in the evidence you are given. Never invent features, results, "
        "or names. Refer to each student by the label provided.\n\n"
        "Rules:\n"
        "- Course staff already chose the featured builds and their order. Write one highlight "
        "per listed project, in the given order, using the given project ids.\n"
        "- Each highlight has a pun-friendly headline of at most six words and a description "
        "of exactly two sentences (at most 40 words in total): the first says what the build "
        "is and what makes it interesting; the second says specifically how it does what this "
        "week's assignment asked. An image of the build appears above the text, so do not "
        "describe what it looks like.\n"
        "- The opening is one sentence (at most 24 words) that reads like a headline for the "
        "week.\n"
        "- Write plain text only: no Markdown, no bullet characters, no URLs, no email "
        "addresses, no emoji. Links, the full project list, the quote, and the footer are "
        "added by the platform.\n"
        "- Respond with JSON that matches the provided schema and nothing else."
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


def build_user_prompt(
    digest: WeeklyDigest,
    *,
    selected: Sequence[str],
    feedback: tuple[str, ...] = (),
) -> str:
    week = digest.week
    by_id = {project.project_id: project for project in digest.projects}
    sections = [
        f"# Week {week.number} (class on {week.class_date.isoformat()})",
        f"Lecture topic: {week.topic}",
        f"Hands-on assignment goal for the week: {week.tutorial}",
        f"Build window: {week.starts_at.date().isoformat()} to {week.ends_at.date().isoformat()}",
        "Featured projects, in order: " + ", ".join(selected),
        f"Total projects this week: {len(digest.projects)}",
    ]
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


def _word_count(value: str) -> int:
    return len(value.split())


def validate_copy(copy: NewsletterCopy, *, selected: Sequence[str]) -> tuple[str, ...]:
    """Deterministic checks the model cannot override; returns problems to fix."""

    problems: list[str] = []
    written = [highlight.project_id for highlight in copy.highlights]
    if written != list(selected):
        problems.append(
            "highlights must cover exactly these project ids in this order: "
            + ", ".join(selected)
            + f" (got: {', '.join(written) or 'none'})"
        )
    for highlight in copy.highlights:
        for field_name in ("headline", "description"):
            value = getattr(highlight, field_name)
            if _LINK_LIKE.search(value):
                problems.append(f"{highlight.project_id} {field_name} must not contain links")
            if _word_count(value) > WORD_LIMITS[field_name]:
                problems.append(
                    f"{highlight.project_id} {field_name} has {_word_count(value)} words; "
                    f"the limit is {WORD_LIMITS[field_name]}"
                )
    if _LINK_LIKE.search(copy.opening):
        problems.append("opening must not contain links or addresses")
    if _word_count(copy.opening) > WORD_LIMITS["opening"]:
        problems.append(
            f"opening has {_word_count(copy.opening)} words; the limit is {WORD_LIMITS['opening']}"
        )
    return tuple(problems)


def compose_newsletter(
    digest: WeeklyDigest,
    writer: NewsletterWriter,
    *,
    selected: Sequence[str],
    branding: NewsletterBranding,
    max_attempts: int = 2,
) -> NewsletterCopy:
    """Ask the writer for copy and re-prompt once with concrete problems if it breaks a rule."""

    if not selected:
        raise NewsletterCompositionError("No project was selected for highlights.")
    system_prompt = build_system_prompt(branding)
    feedback: tuple[str, ...] = ()
    for _ in range(max(1, max_attempts)):
        raw = writer.write(
            system_prompt=system_prompt,
            user_prompt=build_user_prompt(digest, selected=selected, feedback=feedback),
            schema=NEWSLETTER_COPY_SCHEMA,
        )
        copy = parse_copy(raw)
        feedback = validate_copy(copy, selected=selected)
        if not feedback:
            return copy
    raise NewsletterCompositionError("The model copy broke platform rules: " + "; ".join(feedback))
