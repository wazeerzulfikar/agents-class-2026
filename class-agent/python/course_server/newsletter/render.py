"""Deterministic plain-text and HTML renderings of a newsletter issue.

Every string that originated in the model or in repository metadata is escaped here.
Only roster URLs resolved by platform code become links.
"""

from __future__ import annotations

from datetime import timedelta
from html import escape

from .models import NewsletterIssue, ProjectLink

_RULE = "-" * 60


def _window_label(issue: NewsletterIssue) -> str:
    week = issue.week
    last_day = (week.ends_at - timedelta(days=1)).date()
    return (
        f"Week {week.number} · class of {week.class_date.strftime('%b')} {week.class_date.day}, "
        f"{week.class_date.year} · builds through {last_day.strftime('%b')} {last_day.day}"
    )


def _course_line(issue: NewsletterIssue) -> str:
    branding = issue.branding
    return (
        f"{branding.course_code} · {branding.course_title} · "
        f"{branding.institution}, {branding.course_term}"
    )


def _project_line(link: ProjectLink) -> str:
    return f"{link.label} — {link.site_url}" if link.site_url else f"{link.label} (no site yet)"


def render_text(issue: NewsletterIssue) -> str:
    branding = issue.branding
    lines: list[str] = [
        branding.newsletter_name.upper(),
        _window_label(issue),
        _course_line(issue),
        _RULE,
        "",
        issue.body.opening,
        "",
        f"THIS WEEK'S HIGHLIGHTS — the brief: {issue.week.tutorial}",
        "",
    ]
    for index, highlight in enumerate(issue.body.highlights, start=1):
        link = issue.link_for(highlight.project_id)
        label = link.label if link else highlight.project_id
        lines.append(f"{index}. {highlight.headline} — {label}")
        lines.append(f"   {highlight.summary}")
        lines.append(f"   Why it fits the brief: {highlight.goal_link}")
        if link and link.site_url:
            lines.append(f"   Open it: {link.site_url}")
        lines.append("")
    lines.append(issue.body.closing)
    lines.append("")
    lines.append("ALL THE OTHER BUILDS THIS WEEK")
    others = issue.other_projects()
    if others:
        lines.extend(f"- {_project_line(link)}" for link in others)
    else:
        lines.append("- Everyone made the highlights this week.")
    lines.extend(
        [
            "",
            _RULE,
            f'"{issue.quote.text}"',
            f"— {issue.quote.author}, {issue.quote.source}",
            "",
            _course_line(issue),
            f"Class website: {branding.course_site_url}",
            f"Sent by {branding.sender_name}.",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def _link_html(link: ProjectLink | None, fallback: str) -> str:
    if link is None:
        return escape(fallback)
    if link.site_url is None:
        return escape(link.label)
    return (
        f'<a href="{escape(link.site_url, quote=True)}" style="color:#111111;">'
        f"{escape(link.label)}</a>"
    )


def render_html(issue: NewsletterIssue) -> str:
    branding = issue.branding
    highlights: list[str] = []
    for highlight in issue.body.highlights:
        link = issue.link_for(highlight.project_id)
        open_link = (
            f'<p style="margin:6px 0 0 0;"><a href="{escape(link.site_url, quote=True)}" '
            f'style="color:#111111;">Open {escape(link.label)}\u2019s build &rarr;</a></p>'
            if link and link.site_url
            else ""
        )
        highlights.append(
            '<li style="margin:0 0 22px 0;">'
            f'<p style="margin:0;font-size:17px;font-weight:600;">{escape(highlight.headline)}'
            f' <span style="font-weight:400;color:#666666;">&middot; '
            f"{_link_html(link, highlight.project_id)}</span></p>"
            f'<p style="margin:6px 0 0 0;">{escape(highlight.summary)}</p>'
            f'<p style="margin:6px 0 0 0;color:#444444;"><em>Why it fits the brief:</em> '
            f"{escape(highlight.goal_link)}</p>"
            f"{open_link}</li>"
        )
    others = issue.other_projects()
    other_items = (
        "".join(
            f'<li style="margin:0 0 6px 0;">{_link_html(link, link.project_id)}</li>'
            for link in others
        )
        if others
        else '<li style="margin:0;">Everyone made the highlights this week.</li>'
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(issue.subject)}</title></head>"
        '<body style="margin:0;padding:24px 16px;background:#ffffff;color:#111111;'
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;"
        'font-size:16px;line-height:1.5;">'
        '<div style="max-width:640px;margin:0 auto;">'
        '<p style="margin:0;font-size:12px;letter-spacing:0.12em;text-transform:uppercase;'
        f'color:#666666;">{escape(branding.newsletter_name)}</p>'
        '<h1 style="margin:6px 0 4px 0;font-size:24px;font-weight:600;">'
        f"{escape(_window_label(issue))}</h1>"
        '<p style="margin:0 0 20px 0;color:#666666;font-size:14px;">'
        f"{escape(_course_line(issue))}</p>"
        '<hr style="border:0;border-top:1px solid #dddddd;margin:0 0 20px 0;">'
        f'<p style="margin:0 0 20px 0;">{escape(issue.body.opening)}</p>'
        '<h2 style="margin:0 0 4px 0;font-size:18px;font-weight:600;">'
        "This week\u2019s highlights</h2>"
        '<p style="margin:0 0 16px 0;color:#666666;font-size:14px;">'
        f"The brief: {escape(issue.week.tutorial)}</p>"
        f'<ol style="margin:0;padding:0 0 0 22px;">{"".join(highlights)}</ol>'
        f'<p style="margin:8px 0 20px 0;">{escape(issue.body.closing)}</p>'
        '<h2 style="margin:0 0 8px 0;font-size:18px;font-weight:600;">'
        "All the other builds this week</h2>"
        f'<ul style="margin:0 0 24px 0;padding:0 0 0 22px;">{other_items}</ul>'
        '<hr style="border:0;border-top:1px solid #dddddd;margin:0 0 20px 0;">'
        '<blockquote style="margin:0 0 20px 0;padding:0 0 0 14px;'
        f'border-left:2px solid #111111;color:#333333;">{escape(issue.quote.text)}<br>'
        f'<span style="color:#666666;font-size:14px;">&mdash; {escape(issue.quote.author)}, '
        f"{escape(issue.quote.source)}</span></blockquote>"
        '<p style="margin:0;color:#666666;font-size:13px;">'
        f"{escape(_course_line(issue))}<br>"
        f'<a href="{escape(branding.course_site_url, quote=True)}" style="color:#111111;">'
        f"{escape(branding.course_site_url)}</a><br>Sent by {escape(branding.sender_name)}.</p>"
        "</div></body></html>\n"
    )
