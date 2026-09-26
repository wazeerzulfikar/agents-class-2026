from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from PIL import Image
from pydantic import JsonValue

from agent_core import PrincipalContext
from course_server.agent import ToolExecutionContext, ToolValidationError
from course_server.auth import InMemoryAuthStore, UserAdminService
from course_server.config import ConfigurationError
from course_server.mail import InboundMail, OutboundMail, SentMail
from course_server.newsletter import (
    PIONEER_QUOTES,
    CourseWeek,
    EvidenceLimits,
    FileNewsletterStore,
    FoundImage,
    Highlight,
    HighlightImage,
    LectureNotes,
    NewsletterBranding,
    NewsletterCompositionError,
    NewsletterCopy,
    NewsletterIssue,
    NewsletterScheduleError,
    NewsletterService,
    NewsletterSettings,
    NewsletterStateError,
    NewsletterStoreError,
    PioneerQuote,
    ProjectDocument,
    ProjectEvidence,
    ProjectLink,
    ProjectScore,
    ScreenshotError,
    SiteScreenshot,
    WeeklyDigest,
    WeeklyEvidenceCollector,
    build_editorial_user_prompt,
    build_highlights_user_prompt,
    choose_quote,
    cid_image_source,
    compact_slides_text,
    compose_editorial,
    compose_highlights,
    compose_newsletter,
    encode_jpeg,
    learning_goals_from_syllabus,
    load_lecture_notes,
    parse_schedule,
    project_label,
    render_html,
    render_text,
    select_week,
    validate_editorial,
    verify_quote,
)
from course_server.newsletter.cli import main as newsletter_main
from course_server.newsletter.compose import EDITORIAL_MARKER, HIGHLIGHTS_MARKER
from course_server.newsletter.jobs import NewsletterJobRunner
from course_server.newsletter.score import SCORING_MARKER
from course_server.newsletter.tools import NewsletterTools
from course_server.student_projects import (
    RepositoryView,
    StudentProject,
    StudentProjectNotFound,
)

EASTERN = ZoneInfo("America/New_York")
SCHEDULE = """# Fall 2026 Schedule

| Date / Week | Lecture Topics (45 min) | Hands-on tutorial (50 min) |
| ----- | ----- | ----- |
| Week 1 (9/15) | **What is an AI agent?** *Prof. Pattie Maes* | Build a minimal agent loop. *WZ* |
| Week 2 (9/22) | **Tool use and agent interfaces.** *VD* | Build a tool-calling agent. |
| Week 3 (9/29) | No class | No class |
| Week 4 (10/6) | **Agent environments and evaluation** | Build an environment. |
| Week 14 TBD | **Final project presentations** | Final demos |
"""


def weeks() -> tuple[CourseWeek, ...]:
    return parse_schedule(SCHEDULE, timezone="America/New_York")


def test_schedule_parses_dated_weeks_windows_and_goals() -> None:
    parsed = weeks()

    assert [week.number for week in parsed] == [1, 2, 3, 4]
    week1 = parsed[0]
    assert week1.class_date == date(2026, 9, 15)
    assert week1.starts_at == datetime(2026, 9, 15, tzinfo=EASTERN)
    assert week1.ends_at == datetime(2026, 9, 22, tzinfo=EASTERN)
    assert week1.topic == "What is an AI agent?"
    assert week1.tutorial == "Build a minimal agent loop."
    assert parsed[1].topic == "Tool use and agent interfaces."
    assert parsed[2].has_class is False
    assert parsed[3].ends_at == datetime(2026, 10, 13, tzinfo=EASTERN)


def test_select_week_uses_the_last_class_week_whose_build_window_closed() -> None:
    parsed = weeks()

    assert select_week(parsed, as_of=date(2026, 9, 22)).number == 1
    assert select_week(parsed, as_of=date(2026, 9, 28)).number == 1
    assert select_week(parsed, as_of=date(2026, 9, 29)).number == 2
    # Week 3 has no class, so it is never the "previous week" even after its window closes.
    assert select_week(parsed, as_of=date(2026, 10, 6)).number == 2
    assert select_week(parsed, as_of=date(2026, 10, 13)).number == 4
    assert select_week(parsed, week_number=2, as_of=date(2026, 9, 16)).number == 2
    with pytest.raises(NewsletterScheduleError):
        select_week(parsed, as_of=date(2026, 9, 20))
    with pytest.raises(NewsletterScheduleError):
        select_week(parsed, week_number=9, as_of=date(2026, 12, 1))
    with pytest.raises(NewsletterScheduleError):
        parse_schedule("# Schedule without dated rows\n", timezone="America/New_York")


@dataclass
class FakeRepository:
    site_url: str | None
    tree: list[str]
    files: dict[str, str]
    commits: list[tuple[str, str]]
    broken: bool = False


@dataclass
class FakeCatalog:
    repositories: dict[str, FakeRepository]
    requests: list[tuple[str, str, str | None]] = field(default_factory=list)

    def list_projects(self) -> list[StudentProject]:
        return [
            StudentProject(project_id, repository.site_url)
            for project_id, repository in sorted(self.repositories.items())
        ]

    def inspect_repository(
        self,
        project_id: str,
        view: RepositoryView,
        *,
        path: str | None = None,
        ref: str | None = None,
    ) -> dict[str, JsonValue]:
        self.requests.append((project_id, view, path))
        repository = self.repositories[project_id]
        if repository.broken:
            raise StudentProjectNotFound("Student project data was not found.")
        if view == "tree":
            return {
                "ref": ref or "main",
                "entries": [{"path": item, "type": "blob", "size": 1} for item in repository.tree],
                "truncated": False,
            }
        if view == "file":
            assert path is not None
            if path not in repository.files:
                raise StudentProjectNotFound("missing")
            return {"ref": "main", "path": path, "text": repository.files[path], "truncated": False}
        if view == "commits":
            return {
                "ref": "main",
                "commits": [
                    {"sha": "abc", "message": message, "date": when, "author": "Student"}
                    for when, message in repository.commits
                ],
            }
        raise AssertionError(f"unexpected view {view}")


def fake_catalog() -> FakeCatalog:
    template = ["LICENSE", "README.md", "website/index.html", "weekly_builds/week01/.gitkeep"]
    return FakeCatalog(
        {
            "agents2026-ada": FakeRepository(
                site_url="https://mitmedialab.github.io/agents2026-ada/",
                tree=[
                    *template,
                    "weekly_builds/week01/README.md",
                    "weekly_builds/week01/agent.py",
                    "weekly_builds/week01/notes/PLAN.md",
                    "weekly_builds/week01/node_modules/pkg/README.md",
                    "weekly_builds/week02/README.md",
                    "website/style.css",
                ],
                files={
                    "weekly_builds/week01/README.md": "# Loop\n\nA   minimal   agent loop with  "
                    "a scratch-built planner that is described at some length here.",
                    "weekly_builds/week01/notes/PLAN.md": "Plan: observe, think, act.",
                },
                commits=[
                    ("2026-09-23T12:00:00Z", "Week 2 start"),
                    ("2026-09-21T23:30:00Z", "Week 1: agent loop\n\nDetails"),
                    ("2026-09-14T17:00:00Z", "Personalize repository README"),
                ],
            ),
            "agents2026-grace": FakeRepository(
                site_url="https://mitmedialab.github.io/agents2026-grace/",
                tree=template,
                files={},
                commits=[("2026-09-14T17:00:00Z", "Initial commit")],
            ),
            "agents2026-hal-9000": FakeRepository(
                site_url=None,
                tree=[*template, "weekly_builds/week01/README.md"],
                files={"weekly_builds/week01/README.md": "Hal's loop."},
                commits=[("2026-09-20T12:00:00Z", "week 1")],
            ),
            "agents2026-zed": FakeRepository(
                site_url="https://mitmedialab.github.io/agents2026-zed/",
                tree=template,
                files={},
                commits=[],
                broken=True,
            ),
        }
    )


def read_site(url: str) -> str | dict[str, object]:
    if url.endswith("week01.html"):
        return {"url": url, "text": "Post: I built a loop that   renames files.", "images": []}
    return {"url": url, "text": f"Welcome to {url}\n\n\n\nWeek 1 write-up", "images": []}


def find_week_page_for_tests(site_url: str, week: CourseWeek) -> str | None:
    if "ada" in site_url:
        return f"{site_url}week01.html"
    if "grace" in site_url:
        return f"{site_url}#week-01"
    if "zed" in site_url:
        raise RuntimeError("discovery exploded")
    return None


