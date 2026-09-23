"""Score every active project against a fixed rubric, then rank and select in code.

The model judges one project at a time from bounded evidence. Platform code owns the
weights, the goal-fit gate, the cooldown, tie-breaking, and the final selection.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from pydantic import ValidationError

from .models import CourseWeek, NewsletterBranding, ProjectEvidence, ProjectScore, WeeklyDigest

if TYPE_CHECKING:
    from .compose import NewsletterWriter

SCORE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "interest": {
            "type": "integer",
            "description": "0-10: how interesting, original, or ambitious the build is.",
        },
        "execution": {
            "type": "integer",
            "description": "0-10: how complete, working, and well documented it is.",
        },
        "goal_fit": {
            "type": "integer",
            "description": "0-10: how properly it follows this week's assignment goal.",
        },
        "rationale": {
            "type": "string",
            "description": "At most 40 words explaining the scores, for the instructor.",
        },
    },
    "required": ["interest", "execution", "goal_fit", "rationale"],
    "additionalProperties": False,
}
# Instructor-set weights: following the assignment and being interesting matter most.
SCORE_WEIGHTS: dict[str, float] = {"goal_fit": 0.4, "interest": 0.4, "execution": 0.2}
# Projects below this goal-fit score are featured only when nothing better exists.
MIN_GOAL_FIT = 5
SCORING_MARKER = "You are scoring one student project"


class NewsletterScoringError(RuntimeError):
    """A project could not be scored; it is excluded from selection."""


def build_score_system_prompt(branding: NewsletterBranding) -> str:
    return (
        f"{SCORING_MARKER} for {branding.course_code} {branding.course_title} "
        f"({branding.institution}, {branding.course_term}) so course staff can choose which "
        "builds to feature in the weekly newsletter.\n\n"
        "Score three things from 0 to 10 using only the evidence provided:\n"
        "- interest: how interesting the idea is and how interesting the result is; reward "
        "originality, ambition, surprising findings, and honest analysis of failures.\n"
        "- execution: how complete and working the build is and how well it is documented; "
        "a bare template, an empty folder, or plans without a build score low.\n"
        "- goal_fit: whether the student properly did what this week's assignment asked, not "
        "something adjacent. Missing the core of the assignment scores 3 or below.\n"
        "Be strict and consistent: 5 is an ordinary complete submission, 8 or more is "
        "exceptional, and evidence-free claims do not count. Respond with JSON matching the "
        "schema and nothing else."
    )


def build_score_user_prompt(week: CourseWeek, project: ProjectEvidence) -> str:
    from .compose import describe_project

    return "\n\n".join(
        [
            f"Week {week.number} assignment goal: {week.tutorial}",
            f"Lecture topic: {week.topic}",
            f"Project id: {project.project_id}",
            describe_project(project, status="candidate"),
        ]
    )


def parse_score(raw: str, *, project_id: str, eligible: bool) -> ProjectScore:
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise NewsletterScoringError(f"{project_id}: the model returned invalid JSON.") from error
    if not isinstance(payload, dict):
        raise NewsletterScoringError(f"{project_id}: the model returned no score object.")
    try:
        interest = int(payload.get("interest", -1))
        execution = int(payload.get("execution", -1))
        goal_fit = int(payload.get("goal_fit", -1))
        rationale = str(payload.get("rationale", ""))[:600]
        return ProjectScore(
            project_id=project_id,
            interest=interest,
            execution=execution,
            goal_fit=goal_fit,
            total=round(
                SCORE_WEIGHTS["goal_fit"] * goal_fit
                + SCORE_WEIGHTS["interest"] * interest
                + SCORE_WEIGHTS["execution"] * execution,
                2,
            ),
            rationale=rationale,
            eligible=eligible,
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise NewsletterScoringError(
            f"{project_id}: the score did not match the rubric."
        ) from error


def score_projects(
    digest: WeeklyDigest,
    writer: NewsletterWriter,
    *,
    branding: NewsletterBranding,
    workers: int = 4,
    log: Callable[[str], None] | None = None,
) -> tuple[ProjectScore, ...]:
    """Score every active project; cooldown projects are scored but marked ineligible."""

    say = log or (lambda message: None)
    system_prompt = build_score_system_prompt(branding)
    eligible = set(digest.eligible_project_ids())
    active = [project for project in digest.projects if project.active]

    def score_one(project: ProjectEvidence) -> ProjectScore | None:
        try:
            raw = writer.write(
                system_prompt=system_prompt,
                user_prompt=build_score_user_prompt(digest.week, project),
                schema=SCORE_SCHEMA,
            )
            score = parse_score(
                raw, project_id=project.project_id, eligible=project.project_id in eligible
            )
        except NewsletterScoringError as error:
            say(f"  {project.project_id}: not scored ({error})")
            return None
        except Exception as error:  # a provider failure for one project must not stop the rest
            say(f"  {project.project_id}: not scored ({type(error).__name__})")
            return None
        say(
            f"  {project.project_id}: total {score.total:.1f} "
            f"(goal {score.goal_fit}, interest {score.interest}, execution {score.execution})"
        )
        return score

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(score_one, active))
    scored = [score for score in results if score is not None]
    return tuple(sorted(scored, key=_ranking_key))


def _ranking_key(score: ProjectScore) -> tuple[int, float, int, int, str]:
    return (
        0 if score.goal_fit >= MIN_GOAL_FIT else 1,
        -score.total,
        -score.goal_fit,
        -score.interest,
        score.project_id,
    )


def select_highlights(scores: Sequence[ProjectScore], *, count: int) -> tuple[str, ...]:
    """Top eligible projects by weighted total; goal-fit failures rank last."""

    ranked = sorted((score for score in scores if score.eligible), key=_ranking_key)
    return tuple(score.project_id for score in ranked[: max(0, count)])
