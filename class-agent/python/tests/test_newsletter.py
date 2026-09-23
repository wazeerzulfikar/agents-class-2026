from __future__ import annotations

import asyncio
import io
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image
from pydantic import JsonValue

from course_server.config import ConfigurationError
from course_server.mail import InboundMail, OutboundMail, SentMail
from course_server.newsletter import (
    PIONEER_QUOTES,
    CourseWeek,
    EvidenceLimits,
    FileNewsletterStore,
    Highlight,
    HighlightImage,
    NewsletterBranding,
    NewsletterCompositionError,
    NewsletterCopy,
    NewsletterIssue,
    NewsletterScheduleError,
    NewsletterService,
    NewsletterSettings,
    NewsletterStateError,
    NewsletterStoreError,
    ProjectEvidence,
    ProjectLink,
    ScreenshotError,
    SiteScreenshot,
    WeeklyDigest,
    WeeklyEvidenceCollector,
    build_user_prompt,
    choose_quote,
    cid_image_source,
    compose_newsletter,
    encode_jpeg,
    parse_schedule,
    project_label,
    render_html,
    render_text,
    select_week,
)
from course_server.newsletter.cli import main as newsletter_main
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
    return {"url": url, "text": f"Welcome to {url}\n\n\n\nWeek 1 write-up", "images": []}


def test_collector_gathers_bounded_week_evidence_without_stopping_on_failures() -> None:
    catalog = fake_catalog()
    collector = WeeklyEvidenceCollector(
        catalog,
        repository_prefix="agents2026-",
        read_site=read_site,
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
            "opening": "Loops, loops everywhere." + extra,
            "highlights": [
                {
                    "project_id": project_id,
                    "headline": f"{project_id} gets loopy",
                    "summary": "A from-scratch loop.",
                    "goal_link": "It is the minimal loop the brief asked for.",
                }
                for project_id in project_ids
            ],
            "closing": "Scroll on for everyone else.",
        }
    )


@dataclass
class ScriptedWriter:
    responses: list[str]
    prompts: list[str] = field(default_factory=list)

    def write(self, *, system_prompt: str, user_prompt: str, schema: dict[str, object]) -> str:
        assert "Respond with JSON" in system_prompt
        assert schema["type"] == "object"
        self.prompts.append(user_prompt)
        return self.responses.pop(0)


def test_compose_enforces_eligibility_and_reprompts_once_with_concrete_problems() -> None:
    week = weeks()[0]
    digest = digest_for(week, cooldown=frozenset({"agents2026-hal"}))
    writer = ScriptedWriter(
        [
            copy_json("agents2026-hal", "agents2026-ada", "agents2026-ada"),
            copy_json("agents2026-ada", "agents2026-grace"),
        ]
    )

    copy = compose_newsletter(digest, writer, branding=NewsletterBranding())

    assert [item.project_id for item in copy.highlights] == ["agents2026-ada", "agents2026-grace"]
    assert len(writer.prompts) == 2
    assert "INELIGIBLE (featured recently)" in writer.prompts[0]
    assert "INELIGIBLE (no build activity this week)" in writer.prompts[0]
    assert "goal for the week: Build a minimal agent loop.\n" in writer.prompts[0]
    assert "rejected" in writer.prompts[1]
    assert "agents2026-hal was featured recently" in writer.prompts[1]
    assert "highlighted more than once" in writer.prompts[1]
    assert "expected exactly 2 highlights, got 3" in writer.prompts[1]

    with pytest.raises(NewsletterCompositionError, match="broke platform rules"):
        compose_newsletter(
            digest,
            ScriptedWriter([copy_json("agents2026-idle"), copy_json("agents2026-idle")]),
            branding=NewsletterBranding(),
        )
    with pytest.raises(NewsletterCompositionError, match="invalid JSON"):
        compose_newsletter(digest, ScriptedWriter(["not json"]), branding=NewsletterBranding())
    with pytest.raises(NewsletterCompositionError, match="must not contain links"):
        compose_newsletter(
            digest,
            ScriptedWriter(
                [
                    copy_json("agents2026-ada", "agents2026-grace", extra=" See https://x.y"),
                    copy_json("agents2026-ada", "agents2026-grace", extra=" Mail me@x.edu"),
                ]
            ),
            branding=NewsletterBranding(),
        )
    wordy = " ".join(["word"] * 40)
    with pytest.raises(NewsletterCompositionError, match="40 words; the limit is 32"):
        compose_newsletter(
            digest,
            ScriptedWriter(
                [
                    copy_json("agents2026-ada", "agents2026-grace").replace(
                        "A from-scratch loop.", wordy
                    ),
                    copy_json("agents2026-ada", "agents2026-grace").replace(
                        "A from-scratch loop.", wordy
                    ),
                ]
            ),
            branding=NewsletterBranding(),
        )
    empty = WeeklyDigest(
        week=week, projects=(evidence("agents2026-idle", "Idle", active=False),), highlight_count=4
    )
    with pytest.raises(NewsletterCompositionError, match="No eligible project"):
        compose_newsletter(empty, ScriptedWriter([]), branding=NewsletterBranding())
    assert "Number of highlights to write: 2" in build_user_prompt(digest)