def test_collector_gathers_bounded_week_evidence_without_stopping_on_failures() -> None:
    catalog = fake_catalog()
    collector = WeeklyEvidenceCollector(
        catalog,
        repository_prefix="agents2026-",
        read_site=read_site,
        find_week_page=find_week_page_for_tests,
        limits=EvidenceLimits(max_document_chars=60, workers=1),
    )

    evidence = collector.collect(weeks()[0])

    assert [item.label for item in evidence] == ["Ada", "Grace", "Hal 9000", "Zed"]
    ada, grace, hal, zed = evidence
    assert ada.active and ada.week_file_count == 3 and ada.site_file_count == 2
    assert [document.path for document in ada.documents] == [
        "weekly_builds/week01/README.md",
        "weekly_builds/week01/notes/PLAN.md",
    ]
    assert ada.documents[0].truncated is True
    assert "   " not in ada.documents[0].text
    assert ada.commit_count == 1
    assert ada.commits[0].subject == "Week 1: agent loop"
    assert (
        ada.site_text
        == "Welcome to https://mitmedialab.github.io/agents2026-ada/\n\nWeek 1 write-up"
    )
    assert ada.week_page_url == "https://mitmedialab.github.io/agents2026-ada/week01.html"
    assert ada.week_page_text == "Post: I built a loop that renames files."
    assert grace.week_page_url == "https://mitmedialab.github.io/agents2026-grace/#week-01"
    assert grace.week_page_text is None  # a section of the root page, already in site_text
    assert hal.week_page_url is None
    assert any("discovery failed" in note for note in zed.notes)
    assert grace.active is False and grace.documents == () and grace.commit_count == 0
    assert hal.active and hal.site_url is None and hal.site_text is None
    assert zed.active is False
    assert any("unavailable" in note for note in zed.notes)
    assert all(
        request[2] is None or "node_modules" not in request[2] for request in catalog.requests
    )
    assert project_label("agents2026-juan-ruben", repository_prefix="agents2026-") == "Juan Ruben"
    assert project_label("other", repository_prefix="agents2026-") == "other"


def evidence(project_id: str, label: str, *, active: bool, site: bool = True) -> ProjectEvidence:
    return ProjectEvidence(
        project_id=project_id,
        label=label,
        site_url=f"https://example.edu/{project_id}/" if site else None,
        week_file_count=2 if active else 0,
        site_file_count=1,
        commit_count=1 if active else 0,
        documents=(),
    )


def digest_for(week: CourseWeek, *, cooldown: frozenset[str] = frozenset()) -> WeeklyDigest:
    return WeeklyDigest(
        week=week,
        projects=(
            evidence("agents2026-ada", "Ada", active=True),
            evidence("agents2026-grace", "Grace", active=True),
            evidence("agents2026-hal", "Hal", active=True, site=False),
            evidence("agents2026-idle", "Idle", active=False),
        ),
        cooldown_project_ids=cooldown,
        highlight_count=2,
    )


def copy_json(*project_ids: str, extra: str = "") -> str:
    return json.dumps(
        {
            "highlights": [
                {
                    "project_id": project_id,
                    "headline": f"{project_id} gets loopy",
                    "description": (
                        f"A tiny loop made by {project_id.rsplit('-', 1)[-1]}. It looks, then "
                        f"acts, then stops when the job is done for "
                        f"{project_id.rsplit('-', 1)[-1]}." + extra
                    ),
                }
                for project_id in project_ids
            ],
        }
    )


EDITORIAL = " ".join(["The loop ran and it worked well."] * 16)
DENSE = (
    "Notwithstanding the aforementioned considerations, the heterogeneous submissions "
    "demonstrated extraordinarily sophisticated architectural instrumentation, particularly "
    "regarding observational fidelity; consequently, evaluation methodologies necessitate "
    "comprehensive reconsideration across every conceivable dimension of implementation. "
) * 3


def editorial_json(
    headline: str = "Loop, There It Is", editorial: str = EDITORIAL, quote_choice: int = 0
) -> str:
    return json.dumps({"headline": headline, "editorial": editorial, "quote_choice": quote_choice})


def score_json(goal_fit: int, interest: int, execution: int, rationale: str = "ok") -> str:
    return json.dumps(
        {
            "interest": interest,
            "execution": execution,
            "goal_fit": goal_fit,
            "rationale": rationale,
            "built": "A tidy agent loop.",
            "went_well": "Clear loop.",
            "struggled": "Thin docs.",
            "quotes": [],
        }
    )


@dataclass
class ScriptedWriter:
    """Highlight and editorial responses are consumed in order; scores by project id."""

    responses: list[str]
    scores: dict[str, str] = field(default_factory=dict)
    editorials: list[str] = field(default_factory=lambda: [editorial_json()])
    prompts: list[str] = field(default_factory=list)
    editorial_prompts: list[str] = field(default_factory=list)
    score_prompts: list[str] = field(default_factory=list)
    judge_prompts: list[str] = field(default_factory=list)
    judge_choice: int = 1

    def write(self, *, system_prompt: str, user_prompt: str, schema: dict[str, object]) -> str:
        assert schema["type"] == "object"
        if system_prompt.startswith(SCORING_MARKER):
            self.score_prompts.append(user_prompt)
            project_id = user_prompt.split("Project id: ", 1)[1].split("\n", 1)[0]
            return self.scores.get(project_id, score_json(6, 6, 6))
        if system_prompt.startswith(EDITORIAL_MARKER):
            self.editorial_prompts.append(user_prompt)
            return self.editorials.pop(0) if len(self.editorials) > 1 else self.editorials[0]
        assert system_prompt.startswith(HIGHLIGHTS_MARKER)
        self.prompts.append(user_prompt)
        # The last scripted response is repeated so rule-breaking scripts exhaust every retry.
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def judge_images(self, *, prompt: str, images: Sequence[bytes]) -> int:
        self.judge_prompts.append(prompt)
        return self.judge_choice


def test_compose_writes_only_the_selected_projects_in_order_and_reprompts_once() -> None:
    week = weeks()[0]
    digest = digest_for(week, cooldown=frozenset({"agents2026-hal"}))
    selected = ("agents2026-ada", "agents2026-grace")
    branding = NewsletterBranding()
    writer = ScriptedWriter(
        [
            copy_json("agents2026-grace", "agents2026-ada", "agents2026-ada"),
            copy_json("agents2026-ada", "agents2026-grace"),
        ]
    )

    highlights = compose_highlights(digest, writer, selected=selected, branding=branding)

    assert [item.project_id for item in highlights] == list(selected)
    assert len(writer.prompts) == 2
    assert "Featured projects, in order: agents2026-ada, agents2026-grace" in writer.prompts[0]
    assert "FEATURED #1" in writer.prompts[0] and "agents2026-idle" not in writer.prompts[0]
    assert "assignment: Build a minimal agent loop.\n" in writer.prompts[0]
    assert "rejected" in writer.prompts[1]
    assert "exactly these project ids in this order" in writer.prompts[1]

    with pytest.raises(NewsletterCompositionError, match="broke platform rules"):
        compose_highlights(
            digest,
            ScriptedWriter([copy_json("agents2026-idle"), copy_json("agents2026-idle")]),
            selected=selected,
            branding=branding,
        )
    with pytest.raises(NewsletterCompositionError, match="invalid JSON"):
        compose_highlights(
            digest, ScriptedWriter(["not json"]), selected=selected, branding=branding
        )
    with pytest.raises(NewsletterCompositionError, match="must not contain links"):
        compose_highlights(
            digest,
            ScriptedWriter(
                [
                    copy_json(*selected, extra=" See https://x.y"),
                    copy_json(*selected, extra=" Mail me@x.edu"),
                ]
            ),
            selected=selected,
            branding=branding,
        )
    with pytest.raises(NewsletterCompositionError, match='must not use the word "brief"'):
        compose_highlights(
            digest,
            ScriptedWriter(
                [
                    copy_json(*selected, extra=" Fits the brief."),
                    copy_json(*selected, extra=" The brief."),
                ]
            ),
            selected=selected,
            branding=branding,
        )
    wordy = " ".join(["word"] * 60)
    with pytest.raises(NewsletterCompositionError, match=r"\d+ words; the limit is 48"):
        compose_highlights(
            digest,
            ScriptedWriter(
                [
                    copy_json(*selected).replace("A tiny loop", wordy),
                    copy_json(*selected).replace("A tiny loop", wordy),
                ]
            ),
            selected=selected,
            branding=branding,
        )
    with pytest.raises(NewsletterCompositionError, match="No project was selected"):
        compose_highlights(digest, ScriptedWriter([]), selected=(), branding=branding)
    dense = copy_json(*selected).replace(
        "A tiny loop made by",
        "Notwithstanding heterogeneous architectural instrumentation, the STALLED_SHORT "
        "outcome taxonomy substantiates a sophisticated observe_act loop by",
    )
    with pytest.raises(NewsletterCompositionError, match=r"reading ease|internal name"):
        compose_highlights(
            digest, ScriptedWriter([dense, dense]), selected=selected, branding=branding
        )
    same = copy_json(*selected).replace("for grace.", "for ada.")
    repeated = ScriptedWriter([same, same])
    with pytest.raises(NewsletterCompositionError, match="repeats a sentence from agents2026-ada"):
        compose_highlights(digest, repeated, selected=selected, branding=branding)
    assert "This week's assignment: Build a minimal agent loop." in build_highlights_user_prompt(
        digest, selected=selected
    )


