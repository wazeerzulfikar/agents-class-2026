"""Gather bounded, read-only evidence of each student's weekly build from GitHub."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from course_server.student_projects import (
    StudentProject,
    StudentProjectCatalog,
    StudentProjectNotFound,
    StudentProjectProviderError,
)

from .images import discover_week_anchor, discover_week_pages, static_anchors
from .models import CommitSummary, CourseWeek, ProjectDocument, ProjectEvidence

WEEKLY_BUILDS_DIRECTORY = "weekly_builds"
SITE_DIRECTORY = "website/"
_DOCUMENT_SUFFIXES = (".md", ".txt", ".rst")
_IGNORED_PATH_PARTS = frozenset({"node_modules", ".git", "__pycache__", "venv", ".venv"})
_WHITESPACE = re.compile(r"[ \t\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")


class SiteReader(Protocol):
    """Reads one deployed public site; matches `fetch_public_webpage`'s return shape."""

    def __call__(self, url: str) -> str | dict[str, object]: ...


class WeekPageFinder(Protocol):
    """Finds the student's post for the week from their site root, or None."""

    def __call__(self, site_url: str, week: CourseWeek) -> str | None: ...


def find_week_page(site_url: str, week: CourseWeek) -> str | None:
    """The first same-site link naming the week in the served HTML, else a week section."""

    anchors = static_anchors(site_url)
    pages = discover_week_pages(anchors, site_url=site_url, week=week)
    if pages:
        return pages[0]
    return discover_week_anchor(anchors, site_url=site_url, week=week)


@dataclass(frozen=True)
class EvidenceLimits:
    """Bounds that keep the model input and GitHub usage predictable."""

    max_documents: int = 6
    max_document_chars: int = 4_000
    max_project_document_chars: int = 9_000
    max_site_chars: int = 2_500
    max_week_page_chars: int = 5_000
    max_commits: int = 15
    workers: int = 4


def week_directory(week: CourseWeek) -> str:
    return f"{WEEKLY_BUILDS_DIRECTORY}/week{week.number:02d}/"


def project_label(project_id: str, *, repository_prefix: str) -> str:
    """Human label derived only from the repository name, never from model output."""

    suffix = (
        project_id[len(repository_prefix) :] if project_id.startswith(repository_prefix) else ""
    )
    words = [part for part in re.split(r"[-_]+", suffix) if part]
    return " ".join(word.capitalize() for word in words) or project_id


def compact_text(value: str, *, limit: int) -> tuple[str, bool]:
    collapsed = _BLANK_LINES.sub("\n\n", _WHITESPACE.sub(" ", value.replace("\r\n", "\n"))).strip()
    if len(collapsed) <= limit:
        return collapsed, False
    return collapsed[:limit].rstrip(), True


def _document_priority(path: str) -> tuple[int, int, str]:
    name = path.rsplit("/", 1)[-1].casefold()
    return (path.count("/"), 0 if name.startswith("readme") else 1, path)


