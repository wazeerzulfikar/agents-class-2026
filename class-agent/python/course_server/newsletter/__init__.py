"""Instructor-run weekly newsletter built from read-only student repository evidence."""

from .collect import EvidenceLimits, WeeklyEvidenceCollector, project_label
from .compose import (
    NEWSLETTER_COPY_SCHEMA,
    NewsletterCompositionError,
    NewsletterWriter,
    OpenAINewsletterWriter,
    build_system_prompt,
    build_user_prompt,
    compose_newsletter,
    validate_copy,
)
from .models import (
    CommitSummary,
    CourseWeek,
    Delivery,
    Highlight,
    NewsletterBranding,
    NewsletterCopy,
    NewsletterIssue,
    NewsletterSettings,
    PioneerQuote,
    ProjectDocument,
    ProjectEvidence,
    ProjectLink,
    WeeklyDigest,
    issue_id_for,
)
from .quotes import PIONEER_QUOTES, choose_quote
from .render import render_html, render_text
from .schedule import NewsletterScheduleError, load_schedule, parse_schedule, select_week
from .service import NewsletterService, NewsletterStateError, normalize_recipients
from .store import FileNewsletterStore, NewsletterStoreError

__all__ = [
    "NEWSLETTER_COPY_SCHEMA",
    "PIONEER_QUOTES",
    "CommitSummary",
    "CourseWeek",
    "Delivery",
    "EvidenceLimits",
    "FileNewsletterStore",
    "Highlight",
    "NewsletterBranding",
    "NewsletterCompositionError",
    "NewsletterCopy",
    "NewsletterIssue",
    "NewsletterScheduleError",
    "NewsletterService",
    "NewsletterSettings",
    "NewsletterStateError",
    "NewsletterStoreError",
    "NewsletterWriter",
    "OpenAINewsletterWriter",
    "PioneerQuote",
    "ProjectDocument",
    "ProjectEvidence",
    "ProjectLink",
    "WeeklyDigest",
    "WeeklyEvidenceCollector",
    "build_system_prompt",
    "build_user_prompt",
    "choose_quote",
    "compose_newsletter",
    "issue_id_for",
    "load_schedule",
    "normalize_recipients",
    "parse_schedule",
    "project_label",
    "render_html",
    "render_text",
    "select_week",
    "validate_copy",
]