def test_compose_editorial_is_anonymous_short_constructive_and_link_checked() -> None:
    week = weeks()[0]
    digest = digest_for(week)
    branding = NewsletterBranding()
    scores = (
        ProjectScore(
            project_id="agents2026-ada",
            interest=8,
            execution=7,
            goal_fit=9,
            total=8.2,
            built="Ada built a loop.",
            went_well="Clean loop.",
            struggled="Sparse tests.",
            quotes=("Agents learn best when reality gets a vote.", "My loop ate my homework."),
        ),
        ProjectScore(project_id="agents2026-grace", interest=5, execution=5, goal_fit=6, total=5.4),
    )
    lecture = LectureNotes(
        title="Week 1 slides",
        slides_text="What is an agent? Senses, acts, maintains state, learns.",
        learning_goals="1. Build functional AI agents.",
    )

    def link_checker(url: str) -> bool:
        return url.startswith("https://good.example/")

    writer = ScriptedWriter([], editorials=[editorial_json("Loop, There It Is", quote_choice=1)])

    draft = compose_editorial(
        digest, scores, writer, branding=branding, lecture=lecture, link_checker=link_checker
    )

    assert (draft.headline, draft.editorial, draft.quote_choice) == (
        "Loop, There It Is",
        EDITORIAL,
        1,
    )
    prompt = writer.editorial_prompts[0]
    assert "Submission 1: built: Ada built a loop. / went well: Clean loop." in prompt
    assert "students;" not in prompt and "Nothing posted" not in prompt
    assert '1. "Agents learn best when reality gets a vote." \u2014 Ada' in prompt
    assert '2. "My loop ate my homework." \u2014 Ada' in prompt
    assert "## What was taught this week: Week 1 slides\nWhat is an agent?" in prompt
    assert "## Course learning goals (syllabus)\n1. Build functional AI agents." in prompt
    assert prompt == build_editorial_user_prompt(digest, scores, lecture=lecture)
    assert "No slide deck is published" in build_editorial_user_prompt(digest, scores)

    # Names, participation counts, long copy, and dead links are all rejected with feedback.
    names = ["Ada", "Grace", "Hal", "Idle"]
    assert validate_editorial("Fine", EDITORIAL, names=names) == ()
    assert any(
        'name students (found "Grace")' in item
        for item in validate_editorial("Fine", EDITORIAL + " Grace shone.", names=names)
    )
    assert any(
        "count participation" in item
        for item in validate_editorial("Fine", "Twenty-seven of you posted. " + EDITORIAL)
    )
    assert any(
        "count participation" in item
        for item in validate_editorial("Fine", EDITORIAL + " 12 students struggled.")
    )
    dense_problems = validate_editorial("Fine", DENSE)
    assert any("sentences average" in item for item in dense_problems)
    assert any("longest sentence" in item for item in dense_problems)
    assert any("reading ease" in item for item in dense_problems)
    assert any("no semicolons" in item for item in dense_problems)
    borrowed = EDITORIAL + " Do not become a horoscope in a trench coat."
    assert any(
        'reuse wording from a candidate quote (found "a horoscope in a trench")' in item
        for item in validate_editorial(
            "Fine", borrowed, quotes=["Otherwise it's a horoscope in a trench coat."]
        )
    )
    assert validate_editorial("Fine", EDITORIAL, quotes=["Otherwise it's a horoscope."]) == ()
    assert any(
        "write between 100 and 165" in item
        for item in validate_editorial("Fine", " ".join(["word"] * 200))
    )
    linked = EDITORIAL + " See [the ReAct paper](https://good.example/react) for more."
    assert validate_editorial("Fine", linked, link_checker=link_checker) == ()
    dead = EDITORIAL + " See [notes](https://bad.example/gone)."
    assert any(
        "does not resolve" in item
        for item in validate_editorial("Fine", dead, link_checker=link_checker)
    )
    many = EDITORIAL + (
        " [a](https://good.example/1) [b](https://good.example/2) [c](https://good.example/3)"
    )
    assert any(
        "3 reference links" in item
        for item in validate_editorial("Fine", many, link_checker=link_checker)
    )
    # Links to students' own sites are neither counted nor fetched.
    sites = ["https://a.example/", "https://g.example/"]
    site_linked = EDITORIAL + (
        " [a rolling-ball world](https://a.example/) and [a renamer](https://g.example)"
        " and [a maze](https://a.example/) plus [one paper](https://good.example/paper)."
    )
    assert (
        validate_editorial(
            "Fine", site_linked, project_urls=sites, link_checker=lambda url: "good" in url
        )
        == ()
    )
    assert "site: https://example.edu/agents2026-ada/" in prompt
    assert any(
        "must not contain links" in item
        for item in validate_editorial("Fine", EDITORIAL + " See https://bare.example/")
    )

    short = ScriptedWriter(
        [], editorials=[editorial_json(editorial="Too short."), editorial_json()]
    )
    retried = compose_editorial(digest, scores, short, branding=branding, link_checker=None)
    assert retried.editorial == EDITORIAL
    assert "editorial has 2 words; write between 100 and 165" in short.editorial_prompts[1]
    with pytest.raises(NewsletterCompositionError, match="editorial broke platform rules"):
        compose_editorial(
            digest,
            scores,
            ScriptedWriter([], editorials=[editorial_json(quote_choice=5)]),
            branding=branding,
            link_checker=None,
        )

    copy, chosen = compose_newsletter(
        digest,
        scores,
        ScriptedWriter([copy_json("agents2026-ada")], editorials=[editorial_json(quote_choice=2)]),
        selected=("agents2026-ada",),
        branding=branding,
        link_checker=None,
    )
    assert copy.headline == "Loop, There It Is" and len(copy.highlights) == 1
    assert chosen == ("agents2026-ada", "Ada", "My loop ate my homework.")


def test_lecture_notes_come_from_the_week_deck_and_syllabus_goals(tmp_path: Path) -> None:
    slides = tmp_path / "slides" / "week-01"
    slides.mkdir(parents=True)
    (slides / "resource.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "resource": {
                    "uri": "course://slides/week-01",
                    "title": "Agents 101",
                    "media_type": "text/markdown",
                    "file": "deck.md",
                },
            }
        )
    )
    (slides / "deck.md").write_text(
        "--- Page 1 ---\nWhat   is an agent?\n\n\n\n--- Page 2 ---\nSenses, acts, learns.\n"
    )
    syllabus = tmp_path / "syllabus.md"
    syllabus.write_text(
        "# Course\n\n## **Learning Goals**\n\n1. Build agents.\n2. Evaluate them.\n\n"
        "## Structure\n\nWeekly.\n"
    )

    notes = load_lecture_notes(tmp_path / "slides", syllabus, weeks()[0])

    assert notes is not None and notes.title == "Agents 101" and notes.truncated is False
    assert notes.slides_text == "What is an agent?\n\nSenses, acts, learns."
    assert notes.learning_goals == "1. Build agents.\n2. Evaluate them."
    assert load_lecture_notes(tmp_path / "slides", syllabus, weeks()[1]) is None
    assert compact_slides_text("a " * 50, limit=20) == ("a a a a a a a a a a", True)
    assert learning_goals_from_syllabus("# No goals here\n") == ""


def test_verify_quote_accepts_only_verbatim_student_prose() -> None:
    project = ProjectEvidence(
        project_id="agents2026-ada",
        label="Ada",
        week_file_count=1,
        site_file_count=1,
        commit_count=1,
        documents=(
            ProjectDocument(
                path="weekly_builds/week01/README.md",
                text="# Loop\n\nI learned that agents   learn best when\nreality gets a vote.",
            ),
        ),
        site_text="Welcome to my site, where \u201cthe loop is the lesson\u201d every week.",
    )
    assert (
        verify_quote("Agents learn best when reality gets a vote.", project)
        == "Agents learn best when reality gets a vote."
    )
    assert verify_quote("\u201cthe loop is the lesson\u201d every week.", project) != ""
    assert verify_quote("Agents learn best when reality votes.", project) == ""
    spiral = ProjectEvidence(
        project_id="agents2026-egemen",
        label="Egemen",
        week_file_count=1,
        site_file_count=1,
        commit_count=1,
        documents=(
            ProjectDocument(
                path="weekly_builds/week01/README.md",
                text=(
                    "## Why\n\nThe agent has to earn its calm with visible reasoning steps. "
                    "Otherwise it's a horoscope in a trench coat. "
                    "Even a happy prompt gets spiraled."
                ),
            ),
        ),
    )
    assert verify_quote("Otherwise it's a horoscope in a trench coat.", spiral) == (
        "The agent has to earn its calm with visible reasoning steps. "
        "Otherwise it's a horoscope in a trench coat."
    )
    assert verify_quote("Even a happy prompt gets spiraled.", spiral) == (
        "Even a happy prompt gets spiraled."
    )
    heading_only = spiral.model_copy(
        update={
            "documents": (
                ProjectDocument(
                    path="weekly_builds/week01/README.md",
                    text="## Why\n\nOtherwise it's a horoscope in a trench coat.",
                ),
            )
        }
    )
    assert verify_quote("Otherwise it's a horoscope in a trench coat.", heading_only) == (
        "Otherwise it's a horoscope in a trench coat."
    )
    assert verify_quote("Short quote here.", project) == ""
    assert verify_quote(" ".join(["word"] * 41), project) == ""


