"""Instructor-run newsletter workflow: draft for review, then send only on explicit approval."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from pydantic import EmailStr, ValidationError

from course_server.mail.models import MailAdapter, OutboundMail

from .collect import WeeklyEvidenceCollector
from .compose import NewsletterWriter, compose_newsletter
from .models import (
    CourseWeek,
    Delivery,
    NewsletterIssue,
    NewsletterModel,
    NewsletterSettings,
    PioneerQuote,
    ProjectLink,
    WeeklyDigest,
    issue_id_for,
)
from .quotes import PIONEER_QUOTES, choose_quote
from .render import render_html, render_text
from .schedule import select_week
from .store import FileNewsletterStore


class NewsletterStateError(RuntimeError):
    """The requested transition is not allowed for the issue's current state."""


class _Recipients(NewsletterModel):
    addresses: tuple[EmailStr, ...]


def normalize_recipients(values: Sequence[str]) -> tuple[str, ...]:
    """Validate, trim, and de-duplicate addresses while preserving order."""

    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        address = value.strip()
        if address and address.casefold() not in seen:
            seen.add(address.casefold())
            ordered.append(address)
    try:
        return tuple(str(item) for item in _Recipients(addresses=tuple(ordered)).addresses)
    except ValidationError as error:
        raise NewsletterStateError("One or more recipient addresses are invalid.") from error


class NewsletterService:
    def __init__(
        self,
        *,
        settings: NewsletterSettings,
        weeks: Sequence[CourseWeek],
        store: FileNewsletterStore,
        collector: WeeklyEvidenceCollector | None = None,
        writer: NewsletterWriter | None = None,
        quotes: tuple[PioneerQuote, ...] = PIONEER_QUOTES,
        model_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._settings = settings
        self._weeks = tuple(weeks)
        self._collector = collector
        self._writer = writer
        self._store = store
        self._quotes = quotes
        self._model_id = model_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._log = log or (lambda message: None)

    def resolve_week(
        self, *, week_number: int | None = None, as_of: date | None = None
    ) -> CourseWeek:
        today = as_of or self._clock().astimezone(ZoneInfo(self._settings.timezone)).date()
        return select_week(self._weeks, week_number=week_number, as_of=today)

    def draft(
        self,
        *,
        week_number: int | None = None,
        as_of: date | None = None,
        force: bool = False,
    ) -> NewsletterIssue:
        if self._collector is None or self._writer is None:
            raise NewsletterStateError("Drafting requires the repository collector and a writer.")
        week = self.resolve_week(week_number=week_number, as_of=as_of)
        issue_id = issue_id_for(week)
        existing = self._store.load(issue_id)
        if existing is not None and existing.status == "sent" and not force:
            raise NewsletterStateError(
                f"Issue {issue_id} was already sent on {existing.sent_at}; pass force to redraft."
            )
        cooldown = self._store.recently_highlighted(
            before_week=week.number,
            cooldown=self._settings.highlight_cooldown_issues,
        )
        evidence = self._collector.collect(week)
        digest = WeeklyDigest(
            week=week,
            projects=evidence,
            cooldown_project_ids=cooldown,
            highlight_count=self._settings.highlight_count,
        )
        self._log(
            f"{len(digest.eligible_project_ids())} of {len(evidence)} projects are eligible; "
            f"asking the model for {digest.target_highlight_count()} highlights."
        )
        copy = compose_newsletter(digest, self._writer, branding=self._settings.branding)
        issue = NewsletterIssue(
            issue_id=issue_id,
            week=week,
            branding=self._settings.branding,
            subject=self._settings.subject,
            body=copy,
            roster=tuple(
                ProjectLink(project_id=item.project_id, label=item.label, site_url=item.site_url)
                for item in evidence
            ),
            quote=choose_quote(
                week_number=week.number,
                used_texts=self._store.used_quote_texts(),
                quotes=self._quotes,
            ),
            model_id=self._model_id,
            created_at=self._clock(),
        )
        self._store.save(issue)
        return issue

    def load(self, issue_id: str) -> NewsletterIssue:
        issue = self._store.load(issue_id)
        if issue is None:
            raise NewsletterStateError(f"Issue {issue_id} does not exist; draft it first.")
        return issue

    async def send(
        self,
        issue_id: str,
        *,
        mail: MailAdapter,
        recipients: Sequence[str],
        test_only: bool = False,
    ) -> NewsletterIssue:
        """Send one message per recipient. A test send never changes the stored issue."""

        issue = self.load(issue_id)
        if issue.status == "sent" and not test_only:
            raise NewsletterStateError(f"Issue {issue_id} was already sent on {issue.sent_at}.")
        addresses = normalize_recipients(recipients)
        if not addresses:
            raise NewsletterStateError("At least one recipient is required.")
        text = render_text(issue)
        html = render_html(issue)
        deliveries: list[Delivery] = []
        for address in addresses:
            attempted_at = self._clock()
            try:
                sent = await mail.send_message(
                    OutboundMail(to=(address,), subject=issue.subject, text=text, html=html)
                )
            except Exception as error:  # record the class, keep sending
                self._log(f"  {address}: failed ({type(error).__name__})")
                deliveries.append(
                    Delivery(
                        recipient=address,
                        error=type(error).__name__,
                        attempted_at=attempted_at,
                    )
                )
                continue
            self._log(f"  {address}: sent")
            deliveries.append(
                Delivery(
                    recipient=address,
                    provider_message_id=sent.provider_message_id,
                    attempted_at=attempted_at,
                )
            )
        succeeded = any(delivery.error is None for delivery in deliveries)
        if test_only:
            return issue.model_copy(update={"deliveries": tuple(deliveries)})
        updated = issue.model_copy(
            update={
                "status": "sent" if succeeded else "draft",
                "sent_at": self._clock() if succeeded else None,
                "deliveries": tuple(deliveries),
            }
        )
        self._store.save(updated)
        if not succeeded:
            raise NewsletterStateError("No recipient accepted the newsletter; it remains a draft.")
        return updated
