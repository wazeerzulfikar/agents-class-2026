"""Local JSON storage for newsletter issues plus reviewable text/HTML renderings."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import ValidationError

from .models import ISSUE_ID_PATTERN, NewsletterIssue
from .render import render_html, render_text

_ISSUE_ID = re.compile(ISSUE_ID_PATTERN)
_IMAGE_FILENAME = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


class NewsletterStoreError(RuntimeError):
    """A stored issue is unreadable or an identifier is unsafe."""


class FileNewsletterStore:
    """One `<issue_id>.json` per issue under `<root>/issues/`, ignored by Git."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def issues_directory(self) -> Path:
        return self._root / "issues"

    def paths_for(self, issue_id: str) -> tuple[Path, Path, Path]:
        if not _ISSUE_ID.fullmatch(issue_id):
            raise NewsletterStoreError(f"Invalid issue id: {issue_id!r}")
        base = self.issues_directory / issue_id
        return base.with_suffix(".json"), base.with_suffix(".txt"), base.with_suffix(".html")

    def save(self, issue: NewsletterIssue) -> Path:
        json_path, text_path, html_path = self.paths_for(issue.issue_id)
        self.issues_directory.mkdir(parents=True, exist_ok=True)
        temporary = json_path.with_suffix(".json.tmp")
        temporary.write_text(issue.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(json_path)
        text_path.write_text(render_text(issue), encoding="utf-8")
        html_path.write_text(render_html(issue), encoding="utf-8")
        return json_path

    def image_path(self, issue_id: str, filename: str) -> Path:
        self.paths_for(issue_id)
        if not _IMAGE_FILENAME.fullmatch(filename) or filename.startswith("."):
            raise NewsletterStoreError(f"Invalid image filename: {filename!r}")
        return self.issues_directory / issue_id / filename

    def save_image(self, issue_id: str, filename: str, data: bytes) -> Path:
        path = self.image_path(issue_id, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def clear_images(self, issue_id: str) -> None:
        """Remove a previous draft's images so a redraft leaves no stale files behind."""

        self.paths_for(issue_id)
        directory = self.issues_directory / issue_id
        if not directory.is_dir() or directory.is_symlink():
            return
        for path in directory.iterdir():
            if path.is_file() and not path.is_symlink() and _IMAGE_FILENAME.fullmatch(path.name):
                path.unlink()

    def load_image(self, issue_id: str, filename: str) -> bytes:
        path = self.image_path(issue_id, filename)
        if not path.is_file() or path.is_symlink():
            raise NewsletterStoreError(f"Image {filename} for {issue_id} is missing.")
        return path.read_bytes()

    def load(self, issue_id: str) -> NewsletterIssue | None:
        json_path, _, _ = self.paths_for(issue_id)
        if not json_path.is_file() or json_path.is_symlink():
            return None
        try:
            return NewsletterIssue.model_validate_json(json_path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError) as error:
            raise NewsletterStoreError(f"Stored issue {issue_id} is unreadable.") from error

    def list_issues(self) -> tuple[NewsletterIssue, ...]:
        if not self.issues_directory.is_dir():
            return ()
        issues: list[NewsletterIssue] = []
        for path in sorted(self.issues_directory.glob("*.json")):
            if _ISSUE_ID.fullmatch(path.stem) and (issue := self.load(path.stem)) is not None:
                issues.append(issue)
        return tuple(sorted(issues, key=lambda issue: (issue.week.class_date, issue.issue_id)))

    def sent_issues(self) -> tuple[NewsletterIssue, ...]:
        return tuple(issue for issue in self.list_issues() if issue.status == "sent")

    def recently_highlighted(self, *, before_week: int, cooldown: int) -> frozenset[str]:
        """Project ids featured in the last `cooldown` sent issues before `before_week`."""

        if cooldown <= 0:
            return frozenset()
        earlier = [issue for issue in self.sent_issues() if issue.week.number < before_week]
        recent = earlier[-cooldown:]
        return frozenset(
            project_id for issue in recent for project_id in issue.highlighted_project_ids()
        )

    def used_quote_texts(self) -> frozenset[str]:
        return frozenset(issue.quote.text for issue in self.sent_issues())