def sample_issue(*, status: str = "draft") -> NewsletterIssue:
    week = weeks()[0]
    return NewsletterIssue(
        issue_id="2026-week01",
        week=week,
        branding=NewsletterBranding(),
        subject="The Class Runtime from MAS.S60",
        body=NewsletterCopy(
            headline="Week one is in the loop.",
            editorial=(
                "The loop ran.\n\nSome looped twice; see "
                "[the ReAct paper](https://arxiv.org/abs/2210.03629?x=1&y=2) for why."
            ),
            highlights=(
                Highlight(
                    project_id="agents2026-ada",
                    headline="Ada <script>alert(1)</script> loops",
                    description=(
                        "A minimal agent loop built from scratch. It is the loop asked for."
                    ),
                ),
            ),
        ),
        roster=(
            ProjectLink(
                project_id="agents2026-ada",
                label="Ada",
                site_url="https://a.example/",
                post_url="https://a.example/week01.html",
            ),
            ProjectLink(
                project_id="agents2026-grace", label="Grace", site_url="https://g.example/?x=1&y=2"
            ),
            ProjectLink(project_id="agents2026-hal", label="Hal", site_url=None, posted=False),
            ProjectLink(project_id="agents2026-ivy", label="Ivy", site_url=None),
        ),
        quote=PioneerQuote(
            text="Agents learn best when reality gets a vote.",
            author="Ada",
            source="from their week 1 post",
            url="https://a.example/",
            kind="student",
        ),
        images=(
            HighlightImage(
                project_id="agents2026-ada",
                filename="agents2026-ada.jpg",
                media_type="image/jpeg",
                width=1200,
                height=750,
                kind="post_image",
                source_url="https://a.example/assets/hero.webp",
                page_url="https://a.example/week01.html",
            ),
        ),
        scores=(
            ProjectScore(
                project_id="agents2026-ada", interest=8, execution=7, goal_fit=9, total=8.2
            ),
            ProjectScore(
                project_id="agents2026-grace",
                interest=5,
                execution=5,
                goal_fit=6,
                total=5.4,
                built="Grace built a tiny tool-calling loop.",
            ),
        ),
        model_id="test-model",
        status="sent" if status == "sent" else "draft",
        created_at=datetime(2026, 9, 22, 13, 0, tzinfo=UTC),
        sent_at=datetime(2026, 9, 22, 14, 0, tzinfo=UTC) if status == "sent" else None,
    )


def test_render_text_and_html_carry_links_lists_quote_footer_and_escaping() -> None:
    issue = sample_issue()

    text = render_text(issue)
    assert text.startswith("THE CLASS RUNTIME · ISSUE 01\nWeek one is in the loop.")
    assert (
        "Week 1 · Sep 15 \u2013 Sep 21, 2026\nTHE ASSIGNMENT: Build a minimal agent loop." in text
    )
    assert "MAS.S60 · AI Agents for Cognitive Augmentation · MIT, Fall 2026" in text
    assert "1. Ada <script>alert(1)</script> loops — Ada" in text
    assert "   A minimal agent loop built from scratch. It is the loop asked for." in text
    assert "THE ASSIGNMENT: Build a minimal agent loop.\n\nThe loop ran." in text
    assert "brief" not in text.casefold() and "scroll" not in text.casefold()
    assert "\n\n— The Course Agent\n\n" in text
    assert (
        "Curation and commentary by The Course Agent.\nReviewed by The MAS.S60 teaching team."
        in text
    )
    assert "Open it: https://a.example/week01.html" in text
    assert (
        "ALL THE OTHER BUILDS THIS WEEK\n- Grace: Grace built a tiny tool-calling loop. "
        "https://g.example/?x=1&y=2\n- Ivy: Nothing posted for this week yet."
    ) in text
    assert "Hal" not in text
    assert "Ada — https://a.example/" not in text.split("ALL THE OTHER BUILDS")[1]
    assert '"Agents learn best when reality gets a vote."' in text
    assert "— Ada, from their week 1 post (https://a.example/)" in text
    assert "the ReAct paper (https://arxiv.org/abs/2210.03629?x=1&y=2)" in text
    assert "Class website: https://cognitive-agents.media.mit.edu" in text

    html = render_html(issue)
    assert "<script>" not in html
    assert "Ada &lt;script&gt;alert(1)&lt;/script&gt; loops" in html
    assert "<h1" in html and "Week one is in the loop." in html
    assert "How the week went</p>" in html
    assert "&mdash; The Course Agent</p>" in html
    assert "Curation and commentary by The Course Agent</p>" in html
    assert "Reviewed by The MAS.S60 teaching team</p>" in html and "Sent by" not in html
    assert html.count('bgcolor="#000000"') >= 4 and "supported-color-schemes" in html
    assert html.count("The loop ran.</p>") == 1
    assert (
        '<a href="https://arxiv.org/abs/2210.03629?x=1&amp;y=2"' in html
        and ">the ReAct paper</a> for why.</p>" in html
    )
    assert "Grace built a tiny tool-calling loop." in html
    assert "Nothing posted for this week yet." in html
    assert "The assignment</p>" in html and ">Build a minimal agent loop.</p>" in html
    assert "Week 1 · Sep 15 \u2013 Sep 21, 2026" in html
    assert "Brief" not in html and "Keep scrolling" not in html
    assert 'src="2026-week01/agents2026-ada.jpg"' in html
    assert 'alt="Ada &lt;script&gt;alert(1)&lt;/script&gt; loops"' in html
    assert 'src="cid:agents2026-ada"' in render_html(issue, image_src=cid_image_source)
    assert '<a href="https://a.example/week01.html"' in html
    assert 'href="https://g.example/?x=1&amp;y=2"' in html
    assert ">Ivy</span>" in html and "Hal" not in html
    assert 'href="https://cognitive-agents.media.mit.edu"' in html
    assert "Alan Turing" not in html and "<title>The Class Runtime from MAS.S60</title>" in html
    assert "&ldquo;Agents learn best when reality gets a vote.&rdquo;" in html
    assert html.count("linear-gradient(#000000,#000000)") == 2
    assert '<a href="https://a.example/"' in html and "Ada, from their week 1 post</a>" in html
    assert "background-color:#000000" in html and "#f5f5f2" in html
    # Section labels are sentence-case sans, never tracked-out monospace, italic, or webfonts.
    assert 'font-weight:600;color:#f5f5f2;">How the week went</p>' in html
    assert "monospace" not in html and "uppercase" not in html and "italic" not in html
    assert "fonts.googleapis.com" not in html
    # Secondary text is near-white: the site's faint grey never reaches the email.
    assert "#8b8b86" not in html and "#c9c9c4" not in html and "#f0f0ec" in html
    # Gmail-only blend wrappers keep text light under its dark mode; images stay outside them.
    assert 'class="body"' in html and "u + .body .gmail-blend-difference{" in html
    segments = html.split('<div class="gmail-blend-screen"><div class="gmail-blend-difference">')
    assert len(segments) == 3 + len(issue.body.highlights)
    for segment in segments[1:]:
        assert "<img" not in segment.split("</div></div>", 1)[0]
    assert "\u2019" not in html and "\u201c" not in html


def test_store_round_trips_issues_and_derives_cooldown_and_used_quotes(tmp_path: Path) -> None:
    store = FileNewsletterStore(tmp_path / "newsletter")
    week1 = sample_issue(status="sent")
    week2 = week1.model_copy(
        update={
            "issue_id": "2026-week02",
            "week": weeks()[1],
            "body": week1.body.model_copy(
                update={
                    "highlights": (
                        week1.body.highlights[0].model_copy(
                            update={"project_id": "agents2026-grace"}
                        ),
                    )
                }
            ),
            "quote": PIONEER_QUOTES[1],
        }
    )
    week4_draft = week1.model_copy(
        update={"issue_id": "2026-week04", "week": weeks()[3], "status": "draft", "sent_at": None}
    )
    for issue in (week2, week1, week4_draft):
        store.save(issue)

    assert store.load("2026-week01") == week1
    assert [issue.issue_id for issue in store.list_issues()] == [
        "2026-week01",
        "2026-week02",
        "2026-week04",
    ]
    assert (
        (tmp_path / "newsletter/issues/2026-week01.txt").read_text().startswith("THE CLASS RUNTIME")
    )
    assert (
        (tmp_path / "newsletter/issues/2026-week01.html").read_text().startswith("<!DOCTYPE html>")
    )
    assert store.recently_highlighted(before_week=3, cooldown=1) == {"agents2026-grace"}
    assert store.recently_highlighted(before_week=3, cooldown=2) == {
        "agents2026-ada",
        "agents2026-grace",
    }
    assert store.recently_highlighted(before_week=2, cooldown=2) == {"agents2026-ada"}
    assert store.recently_highlighted(before_week=5, cooldown=0) == frozenset()
    assert store.used_quote_texts() == {
        "Agents learn best when reality gets a vote.",
        PIONEER_QUOTES[1].text,
    }
    assert store.load("2026-week09") is None
    with pytest.raises(NewsletterStoreError):
        store.load("../etc/passwd")

    saved = store.save_image("2026-week01", "agents2026-ada.jpg", b"\xff\xd8jpeg")
    assert saved == tmp_path / "newsletter/issues/2026-week01/agents2026-ada.jpg"
    assert store.load_image("2026-week01", "agents2026-ada.jpg") == b"\xff\xd8jpeg"
    assert (
        'src="2026-week01/agents2026-ada.jpg"'
        in (tmp_path / "newsletter/issues/2026-week01.html").read_text()
    )
    with pytest.raises(NewsletterStoreError):
        store.load_image("2026-week01", "missing.jpg")
    with pytest.raises(NewsletterStoreError):
        store.image_path("2026-week01", "../secret.jpg")
    with pytest.raises(NewsletterStoreError):
        store.image_path("2026-week01", ".hidden")


