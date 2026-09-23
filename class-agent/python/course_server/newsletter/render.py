"""Deterministic plain-text and HTML renderings of a newsletter issue.

The HTML follows the course site's visual language: a black ground, ivory display text,
small letter-spaced monospace labels, muted secondary text, and fine rules. Every string
that originated in the model or in repository metadata is escaped here, and only roster URLs
resolved by platform code become links.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from html import escape

from .models import HighlightImage, NewsletterIssue, ProjectLink

_RULE = "-" * 60

# Mirrors packages/ui/src/styles.css tokens; email clients need literal values.
_GROUND = "#000000"
_SURFACE = "#111111"
_INK = "#f5f5f2"
_INK_SOFT = "#c9c9c4"
_MUTED = "#8b8b86"
_BORDER = "#2a2a28"
_SANS = "'Helvetica Neue',Helvetica,Arial,sans-serif"
_MONO = "SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"
_LABEL = (
    f"font-family:{_MONO};font-size:11px;letter-spacing:0.14em;text-transform:uppercase;"
    f"color:{_MUTED};"
)
_UNDERLINED = (
    f"color:{_INK};text-decoration:none;border-bottom:1px solid {_BORDER};padding-bottom:3px;"
)

ImageSource = Callable[[HighlightImage], str]


def _short_date(value: date) -> str:
    return f"{value.strftime('%b')} {value.day}"


def _window_label(issue: NewsletterIssue) -> str:
    week = issue.week
    last_day = (week.ends_at - timedelta(days=1)).date()
    return (
        f"Week {week.number} · {_short_date(week.class_date)} \u2013 {_short_date(last_day)}, "
        f"{last_day.year}"
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
        f"{branding.newsletter_name.upper()} · ISSUE {issue.week.number:02d}",
        issue.body.opening,
        "",
        _window_label(issue),
        f"THE ASSIGNMENT: {issue.week.tutorial}",
        _RULE,
        "",
        "HIGHLIGHTS",
        "",
    ]
    for index, highlight in enumerate(issue.body.highlights, start=1):
        link = issue.link_for(highlight.project_id)
        label = link.label if link else highlight.project_id
        lines.append(f"{index}. {highlight.headline} — {label}")
        lines.append(f"   {highlight.description}")
        if link and link.site_url:
            lines.append(f"   Open it: {link.site_url}")
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


def relative_image_source(issue: NewsletterIssue) -> ImageSource:
    """Image paths for the on-disk preview that sits next to the issue's asset folder."""

    return lambda image: f"{issue.issue_id}/{image.filename}"


def cid_image_source(image: HighlightImage) -> str:
    """Image references for an email whose images travel as inline attachments."""

    return f"cid:{image.content_id}"


def _anchor(url: str, inner: str, *, style: str) -> str:
    return f'<a href="{escape(url, quote=True)}" style="{style}">{inner}</a>'


def _highlight_html(issue: NewsletterIssue, index: int, *, image_src: ImageSource) -> str:
    highlight = issue.body.highlights[index - 1]
    link = issue.link_for(highlight.project_id)
    label = escape(link.label if link else highlight.project_id)
    site_url = link.site_url if link else None
    image = issue.image_for(highlight.project_id)
    figure = ""
    if image is not None:
        img = (
            f'<img src="{escape(image_src(image), quote=True)}" width="600" '
            f'alt="{escape(highlight.headline, quote=True)}" '
            f'style="display:block;width:100%;max-width:600px;height:auto;border:1px solid '
            f'{_BORDER};border-radius:12px;background:{_SURFACE};">'
        )
        framed = _anchor(site_url, img, style="text-decoration:none;") if site_url else img
        figure = f'<div style="margin:0 0 18px 0;">{framed}</div>'
    open_link = (
        '<p style="margin:14px 0 0 0;">'
        + _anchor(site_url, f"Open {label}&rsquo;s build &rarr;", style=f"{_LABEL}{_UNDERLINED}")
        + "</p>"
        if site_url
        else ""
    )
    return (
        f'<div style="margin:0 0 48px 0;">{figure}'
        f'<p style="margin:0 0 8px 0;{_LABEL}">{index:02d} &middot; {label}</p>'
        f'<h2 style="margin:0 0 10px 0;font-family:{_SANS};font-size:24px;line-height:1.2;'
        f'font-weight:500;color:{_INK};">{escape(highlight.headline)}</h2>'
        f'<p style="margin:0;font-family:{_SANS};font-size:16px;line-height:1.55;'
        f'color:{_INK_SOFT};">{escape(highlight.description)}</p>'
        f"{open_link}</div>"
    )