def sample_issue(*, status: str = "draft") -> NewsletterIssue:
    week = weeks()[0]
    return NewsletterIssue(
        issue_id="2026-week01",
        week=week,
        branding=NewsletterBranding(),
        subject="The Class Runtime from MAS.S60",
        body=NewsletterCopy(
            opening="Week one is in the loop.",
            highlights=(
                Highlight(
                    project_id="agents2026-ada",
                    headline="Ada <script>alert(1)</script> loops",
                    summary="A minimal agent loop built from scratch.",
                    goal_link="It is exactly the from-scratch loop the brief asked for.",
                ),
            ),
            closing="Everyone else is below.",
        ),
        roster=(
            ProjectLink(project_id="agents2026-ada", label="Ada", site_url="https://a.example/"),
            ProjectLink(
                project_id="agents2026-grace", label="Grace", site_url="https://g.example/?x=1&y=2"
            ),
            ProjectLink(project_id="agents2026-hal", label="Hal", site_url=None),
        ),
        quote=PIONEER_QUOTES[0],
        images=(
            HighlightImage(
                project_id="agents2026-ada",
                filename="agents2026-ada.jpg",
                media_type="image/jpeg",
                width=1200,
                height=750,
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
    assert text.startswith(
        "THE CLASS RUNTIME\nWeek 1 · class of Sep 15, 2026 · builds through Sep 21"
    )
    assert "MAS.S60 · AI Agents for Cognitive Augmentation · MIT, Fall 2026" in text
    assert "1. Ada <script>alert(1)</script> loops — Ada" in text
    assert "Why it fits the brief: It is exactly" in text
    assert "Open it: https://a.example/" in text
    assert (
        "ALL THE OTHER BUILDS THIS WEEK\n- Grace — https://g.example/?x=1&y=2\n- Hal (no site yet)"
        in text
    )
    assert "Ada — https://a.example/" not in text.split("ALL THE OTHER BUILDS")[1]
    assert '"We can only see a short distance ahead' in text
    assert "— Alan Turing, Computing Machinery and Intelligence, 1950" in text
    assert "Class website: https://cognitive-agents.media.mit.edu" in text

    html = render_html(issue)
    assert "<script>" not in html
    assert "Ada &lt;script&gt;alert(1)&lt;/script&gt; loops" in html
    assert "<h1" in html and "Week one is in the loop." in html
    assert 'src="2026-week01/agents2026-ada.jpg"' in html
    assert 'src="cid:agents2026-ada"' in render_html(issue, image_src=cid_image_source)
    assert '<a href="https://a.example/"' in html
    assert 'href="https://g.example/?x=1&amp;y=2"' in html
    assert ">Hal</span>" in html and "Hal</a>" not in html
    assert 'href="https://cognitive-agents.media.mit.edu"' in html
    assert "Alan Turing" in html and "<title>The Class Runtime from MAS.S60</title>" in html
    assert "background:#000000" in html and "#f5f5f2" in html
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
    assert store.used_quote_texts() == {PIONEER_QUOTES[0].text, PIONEER_QUOTES[1].text}
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
class FakeScreenshotter:
    urls: list[str] = field(default_factory=list)
    fail_for: set[str] = field(default_factory=set)

    def __call__(self, url: str) -> SiteScreenshot:
        self.urls.append(url)
        if url in self.fail_for:
            raise ScreenshotError("Screenshot failed (Timeout).")
        return SiteScreenshot(data=b"\xff\xd8jpeg", media_type="image/jpeg", width=1200, height=750)


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
        screenshotter=FakeScreenshotter(),
        model_id="test-model",
        clock=lambda: datetime(2026, 9, 22, 15, 0, tzinfo=UTC),
    )
    return service, store


def test_service_drafts_for_review_and_sends_only_on_explicit_approval(tmp_path: Path) -> None:
    writer = ScriptedWriter([copy_json("agents2026-ada", "agents2026-hal-9000")])
    service, store = make_service(tmp_path, writer)

    issue = service.draft(as_of=date(2026, 9, 22))

    assert issue.issue_id == "2026-week01" and issue.status == "draft"
    assert issue.highlighted_project_ids() == ("agents2026-ada", "agents2026-hal-9000")
    assert [link.label for link in issue.other_projects()] == ["Grace", "Zed"]
    assert issue.model_id == "test-model" and issue.quote == PIONEER_QUOTES[0]
    assert [(image.project_id, image.filename) for image in issue.images] == [
        ("agents2026-ada", "agents2026-ada.jpg")
    ]
    assert (tmp_path / "newsletter/issues/2026-week01/agents2026-ada.jpg").read_bytes() == (
        b"\xff\xd8jpeg"
    )
    assert "Eligible project ids: agents2026-ada, agents2026-hal-9000" in writer.prompts[0]
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
    week2_writer = ScriptedWriter([copy_json("agents2026-grace")])
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
        "Tools!"
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
        screenshotter=FakeScreenshotter(
            fail_for={"https://mitmedialab.github.io/agents2026-grace/"}
        ),
        clock=lambda: datetime(2026, 9, 29, 15, 0, tzinfo=UTC),
    )
    week2 = service2.draft()
    assert week2.issue_id == "2026-week02"
    assert week2.images == ()
    assert week2.highlighted_project_ids() == ("agents2026-grace",)
    assert (
        "Recently featured and therefore ineligible this week: agents2026-ada, agents2026-hal-9000"
        in (week2_writer.prompts[0])
    )
    assert week2.quote == PIONEER_QUOTES[1]

    forced = ScriptedWriter([copy_json("agents2026-hal-9000", "agents2026-ada")])
    service3, _ = make_service(tmp_path, forced)
    assert service3.draft(week_number=1, force=True).status == "draft"

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