def test_store_places_an_email_sized_logo_and_the_email_inlines_it(tmp_path: Path) -> None:
    logo_source = tmp_path / "wordmark.png"
    Image.new("RGBA", (2000, 600), (255, 255, 255, 128)).save(logo_source, format="PNG")
    store = FileNewsletterStore(tmp_path / "newsletter", logo_path=logo_source)
    store.save_image("2026-week01", "agents2026-ada.jpg", b"\xff\xd8jpeg")

    store.save(sample_issue())

    placed = tmp_path / "newsletter/issues/2026-week01/newsletter-logo.png"
    assert placed.is_file()
    with Image.open(placed) as image:
        assert image.size == (1000, 300) and image.mode == "RGBA"
    html = (tmp_path / "newsletter/issues/2026-week01.html").read_text()
    assert 'src="2026-week01/newsletter-logo.png"' in html and 'alt="The Class Runtime"' in html
    assert "The Class Runtime &middot; Issue" not in html and ">Issue 01</p>" in html
    assert store.logo_bytes("2026-week01") == placed.read_bytes()

    async def scenario() -> None:
        service = NewsletterService(settings=NewsletterSettings(), weeks=weeks(), store=store)
        adapter = RecordingMailAdapter()
        preview = await service.send(
            "2026-week01", mail=adapter, recipients=["me@mit.edu"], test_only=True
        )
        assert preview.status == "draft"
        logo = [
            image
            for image in adapter.sent[0].inline_images
            if image.content_id == "newsletter-logo"
        ]
        assert len(logo) == 1 and logo[0].filename == "newsletter-logo.png"
        assert (
            adapter.sent[0].html is not None and 'src="cid:newsletter-logo"' in adapter.sent[0].html
        )

    asyncio.run(scenario())

    store.clear_images("2026-week01")
    assert placed.is_file()  # the logo survives image refreshes

    plain_store = FileNewsletterStore(tmp_path / "plain")
    plain_store.save(sample_issue())
    plain_html = (tmp_path / "plain/issues/2026-week01.html").read_text()
    assert (
        "newsletter-logo" not in plain_html and "The Class Runtime &middot; Issue 01" in plain_html
    )


def test_encode_jpeg_downscales_captures_to_email_width() -> None:
    import io

    buffer = io.BytesIO()
    Image.new("RGBA", (2400, 1500), (10, 20, 30, 255)).save(buffer, format="PNG")

    shot = encode_jpeg(buffer.getvalue(), max_width=1200, quality=80)

    assert (shot.width, shot.height, shot.media_type) == (1200, 750, "image/jpeg")
    assert shot.data.startswith(b"\xff\xd8")
    small = io.BytesIO()
    Image.new("RGB", (300, 200)).save(small, format="PNG")
    assert encode_jpeg(small.getvalue(), max_width=1200, quality=80).width == 300


def test_quotes_rotate_by_week_and_skip_quotes_already_sent() -> None:
    assert choose_quote(week_number=1, used_texts=()) == PIONEER_QUOTES[0]
    assert choose_quote(week_number=2, used_texts=()) == PIONEER_QUOTES[1]
    assert choose_quote(week_number=1, used_texts={PIONEER_QUOTES[0].text}) == PIONEER_QUOTES[1]
    assert choose_quote(week_number=len(PIONEER_QUOTES) + 1, used_texts=()) == PIONEER_QUOTES[0]
    everything = {quote.text for quote in PIONEER_QUOTES}
    assert choose_quote(week_number=3, used_texts=everything) == PIONEER_QUOTES[2]
    assert len(everything) == len(PIONEER_QUOTES)


@dataclass
class RecordingMailAdapter:
    sent: list[OutboundMail] = field(default_factory=list)
    fail_for: set[str] = field(default_factory=set)

    async def send_message(self, message: OutboundMail) -> SentMail:
        if message.to[0] in self.fail_for:
            raise RuntimeError("provider rejected the message")
        self.sent.append(message)
        return SentMail(
            provider_message_id=f"p-{len(self.sent)}", internet_message_id=f"<m{len(self.sent)}@x>"
        )

    async def reply_to_message(
        self, original: object, *, text: str, headers: dict[str, str] | None = None
    ) -> SentMail:
        raise AssertionError("not used")

    async def reply_to_sent_message(
        self,
        original: SentMail,
        *,
        to: tuple[str, ...],
        subject: str,
        text: str,
        headers: dict[str, str] | None = None,
    ) -> SentMail:
        raise AssertionError("not used")

    async def fetch_new_messages(self, *, since: datetime) -> list[InboundMail]:
        return []


@dataclass
class FakeImageFinder:
    calls: list[tuple[str, int, str]] = field(default_factory=list)
    fail_for: set[str] = field(default_factory=set)

    def find(self, *, site_url: str, week: CourseWeek, context: str) -> FoundImage | None:
        self.calls.append((site_url, week.number, context))
        if site_url in self.fail_for:
            raise ScreenshotError("Site inspection failed (Timeout).")
        return FoundImage(
            shot=SiteScreenshot(
                data=b"\xff\xd8jpeg", media_type="image/jpeg", width=1200, height=750
            ),
            kind="post_image",
            page_url=f"{site_url}week01.html",
            source_url=f"{site_url}assets/hero.webp",
        )


def make_service(
    tmp_path: Path, writer: ScriptedWriter, *, cooldown: int = 2
) -> tuple[NewsletterService, FileNewsletterStore]:
    store = FileNewsletterStore(tmp_path / "newsletter")
    settings = NewsletterSettings(highlight_count=2, highlight_cooldown_issues=cooldown)
    service = NewsletterService(
        settings=settings,
        weeks=weeks(),
        store=store,
        collector=WeeklyEvidenceCollector(
            fake_catalog(),
            repository_prefix="agents2026-",
            read_site=read_site,
            limits=EvidenceLimits(workers=1),
        ),
        writer=writer,
        image_finder=FakeImageFinder(),
        link_checker=None,
        lecture_loader=lambda week: None,
        model_id="test-model",
        clock=lambda: datetime(2026, 9, 22, 15, 0, tzinfo=UTC),
    )
    return service, store


