"""Typed configuration and portable records for the weekly course newsletter."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from course_server.auth.models import AwareDatetime
from course_server.config import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_NEWSLETTER_DATA_PATH = PROJECT_ROOT / "var/newsletter"
DEFAULT_SCHEDULE_PATH = PROJECT_ROOT / "shared/course/schedule/schedule.md"
ISSUE_ID_PATTERN = r"^[0-9]{4}-week[0-9]{2}$"

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
ProjectId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,100}$")]


class NewsletterModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NewsletterBranding(NewsletterModel):
    """Course identity printed in every issue; stored with the issue so renders stay stable."""

    newsletter_name: ShortText = "The Class Runtime"
    course_code: ShortText = "MAS.S60"
    course_title: ShortText = "AI Agents for Cognitive Augmentation"
    course_term: ShortText = "Fall 2026"
    institution: ShortText = "MIT"
    course_site_url: Annotated[str, StringConstraints(pattern=r"^https://[^\s]+$")] = (
        "https://cognitive-agents.media.mit.edu"
    )
    sender_name: ShortText = "The MAS.S60 teaching team"


class NewsletterSettings(NewsletterModel):
    """Deployment-owned newsletter configuration read from the environment."""

    branding: NewsletterBranding = NewsletterBranding()
    subject: ShortText = "The Class Runtime from MAS.S60"
    timezone: ShortText = "America/New_York"
    highlight_count: int = Field(default=4, ge=1, le=8)
    highlight_cooldown_issues: int = Field(default=2, ge=0, le=12)
    recipients: tuple[EmailStr, ...] = ()
    data_path: Path = DEFAULT_NEWSLETTER_DATA_PATH
    schedule_path: Path = DEFAULT_SCHEDULE_PATH

    @classmethod
    def from_environment(cls, values: Mapping[str, str]) -> NewsletterSettings:
        def text(name: str, default: str) -> str:
            raw = values.get(name)
            return raw.strip() if raw and raw.strip() else default

        def integer(name: str, default: int) -> int:
            raw = values.get(name)
            if raw is None or not raw.strip():
                return default
            try:
                return int(raw.strip())
            except ValueError as error:
                raise ConfigurationError(f"{name} must be an integer") from error

        def path(name: str, default: Path) -> Path:
            raw = values.get(name)
            return Path(raw.strip()).expanduser() if raw and raw.strip() else default

        defaults = NewsletterBranding()
        branding = NewsletterBranding(
            newsletter_name=text("NEWSLETTER_NAME", defaults.newsletter_name),
            course_code=text("NEWSLETTER_COURSE_CODE", defaults.course_code),
            course_title=text("NEWSLETTER_COURSE_TITLE", defaults.course_title),
            course_term=text("NEWSLETTER_COURSE_TERM", defaults.course_term),
            institution=text("NEWSLETTER_INSTITUTION", defaults.institution),
            course_site_url=text("NEWSLETTER_COURSE_SITE_URL", defaults.course_site_url),
            sender_name=text("NEWSLETTER_SENDER_NAME", defaults.sender_name),
        )
        recipients = tuple(
            address.strip()
            for address in values.get("NEWSLETTER_RECIPIENTS", "").split(",")
            if address.strip()
        )
        return cls(
            branding=branding,
            subject=text("NEWSLETTER_SUBJECT", "The Class Runtime from MAS.S60"),
            timezone=text("NEWSLETTER_TIMEZONE", "America/New_York"),
            highlight_count=integer("NEWSLETTER_HIGHLIGHT_COUNT", 4),
            highlight_cooldown_issues=integer("NEWSLETTER_HIGHLIGHT_COOLDOWN_ISSUES", 2),
            recipients=recipients,
            data_path=path("NEWSLETTER_DATA_PATH", DEFAULT_NEWSLETTER_DATA_PATH),
            schedule_path=path("NEWSLETTER_SCHEDULE_PATH", DEFAULT_SCHEDULE_PATH),
        )


class CourseWeek(NewsletterModel):
    """One scheduled class week and the build window that follows it."""

    number: int = Field(ge=1, le=52)
    class_date: date
    topic: ShortText
    tutorial: ShortText
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    has_class: bool = True


class CommitSummary(NewsletterModel):
    committed_at: AwareDatetime
    subject: Annotated[str, StringConstraints(max_length=200)]


class ProjectDocument(NewsletterModel):
    path: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    text: str
    truncated: bool = False


class ProjectEvidence(NewsletterModel):
    """Bounded, read-only evidence of one student's work during a course week."""

    project_id: ProjectId
    label: ShortText
    site_url: str | None = None
    week_file_count: int = Field(ge=0)
    site_file_count: int = Field(ge=0)
    commit_count: int = Field(ge=0)
    commits: tuple[CommitSummary, ...] = ()
    documents: tuple[ProjectDocument, ...] = ()
    site_text: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        """Whether the student changed anything reviewable during the week."""

        return self.week_file_count > 0 or self.commit_count > 0 or self.site_file_count > 1