def render_html(issue: NewsletterIssue, *, image_src: ImageSource | None = None) -> str:
    branding = issue.branding
    source = image_src or relative_image_source(issue)
    highlights = "".join(
        _highlight_html(issue, index, image_src=source)
        for index in range(1, len(issue.body.highlights) + 1)
    )
    others = issue.other_projects()
    name_style = f"{_LABEL}color:{_INK};text-decoration:none;"
    other_names = (
        " &nbsp;&middot;&nbsp; ".join(
            _anchor(link.site_url, escape(link.label), style=name_style)
            if link.site_url
            else f'<span style="{_LABEL}">{escape(link.label)}</span>'
            for link in others
        )
        if others
        else f'<span style="{_LABEL}">Everyone made the highlights this week.</span>'
    )
    rule = f'<hr style="border:0;border-top:1px solid {_BORDER};margin:36px 0;">'
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="dark">'
        f"<title>{escape(issue.subject)}</title></head>"
        f'<body style="margin:0;padding:0;background:{_GROUND};color:{_INK};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'bgcolor="{_GROUND}" style="background:{_GROUND};"><tr><td align="center" '
        'style="padding:40px 16px;">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
        'style="max-width:600px;width:100%;"><tr><td style="text-align:left;">'
        f'<p style="margin:0 0 28px 0;{_LABEL}">{escape(branding.newsletter_name)} '
        f"&middot; Issue {issue.week.number:02d}</p>"
        f'<h1 style="margin:0 0 24px 0;font-family:{_SANS};font-size:34px;line-height:1.15;'
        f'font-weight:500;letter-spacing:-0.01em;color:{_INK};">{escape(issue.body.opening)}</h1>'
        f'<p style="margin:0 0 10px 0;{_LABEL}">{escape(_window_label(issue))}</p>'
        f'<p style="margin:0 0 6px 0;{_LABEL}">The assignment</p>'
        f'<p style="margin:0;font-family:{_SANS};font-size:20px;line-height:1.4;'
        f'font-weight:400;color:{_INK};">{escape(issue.week.tutorial)}</p>'
        f"{rule}"
        f'<p style="margin:0 0 24px 0;{_LABEL}">Highlights</p>'
        f"{highlights}"
        f"{rule}"
        f'<p style="margin:0 0 16px 0;{_LABEL}">All the other builds this week</p>'
        f'<p style="margin:0;line-height:2.1;">{other_names}</p>'
        f"{rule}"
        f'<p style="margin:0 0 12px 0;font-family:{_SANS};font-size:21px;line-height:1.4;'
        f'font-weight:300;color:{_INK};">&ldquo;{escape(issue.quote.text)}&rdquo;</p>'
        f'<p style="margin:0;{_LABEL}">&mdash; {escape(issue.quote.author)}, '
        f"{escape(issue.quote.source)}</p>"
        f"{rule}"
        f'<p style="margin:0 0 10px 0;{_LABEL}">{escape(_course_line(issue))}</p>'
        '<p style="margin:0 0 10px 0;">'
        + _anchor(
            branding.course_site_url,
            escape(branding.course_site_url),
            style=f"{_LABEL}{_UNDERLINED}",
        )
        + "</p>"
        f'<p style="margin:0;{_LABEL}">Sent by {escape(branding.sender_name)}</p>'
        "</td></tr></table></td></tr></table></body></html>\n"
    )