def test_service_drafts_for_review_and_sends_only_on_explicit_approval(tmp_path: Path) -> None:
    writer = ScriptedWriter(
        [copy_json("agents2026-ada", "agents2026-hal-9000")],
        scores={
            "agents2026-ada": score_json(goal_fit=9, interest=8, execution=7, rationale="Bold."),
            "agents2026-hal-9000": score_json(goal_fit=7, interest=6, execution=6),
        },
    )
    service, store = make_service(tmp_path, writer)

    issue = service.draft(as_of=date(2026, 9, 22))

    assert issue.issue_id == "2026-week01" and issue.status == "draft"
    assert issue.highlighted_project_ids() == ("agents2026-ada", "agents2026-hal-9000")
    assert [score.project_id for score in issue.scores] == [
        "agents2026-ada",
        "agents2026-hal-9000",
    ]
    assert [link.label for link in issue.other_projects()] == []
    assert {link.label: link.posted for link in issue.roster} == {
        "Ada": True,
        "Grace": False,
        "Hal 9000": True,
        "Zed": False,
    }
    assert issue.model_id == "test-model"
    assert issue.quote == PIONEER_QUOTES[0] and issue.quote.kind == "pioneer"
    assert [(image.project_id, image.filename, image.kind) for image in issue.images] == [
        ("agents2026-ada", "agents2026-ada.jpg", "post_image")
    ]
    assert issue.images[0].source_url == (
        "https://mitmedialab.github.io/agents2026-ada/assets/hero.webp"
    )
    assert (tmp_path / "newsletter/issues/2026-week01/agents2026-ada.jpg").read_bytes() == (
        b"\xff\xd8jpeg"
    )
    assert "Featured projects, in order: agents2026-ada, agents2026-hal-9000" in writer.prompts[0]
    assert issue.body.headline == "Loop, There It Is"
    assert issue.built_for("agents2026-ada") == "A tidy agent loop."
    assert issue.built_for("agents2026-grace") is None
    assert "No candidate quotes were found this week" in writer.editorial_prompts[0]
    assert "students;" not in writer.editorial_prompts[0]
    assert (tmp_path / "newsletter/issues/2026-week01.html").exists()

    async def scenario() -> None:
        adapter = RecordingMailAdapter()
        preview = await service.send(
            "2026-week01", mail=adapter, recipients=["me@mit.edu"], test_only=True
        )
        assert preview.status == "draft" and store.load("2026-week01") == issue
        assert adapter.sent[0].to == ("me@mit.edu",)
        assert adapter.sent[0].subject == "The Class Runtime from MAS.S60"
        assert adapter.sent[0].html is not None and "<!DOCTYPE html>" in adapter.sent[0].html
        assert 'src="cid:agents2026-ada"' in adapter.sent[0].html
        assert [(image.content_id, image.data) for image in adapter.sent[0].inline_images] == [
            ("agents2026-ada", b"\xff\xd8jpeg")
        ]
        assert "THE CLASS RUNTIME" in adapter.sent[0].text

        with pytest.raises(NewsletterStateError, match="At least one recipient"):
            await service.send("2026-week01", mail=adapter, recipients=[" "])
        with pytest.raises(NewsletterStateError, match="invalid"):
            await service.send("2026-week01", mail=adapter, recipients=["not-an-address"])
        with pytest.raises(NewsletterStateError, match="does not exist"):
            await service.send("2026-week02", mail=adapter, recipients=["a@mit.edu"])

        failing = RecordingMailAdapter(fail_for={"a@mit.edu"})
        with pytest.raises(NewsletterStateError, match="remains a draft"):
            await service.send("2026-week01", mail=failing, recipients=["a@mit.edu"])
        remains = store.load("2026-week01")
        assert remains is not None and remains.status == "draft"
        assert remains.deliveries[0].error == "RuntimeError"

        adapter = RecordingMailAdapter(fail_for={"c@mit.edu"})
        sent = await service.send(
            "2026-week01",
            mail=adapter,
            recipients=["a@mit.edu", " A@mit.edu ", "b@mit.edu", "c@mit.edu"],
        )
        assert sent.status == "sent" and sent.sent_at is not None
        assert [message.to for message in adapter.sent] == [("a@mit.edu",), ("b@mit.edu",)]
        assert [delivery.error for delivery in sent.deliveries] == [None, None, "RuntimeError"]
        assert [delivery.provider_message_id for delivery in sent.deliveries] == [
            "p-1",
            "p-2",
            None,
        ]
        with pytest.raises(NewsletterStateError, match="already sent"):
            await service.send("2026-week01", mail=adapter, recipients=["d@mit.edu"])

    asyncio.run(scenario())

    with pytest.raises(NewsletterStateError, match="already sent"):
        service.draft(week_number=1)

    # The next issue must not feature the students highlighted last week.
    week2_writer = ScriptedWriter(
        [copy_json("agents2026-grace")],
        scores={
            "agents2026-grace": score_json(9, 8, 8).replace(
                '"quotes": []', '"quotes": ["Tools! Tools are how an agent touches the world."]'
            )
        },
        editorials=[editorial_json(quote_choice=1)],
    )
    service2 = NewsletterService(
        settings=NewsletterSettings(highlight_count=2, highlight_cooldown_issues=2),
        weeks=weeks(),
        store=store,
        collector=WeeklyEvidenceCollector(
            fake_catalog(),
            repository_prefix="agents2026-",
            limits=EvidenceLimits(workers=1),
        ),
        writer=week2_writer,
        clock=lambda: datetime(2026, 9, 29, 15, 0, tzinfo=UTC),
    )
    week2_catalog = fake_catalog()
    week2_catalog.repositories["agents2026-grace"].tree.append("weekly_builds/week02/README.md")
    week2_catalog.repositories["agents2026-grace"].files["weekly_builds/week02/README.md"] = (
        "Tools! Tools are how an agent touches the world."
    )
    week2_catalog.repositories["agents2026-ada"].commits.insert(
        0, ("2026-09-25T12:00:00Z", "week 2")
    )
    service2 = NewsletterService(
        settings=NewsletterSettings(highlight_count=2, highlight_cooldown_issues=2),
        weeks=weeks(),
        store=store,
        collector=WeeklyEvidenceCollector(
            week2_catalog, repository_prefix="agents2026-", limits=EvidenceLimits(workers=1)
        ),
        writer=week2_writer,
        image_finder=FakeImageFinder(fail_for={"https://mitmedialab.github.io/agents2026-grace/"}),
        link_checker=None,
        lecture_loader=lambda week: None,
        clock=lambda: datetime(2026, 9, 29, 15, 0, tzinfo=UTC),
    )
    week2 = service2.draft()
    assert week2.issue_id == "2026-week02"
    assert week2.images == ()
    assert week2.highlighted_project_ids() == ("agents2026-grace",)
    assert {score.project_id: score.eligible for score in week2.scores} == {
        "agents2026-ada": False,
        "agents2026-grace": True,
    }
    assert week2.quote.kind == "student" and week2.quote.author == "Grace"
    assert week2.quote.text == "Tools! Tools are how an agent touches the world."
    assert week2.quote.url == "https://mitmedialab.github.io/agents2026-grace/"

    # Rewriting keeps the stored selection, scores, and images; only the copy changes.
    fresh = ScriptedWriter(
        [copy_json("agents2026-ada", "agents2026-hal-9000")],
        editorials=[editorial_json("Loop Again", quote_choice=0)],
    )
    rewriter, _ = make_service(tmp_path, fresh)
    tools_store = FileNewsletterStore(tmp_path / "newsletter")
    tools_store.save(issue.model_copy(update={"status": "draft", "approval": None}))
    rewritten = rewriter.rewrite_copy("2026-week01")
    assert rewritten.body.headline == "Loop Again"
    assert rewritten.highlighted_project_ids() == ("agents2026-ada", "agents2026-hal-9000")
    assert rewritten.scores == issue.scores and rewritten.images == issue.images
    assert rewritten.created_at == datetime(2026, 9, 22, 15, 0, tzinfo=UTC)
    assert fresh.score_prompts == []  # no re-scoring
    tools_store.save(issue.model_copy(update={"status": "sent"}))
    with pytest.raises(NewsletterStateError, match="only drafts"):
        rewriter.rewrite_copy("2026-week01")

    forced = ScriptedWriter(
        [copy_json("agents2026-hal-9000", "agents2026-ada")],
        scores={"agents2026-hal-9000": score_json(9, 9, 9)},
    )
    service3, _ = make_service(tmp_path, forced)
    stale = tmp_path / "newsletter/issues/2026-week01/agents2026-stale.jpg"
    stale.write_bytes(b"old")
    assert service3.draft(week_number=1, force=True).status == "draft"
    assert not stale.exists()
    assert (tmp_path / "newsletter/issues/2026-week01/agents2026-ada.jpg").exists()

    sendless = NewsletterService(settings=NewsletterSettings(), weeks=weeks(), store=store)
    with pytest.raises(NewsletterStateError, match="Drafting requires"):
        sendless.draft(week_number=1)


def test_newsletter_settings_read_environment_and_reject_bad_integers() -> None:
    settings = NewsletterSettings.from_environment(
        {
            "NEWSLETTER_RECIPIENTS": " list@mit.edu, staff@mit.edu ,",
            "NEWSLETTER_HIGHLIGHT_COUNT": "3",
            "NEWSLETTER_COURSE_SITE_URL": "https://example.edu/",
            "NEWSLETTER_DATA_PATH": "/tmp/newsletter-data",
        }
    )
    assert settings.recipients == ("list@mit.edu", "staff@mit.edu")
    assert settings.highlight_count == 3 and settings.highlight_cooldown_issues == 2
    assert settings.branding.course_site_url == "https://example.edu/"
    assert settings.branding.course_title == "AI Agents for Cognitive Augmentation"
    assert settings.data_path == Path("/tmp/newsletter-data")
    assert NewsletterSettings.from_environment({}).subject == "The Class Runtime from MAS.S60"
    with pytest.raises(ConfigurationError):
        NewsletterSettings.from_environment({"NEWSLETTER_HIGHLIGHT_COUNT": "four"})


def test_cli_lists_shows_and_refuses_to_send_without_recipients_or_mail(tmp_path: Path) -> None:
    environment = {"NEWSLETTER_DATA_PATH": str(tmp_path / "newsletter")}
    out, err = io.StringIO(), io.StringIO()

    assert newsletter_main(["list"], environment=environment, out=out, err=err) == 0
    assert "No issues yet." in out.getvalue()
    assert newsletter_main(["show", "2026-week01"], environment=environment, out=out, err=err) == 2
    assert "does not exist" in err.getvalue()

    FileNewsletterStore(tmp_path / "newsletter").save(sample_issue())
    out = io.StringIO()
    assert newsletter_main(["show", "2026-week01"], environment=environment, out=out, err=err) == 0
    assert "Issue: 2026-week01 (draft)" in out.getvalue()
    assert "THE CLASS RUNTIME" in out.getvalue()
    out = io.StringIO()
    assert (
        newsletter_main(
            ["show", "2026-week01", "--html"], environment=environment, out=out, err=err
        )
        == 0
    )
    assert "<!DOCTYPE html>" in out.getvalue()
    out = io.StringIO()
    assert newsletter_main(["list"], environment=environment, out=out, err=err) == 0
    assert "2026-week01\t1\tdraft\t2026-09-22\t-\tagents2026-ada" in out.getvalue()

    err = io.StringIO()
    assert newsletter_main(["pdf", "2026-week09"], environment=environment, out=out, err=err) == 2
    out = io.StringIO()
    assert newsletter_main(["pdf", "2026-week01"], environment=environment, out=out, err=err) == 0
    pdf_path = Path(out.getvalue().strip())
    assert pdf_path.name == "2026-week01.pdf" and pdf_path.read_bytes().startswith(b"%PDF")
    err = io.StringIO()
    assert newsletter_main(["send", "2026-week01"], environment=environment, out=out, err=err) == 2
    assert "No recipients" in err.getvalue()
    err = io.StringIO()
    assert (
        newsletter_main(
            ["send", "2026-week01", "--to", "a@mit.edu"],
            environment=environment,
            out=out,
            err=err,
            stdin=io.StringIO(""),
        )
        == 2
    )
    assert "MAIL_ENABLED=true" in err.getvalue()
    err = io.StringIO()
    assert (
        newsletter_main(
            ["send", "2026-week09", "--to", "a@mit.edu"], environment=environment, out=out, err=err
        )
        == 2
    )
    assert "does not exist" in err.getvalue()