class WeeklyDigest(NewsletterModel):
    week: CourseWeek
    projects: tuple[ProjectEvidence, ...]
    cooldown_project_ids: frozenset[str] = frozenset()
    highlight_count: int = Field(ge=1, le=8)

    def eligible_project_ids(self) -> tuple[str, ...]:
        return tuple(
            project.project_id
            for project in self.projects
            if project.active and project.project_id not in self.cooldown_project_ids
        )

    def target_highlight_count(self) -> int:
        return min(self.highlight_count, len(self.eligible_project_ids()))


class Highlight(NewsletterModel):
    project_id: ProjectId
    headline: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=700)
    ]


class NewsletterCopy(NewsletterModel):
    """Model-authored prose; platform code owns selection, links, ordering, quote, and footer."""

    opening: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=900)]
    highlights: tuple[Highlight, ...] = Field(max_length=8)


class ProjectScore(NewsletterModel):
    """Rubric scores for one project's week, produced by the model and ranked in code."""

    project_id: ProjectId
    interest: int = Field(ge=0, le=10)
    execution: int = Field(ge=0, le=10)
    goal_fit: int = Field(ge=0, le=10)
    total: float = Field(ge=0, le=10)
    rationale: Annotated[str, StringConstraints(strip_whitespace=True, max_length=600)] = ""
    eligible: bool = True


class PioneerQuote(NewsletterModel):
    text: ShortText
    author: ShortText
    source: ShortText


class ProjectLink(NewsletterModel):
    project_id: ProjectId
    label: ShortText
    site_url: str | None = None


class HighlightImage(NewsletterModel):
    """A screenshot of a highlighted build, stored next to the issue and inlined in email."""

    project_id: ProjectId
    filename: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,120}$")]
    media_type: Literal["image/jpeg", "image/png"]
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    kind: Literal["post_image", "screenshot"] = "screenshot"
    source_url: str | None = None
    page_url: str | None = None

    @property
    def content_id(self) -> str:
        return self.project_id


class Delivery(NewsletterModel):
    recipient: EmailStr
    provider_message_id: str | None = None
    error: str | None = None
    attempted_at: AwareDatetime


class NewsletterIssue(NewsletterModel):
    """One portable newsletter issue: draft until an instructor explicitly sends it."""

    schema_version: Literal[1] = 1
    issue_id: Annotated[str, StringConstraints(pattern=ISSUE_ID_PATTERN)]
    week: CourseWeek
    branding: NewsletterBranding
    subject: ShortText
    body: NewsletterCopy
    roster: tuple[ProjectLink, ...]
    quote: PioneerQuote
    scores: tuple[ProjectScore, ...] = ()
    images: tuple[HighlightImage, ...] = ()
    model_id: str | None = None
    status: Literal["draft", "sent"] = "draft"
    created_at: AwareDatetime
    sent_at: AwareDatetime | None = None
    deliveries: tuple[Delivery, ...] = ()

    def highlighted_project_ids(self) -> tuple[str, ...]:
        return tuple(highlight.project_id for highlight in self.body.highlights)

    def link_for(self, project_id: str) -> ProjectLink | None:
        return next((link for link in self.roster if link.project_id == project_id), None)

    def image_for(self, project_id: str) -> HighlightImage | None:
        return next((image for image in self.images if image.project_id == project_id), None)

    def other_projects(self) -> tuple[ProjectLink, ...]:
        highlighted = set(self.highlighted_project_ids())
        return tuple(link for link in self.roster if link.project_id not in highlighted)


def issue_id_for(week: CourseWeek) -> str:
    return f"{week.class_date.year}-week{week.number:02d}"