def _is_document(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.casefold().endswith(_DOCUMENT_SUFFIXES) and not name.startswith(".")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class WeeklyEvidenceCollector:
    """Read each course repository through the existing bounded GitHub catalog."""

    def __init__(
        self,
        catalog: StudentProjectCatalog,
        *,
        repository_prefix: str,
        read_site: SiteReader | None = None,
        find_week_page: WeekPageFinder | None = None,
        limits: EvidenceLimits | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._catalog = catalog
        self._repository_prefix = repository_prefix
        self._read_site = read_site
        self._find_week_page = find_week_page
        self._limits = limits or EvidenceLimits()
        self._log = log or (lambda message: None)

    def collect(self, week: CourseWeek) -> tuple[ProjectEvidence, ...]:
        projects = self._catalog.list_projects()
        self._log(f"Collecting week {week.number} evidence from {len(projects)} repositories.")
        with ThreadPoolExecutor(max_workers=max(1, self._limits.workers)) as pool:
            evidence = list(
                pool.map(lambda project: self._collect_project(project, week), projects)
            )
        return tuple(sorted(evidence, key=lambda item: (item.label.casefold(), item.project_id)))

    def _collect_project(self, project: StudentProject, week: CourseWeek) -> ProjectEvidence:
        notes: list[str] = []
        prefix = week_directory(week)
        document_paths: list[str] = []
        week_file_count = 0
        site_file_count = 0
        try:
            tree = self._catalog.inspect_repository(project.id, "tree")
            entries = tree.get("entries")
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, Mapping) or entry.get("type") != "blob":
                    continue
                path = entry.get("path")
                if not isinstance(path, str):
                    continue
                if any(part in _IGNORED_PATH_PARTS for part in path.split("/")):
                    continue
                if path.startswith(prefix) and not path.endswith(".gitkeep"):
                    week_file_count += 1
                    if _is_document(path):
                        document_paths.append(path)
                elif path.startswith(SITE_DIRECTORY):
                    site_file_count += 1
            if tree.get("truncated") is True:
                notes.append("repository tree was truncated; counts are lower bounds")
        except (StudentProjectNotFound, StudentProjectProviderError) as error:
            notes.append(f"repository tree unavailable: {error}")

        documents = self._read_documents(project.id, sorted(document_paths, key=_document_priority))
        commits, commit_count = self._read_commits(project.id, week, notes)
        site_text = self._read_site_text(project, notes)
        week_page_url, week_page_text = self._read_week_page(project, week, notes)
        self._log(
            f"  {project.id}: {week_file_count} week files, {len(documents)} documents, "
            f"{commit_count} commits in window"
        )
        return ProjectEvidence(
            project_id=project.id,
            label=project_label(project.id, repository_prefix=self._repository_prefix),
            site_url=project.site_url,
            week_file_count=week_file_count,
            site_file_count=site_file_count,
            commit_count=commit_count,
            commits=commits,
            documents=documents,
            site_text=site_text,
            week_page_url=week_page_url,
            week_page_text=week_page_text,
            notes=tuple(notes),
        )

    def _read_documents(self, project_id: str, paths: list[str]) -> tuple[ProjectDocument, ...]:
        documents: list[ProjectDocument] = []
        remaining = self._limits.max_project_document_chars
        for path in paths:
            if len(documents) >= self._limits.max_documents or remaining <= 0:
                break
            try:
                result = self._catalog.inspect_repository(project_id, "file", path=path)
            except (StudentProjectNotFound, StudentProjectProviderError):
                continue
            raw_text = result.get("text")
            if not isinstance(raw_text, str) or not raw_text.strip():
                continue
            text, truncated = compact_text(
                raw_text, limit=min(self._limits.max_document_chars, remaining)
            )
            remaining -= len(text)
            documents.append(
                ProjectDocument(
                    path=path,
                    text=text,
                    truncated=truncated or result.get("truncated") is True,
                )
            )
        return tuple(documents)

    def _read_commits(
        self, project_id: str, week: CourseWeek, notes: list[str]
    ) -> tuple[tuple[CommitSummary, ...], int]:
        try:
            result = self._catalog.inspect_repository(project_id, "commits")
        except (StudentProjectNotFound, StudentProjectProviderError) as error:
            notes.append(f"commit history unavailable: {error}")
            return (), 0
        raw_commits = result.get("commits")
        in_window: list[CommitSummary] = []
        for raw in raw_commits if isinstance(raw_commits, list) else []:
            if not isinstance(raw, Mapping):
                continue
            committed_at = _parse_timestamp(raw.get("date"))
            if committed_at is None or not (week.starts_at <= committed_at < week.ends_at):
                continue
            message = raw.get("message")
            subject = message.splitlines()[0] if isinstance(message, str) and message else ""
            in_window.append(CommitSummary(committed_at=committed_at, subject=subject[:200]))
        if isinstance(raw_commits, list) and len(raw_commits) >= 30 and in_window:
            notes.append("only the 30 most recent commits were inspected")
        return tuple(in_window[: self._limits.max_commits]), len(in_window)

    def _read_week_page(
        self, project: StudentProject, week: CourseWeek, notes: list[str]
    ) -> tuple[str | None, str | None]:
        if self._find_week_page is None or project.site_url is None:
            return None, None
        try:
            page_url = self._find_week_page(project.site_url, week)
        except Exception as error:  # discovery is best effort
            notes.append(f"week page discovery failed: {type(error).__name__}")
            return None, None
        if page_url is None:
            return None, None
        if page_url.split("#", 1)[0].rstrip("/") == project.site_url.rstrip("/"):
            return page_url, None  # a section of the root page; its text is site_text
        if self._read_site is None:
            return page_url, None
        try:
            page = self._read_site(page_url)
        except Exception as error:  # an unreadable post must not stop the digest
            notes.append(f"week page unreadable: {type(error).__name__}")
            return page_url, None
        text = page if isinstance(page, str) else page.get("text")
        if not isinstance(text, str) or not text.strip():
            return page_url, None
        compacted, _ = compact_text(text, limit=self._limits.max_week_page_chars)
        return page_url, compacted or None

    def _read_site_text(self, project: StudentProject, notes: list[str]) -> str | None:
        if self._read_site is None or project.site_url is None:
            return None
        try:
            page = self._read_site(project.site_url)
        except Exception as error:  # one unreadable site must not stop the digest
            notes.append(f"deployed site unreadable: {type(error).__name__}")
            return None
        text = page if isinstance(page, str) else page.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        compacted, _ = compact_text(text, limit=self._limits.max_site_chars)
        return compacted or None