def staff_principal(role: str) -> PrincipalContext:
    return PrincipalContext(
        authenticated=True,
        user_id=uuid4(),
        username=f"test-{role}",
        roles=["public", role],
        session_id=uuid4(),
    )


def tool_context(principal: PrincipalContext) -> ToolExecutionContext:
    return ToolExecutionContext(
        principal=principal,
        conversation_id=uuid4(),
        permitted_resource_uris=frozenset(),
    )


def make_tools(tmp_path: Path, writer: ScriptedWriter) -> tuple[NewsletterTools, InMemoryAuthStore]:
    store = FileNewsletterStore(tmp_path / "newsletter")
    settings = NewsletterSettings(
        highlight_count=2, highlight_cooldown_issues=2, recipients=("staff@mit.edu",)
    )
    weeks_ = weeks()

    def factory(log: Callable[[str], None]) -> NewsletterService:
        return NewsletterService(
            settings=settings,
            weeks=weeks_,
            store=store,
            collector=WeeklyEvidenceCollector(
                fake_catalog(),
                repository_prefix="agents2026-",
                read_site=read_site,
                limits=EvidenceLimits(workers=1),
            ),
            writer=writer,
            image_finder=FakeImageFinder(),
            link_checker=None,
            lecture_loader=lambda week: None,
            model_id="test-model",
            clock=lambda: datetime(2026, 9, 22, 15, 0, tzinfo=UTC),
            log=log,
        )

    runner = NewsletterJobRunner(
        store=store,
        service_factory=factory,
        pdf_exporter=lambda issue_id: tmp_path / f"{issue_id}.pdf",
        clock=lambda: datetime(2026, 9, 22, 15, 0, tzinfo=UTC),
        run_in_thread=False,
    )
    auth = InMemoryAuthStore()
    tools = NewsletterTools(
        settings=settings,
        store=store,
        service=NewsletterService(settings=settings, weeks=weeks_, store=store),
        runner=runner,
        auth=auth,
    )
    return tools, auth


def test_course_agent_tools_draft_review_and_prepare_send_for_instructors_only(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        writer = ScriptedWriter(
            [copy_json("agents2026-ada", "agents2026-hal-9000")],
            scores={
                "agents2026-ada": score_json(9, 8, 7, rationale="Bold."),
                "agents2026-hal-9000": score_json(7, 6, 6),
            },
        )
        tools, auth = make_tools(tmp_path, writer)
        admin = UserAdminService(auth)
        await admin.create_user(
            username="alice", display_name="Alice", email="alice@mit.edu", role="student"
        )
        await admin.create_user(
            username="bob", display_name="Bob", email="bob@mit.edu", role="student"
        )
        parked = await admin.create_user(
            username="carl", display_name="Carl", email="carl@mit.edu", role="student"
        )
        await admin.deactivate_user("carl")
        assert parked.user.username == "carl"
        await admin.create_user(username="ta", display_name="TA", email="ta@mit.edu", role="ta")
        draft_tool, status_tool, send_tool = tools.tools()
        instructor = staff_principal("instructor")

        for role in ("student", "ta", "admin"):
            with pytest.raises(ToolValidationError, match="instructor login"):
                await draft_tool.execute({}, tool_context(staff_principal(role)))
            with pytest.raises(ToolValidationError, match="instructor login"):
                await status_tool.execute({}, tool_context(staff_principal(role)))
            with pytest.raises(ToolValidationError, match="instructor login"):
                await send_tool.execute(
                    {"issue_id": "2026-week01", "audience": "test"},
                    tool_context(staff_principal(role)),
                )
        with pytest.raises(ToolValidationError, match="No newsletter"):
            await status_tool.execute({}, tool_context(instructor))

        # While a job runs, a turn may report it once; a second poll is refused so the agent
        # ends its turn instead of spinning.
        running = tools.runner._store
        from course_server.newsletter.models import NewsletterJob

        in_flight = NewsletterJob(
            job_id=uuid4(), status="running", started_at=datetime(2026, 9, 22, 14, 50, tzinfo=UTC)
        )
        running.save_job(in_flight)
        same_turn = tool_context(instructor)
        first = await status_tool.execute({"job_id": str(in_flight.job_id)}, same_turn)
        assert isinstance(first.content, dict) and "Do not check again" in str(
            first.content["message"]
        )
        with pytest.raises(ToolValidationError, match="Do not check again"):
            await status_tool.execute({"job_id": str(in_flight.job_id)}, same_turn)
        running.save_job(in_flight.model_copy(update={"status": "failed", "error": "x"}))

        started = await draft_tool.execute({"week": 1}, tool_context(instructor))
        assert isinstance(started.content, dict)
        job = started.content["job"]
        assert isinstance(job, dict) and job["status"] == "done" and started.content["started"]
        assert job["issue_id"] == "2026-week01" and job["week"] == 1
        assert started.storage_policy == "server_summary"

        status = await status_tool.execute({}, tool_context(instructor))
        assert isinstance(status.content, dict)
        issue = status.content["issue"]
        assert isinstance(issue, dict)
        assert issue["status"] == "draft" and issue["headline"] == "Loop, There It Is"
        highlights = cast(list[dict[str, JsonValue]], issue["highlights"])
        assert [h["student"] for h in highlights] == ["Ada", "Hal 9000"]
        scoreboard = cast(list[dict[str, JsonValue]], issue["scoreboard_top"])
        assert scoreboard[0]["rationale"] == "Bold."
        assert "THE CLASS RUNTIME" in str(issue["plain_text"])
        by_id = await status_tool.execute({"job_id": str(job["job_id"])}, tool_context(instructor))
        assert isinstance(by_id.content, dict) and by_id.content["issue"] is not None
        with pytest.raises(ToolValidationError, match="does not exist"):
            await status_tool.execute({"job_id": str(uuid4())}, tool_context(instructor))

        with pytest.raises(ToolValidationError, match="test recipient"):
            await send_tool.execute(
                {"issue_id": "2026-week01", "audience": "test"}, tool_context(instructor)
            )
        context = tool_context(instructor)
        prepared = await send_tool.execute(
            {"issue_id": "2026-week01", "audience": "all_students"}, context
        )
        assert isinstance(prepared.content, dict)
        assert prepared.content["confirmation_required"] is True
        assert prepared.content["recipient_count"] == 3  # alice, bob, staff list; carl inactive
        event = prepared.emitted_events[0]
        assert event.type == "instructor.newsletter.confirmation_requested"
        assert event.payload["audience"] == "all_students" and event.payload["recipients"] == []
        assert event.payload["headline"] == "Loop, There It Is"
        assert event.metadata == {"visibility": "private"}
        stored = tools.store.load("2026-week01")
        assert stored is not None and stored.status == "awaiting_confirmation"
        assert stored.approval is not None
        assert set(stored.approval.recipients) == {"alice@mit.edu", "bob@mit.edu", "staff@mit.edu"}
        assert stored.approval.conversation_id == context.conversation_id

        # A test audience lists its recipients and replaces the pending snapshot.
        test = await send_tool.execute(
            {
                "issue_id": "2026-week01",
                "audience": "test",
                "test_recipients": ["me@mit.edu", "me@mit.edu"],
            },
            context,
        )
        assert test.emitted_events[0].payload["recipients"] == ["me@mit.edu"]

    asyncio.run(scenario())


def test_send_requires_the_platform_confirmation_and_the_worker_drains_approved_issues(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        writer = ScriptedWriter(
            [copy_json("agents2026-ada", "agents2026-hal-9000")],
            scores={"agents2026-ada": score_json(9, 8, 7)},
        )
        tools, _ = make_tools(tmp_path, writer)
        tools.runner.start(week_number=1, force=False, requested_by_user_id=None)
        service = tools.service
        conversation_id = uuid4()
        instructor_id = uuid4()

        held = service.request_send(
            "2026-week01",
            audience="test",
            recipients=["me@mit.edu"],
            conversation_id=conversation_id,
            requested_by_user_id=instructor_id,
        )
        assert held.status == "awaiting_confirmation" and held.approval is not None
        confirmation_id = held.approval.confirmation_id

        # The CLI's direct send refuses while the Course Agent holds the issue.
        adapter = RecordingMailAdapter()
        with pytest.raises(NewsletterStateError, match="resolve it in the Course Agent"):
            await service.send("2026-week01", mail=adapter, recipients=["x@mit.edu"])
        assert await service.deliver_approved(adapter) == ()

        # Only the same instructor, conversation, and confirmation id may decide.
        with pytest.raises(NewsletterStateError, match="no longer awaiting"):
            service.confirm_send(
                "2026-week01",
                confirmation_id=uuid4(),
                conversation_id=conversation_id,
                user_id=instructor_id,
            )
        with pytest.raises(NewsletterStateError, match="no longer awaiting"):
            service.confirm_send(
                "2026-week01",
                confirmation_id=confirmation_id,
                conversation_id=uuid4(),
                user_id=instructor_id,
            )
        with pytest.raises(NewsletterStateError, match="no longer awaiting"):
            service.cancel_send(
                "2026-week01",
                confirmation_id=confirmation_id,
                conversation_id=conversation_id,
                user_id=uuid4(),
            )

        cancelled = service.cancel_send(
            "2026-week01",
            confirmation_id=confirmation_id,
            conversation_id=conversation_id,
            user_id=instructor_id,
        )
        assert cancelled.status == "draft" and cancelled.approval is None
        with pytest.raises(NewsletterStateError, match="no longer awaiting"):
            service.confirm_send(
                "2026-week01",
                confirmation_id=confirmation_id,
                conversation_id=conversation_id,
                user_id=instructor_id,
            )

        # An approved test copy is delivered but leaves the issue a draft for the real send.
        trial = service.request_send(
            "2026-week01",
            audience="test",
            recipients=["me@mit.edu"],
            conversation_id=conversation_id,
            requested_by_user_id=instructor_id,
        )
        assert trial.approval is not None
        service.confirm_send(
            "2026-week01",
            confirmation_id=trial.approval.confirmation_id,
            conversation_id=conversation_id,
            user_id=instructor_id,
        )
        tested = await service.deliver_approved(adapter)
        assert [issue.status for issue in tested] == ["draft"]
        assert [message.to for message in adapter.sent] == [("me@mit.edu",)]
        assert tested[0].approval is None and tested[0].deliveries[0].recipient == "me@mit.edu"
        adapter.sent.clear()

        held = service.request_send(
            "2026-week01",
            audience="all_students",
            recipients=["a@mit.edu", "b@mit.edu"],
            conversation_id=conversation_id,
            requested_by_user_id=instructor_id,
        )
        assert held.approval is not None
        approved = service.confirm_send(
            "2026-week01",
            confirmation_id=held.approval.confirmation_id,
            conversation_id=conversation_id,
            user_id=instructor_id,
        )
        assert approved.status == "approved"
        assert approved.approval is not None and approved.approval.decided_at is not None
        assert adapter.sent == []

        delivered = await service.deliver_approved(adapter)
        assert [issue.status for issue in delivered] == ["sent"]
        assert [message.to for message in adapter.sent] == [("a@mit.edu",), ("b@mit.edu",)]
        assert adapter.sent[0].inline_images[0].content_id == "agents2026-ada"
        assert await service.deliver_approved(adapter) == ()
        final = tools.store.load("2026-week01")
        assert final is not None and final.status == "sent" and len(final.deliveries) == 2
        with pytest.raises(NewsletterStateError, match="cannot be resent"):
            service.request_send(
                "2026-week01",
                audience="test",
                recipients=["me@mit.edu"],
                conversation_id=conversation_id,
                requested_by_user_id=instructor_id,
            )

    asyncio.run(scenario())


def test_job_runner_records_failures_and_refuses_concurrent_drafts(tmp_path: Path) -> None:
    store = FileNewsletterStore(tmp_path / "newsletter")

    def failing(log: Callable[[str], None]) -> NewsletterService:
        log("collecting")
        raise RuntimeError("GitHub is down")

    runner = NewsletterJobRunner(store=store, service_factory=failing, run_in_thread=False)
    job, started = runner.start(week_number=None, force=False, requested_by_user_id=None)
    assert started
    failed = runner.status(job.job_id)
    assert failed is not None and failed.status == "failed"
    assert failed.error == "RuntimeError: GitHub is down" and failed.log == ("collecting",)
    assert runner.latest() == failed

    running = failed.model_copy(update={"status": "running", "finished_at": None})
    store.save_job(running)
    runner._current_id = running.job_id  # simulate a job still in flight
    again, started_again = runner.start(week_number=2, force=False, requested_by_user_id=None)
    assert not started_again and again.job_id == running.job_id

    # A job left "running" by a process that died is reported as failed after the stale window.
    stale = running.model_copy(update={"started_at": datetime(2026, 9, 22, 14, 0, tzinfo=UTC)})
    store.save_job(stale)
    later = NewsletterJobRunner(
        store=store,
        service_factory=failing,
        clock=lambda: datetime(2026, 9, 22, 15, 0, tzinfo=UTC),
        run_in_thread=False,
    )
    reported = later.latest()
    assert reported is not None and reported.status == "failed"
    assert reported.error is not None and "restarted" in reported.error


def test_capability_policy_exposes_newsletter_tools_only_to_instructors() -> None:
    from course_server.agent import CourseCapabilityPolicy
    from course_server.newsletter_tool_ids import NEWSLETTER_TOOL_IDS

    enabled = CourseCapabilityPolicy(newsletter_enabled=True)
    disabled = CourseCapabilityPolicy(newsletter_enabled=False)
    assert set(NEWSLETTER_TOOL_IDS) <= set(
        enabled.authorize(staff_principal("instructor")).tool_ids
    )
    for role in ("student", "ta", "admin"):
        assert not set(NEWSLETTER_TOOL_IDS) & set(enabled.authorize(staff_principal(role)).tool_ids)
    assert not set(NEWSLETTER_TOOL_IDS) & set(
        disabled.authorize(staff_principal("instructor")).tool_ids
    )


def test_newsletter_confirmation_endpoint_checks_instructor_ownership_and_state(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from agent_core import AgentRuntime
    from course_server.agent import CourseAgentService, InMemoryConversationStore
    from course_server.api import API_PREFIX, AppServices, create_app
    from course_server.auth import AuthenticationService

    auth = InMemoryAuthStore()
    admin = UserAdminService(auth)

    async def create_users() -> tuple[str, str, UUID]:
        student = await admin.create_user(
            username="student", display_name="Student", email="s@mit.edu", role="student"
        )
        instructor = await admin.create_user(
            username="prof", display_name="Prof", email="p@mit.edu", role="instructor"
        )
        return student.access_code, instructor.access_code, instructor.user.id

    student_code, instructor_code, instructor_id = asyncio.run(create_users())
    store = FileNewsletterStore(tmp_path / "newsletter")
    store.save(sample_issue())
    service = NewsletterService(settings=NewsletterSettings(), weeks=weeks(), store=store)
    conversations = InMemoryConversationStore()
    app = create_app(
        services=AppServices(
            authentication=AuthenticationService(auth),
            agent=CourseAgentService(
                runtime=cast(AgentRuntime, object()), conversations=conversations
            ),
            conversations=conversations,
            newsletter=service,
        )
    )
    instructor_client = TestClient(app, base_url="https://testserver")
    student_client = TestClient(app, base_url="https://testserver")
    assert (
        instructor_client.post(
            f"{API_PREFIX}/auth/login", json={"username": "prof", "access_code": instructor_code}
        ).status_code
        == 200
    )
    assert (
        student_client.post(
            f"{API_PREFIX}/auth/login", json={"username": "student", "access_code": student_code}
        ).status_code
        == 200
    )
    conversation_id = UUID(
        instructor_client.post(f"{API_PREFIX}/conversations", json={}).json()["id"]
    )
    held = service.request_send(
        "2026-week01",
        audience="test",
        recipients=["me@mit.edu"],
        conversation_id=conversation_id,
        requested_by_user_id=instructor_id,
    )
    assert held.approval is not None
    url = f"{API_PREFIX}/conversations/{conversation_id}/newsletter/2026-week01/confirmation"
    body = {"action": "send", "confirmation_id": str(held.approval.confirmation_id)}

    assert student_client.post(url, json=body).status_code == 404
    wrong = instructor_client.post(url, json={**body, "confirmation_id": str(uuid4())})
    assert wrong.status_code == 409
    approved = instructor_client.post(url, json=body)
    assert approved.status_code == 200
    assert approved.json()["type"] == "instructor.newsletter.approved"
    assert approved.json()["payload"] == {
        "issue_id": "2026-week01",
        "confirmation_id": str(held.approval.confirmation_id),
        "subject": "The Class Runtime from MAS.S60",
        "recipient_count": 1,
        "status": "approved",
    }
    stored = store.load("2026-week01")
    assert stored is not None and stored.status == "approved"
    assert instructor_client.post(url, json=body).status_code == 409
    assert instructor_client.post(url, json={**body, "action": "cancel"}).status_code == 409
