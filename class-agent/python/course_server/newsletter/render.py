"""Deterministic plain-text and HTML renderings of a newsletter issue.

The HTML follows the course site's visual language: a black ground, ivory display text,
quiet sentence-case labels, near-white secondary text, and fine rules. Every string
that originated in the model or in repository metadata is escaped here, and only roster URLs
resolved by platform code become links.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, timedelta
from html import escape
from urllib.parse import urlencode, urlsplit

from .models import HighlightImage, NewsletterIssue, ProjectLink

_MARKDOWN_LINK = re.compile(r"\[([^\]\n]{1,120})\]\((https://[^\s)]+)\)")

_RULE = "-" * 60
# The selection line opens the course site with this as the student's first question to the
# Course Agent (apps/web/src/startupQuery.ts reads `q`), which answers from the course FAQ.
HIGHLIGHTS_QUESTION = "newslettercriteria"

# Mirrors packages/ui/src/styles.css tokens; email clients need literal values.
_GROUND = "#000000"
_SURFACE = "#111111"
_INK = "#f5f5f2"
# Secondary text is near-white on purpose: the site's muted grey reads as a watermark in an
# inbox, so the email separates levels by size and weight rather than by dimming the text.
_INK_SOFT = "#f0f0ec"
_MUTED = "#d6d6d0"
_BORDER = "#2a2a28"
_SANS = "'Helvetica Neue',Helvetica,Arial,sans-serif"
# Section labels and metadata: the body face in sentence case, small and medium-weight.
_LABEL = f"font-family:{_SANS};font-size:14px;line-height:1.4;font-weight:500;color:{_MUTED};"
_SECTION = f"font-family:{_SANS};font-size:15px;line-height:1.4;font-weight:600;color:{_INK};"
# Section subheadings sit between the 17px body and the 34px headline, bold so they read as
# headings, and small enough that "All the other builds this week" fits a phone's width.
_SUBHEAD = (
    f"font-family:{_SANS};font-size:22px;line-height:1.25;font-weight:700;"
    f"letter-spacing:-0.01em;color:{_INK};"
)
# Link underlines sit inside the text blend below, where a client that recolors borders and one
# that does not produce mirror-image results; a mid grey looks the same either way.
_LINK_LINE = "#7d7d78"
_UNDERLINED = (
    f"color:{_INK};text-decoration:none;border-bottom:1px solid {_LINK_LINE};padding-bottom:3px;"
)

# The Gmail apps recolor dark emails in dark mode: plain backgrounds turn light and light text
# turns grey, while gradient backgrounds are left alone. Every run of text therefore sits inside
# a screen/difference blend pair (Rémi Parmentier's technique). It cancels Gmail's recoloring
# and composites to the original pixels in every other client. The styles must be inline: the
# Gmail iPhone app ignored the same rules when they came from a stylesheet. Images and rules
# stay outside the pair, because a difference blend would invert them.
_TEXT_OPEN = (
    '<div style="background:#000;mix-blend-mode:screen;">'
    '<div style="background:#000;mix-blend-mode:difference;">'
)
_TEXT_CLOSE = "</div></div>"
# WebKit's per-element opt-out from the color filter Mac Mail applies to adapt a message.
_APPLE_FILTER_OFF = "html,body,*{-apple-color-filter:none !important;}"
# Section rules are painted as gradients, which Gmail's dark mode leaves alone.
_SECTION_RULE = (
    '<div style="margin:36px 0;height:1px;line-height:1px;font-size:1px;'
    f"mso-line-height-rule:exactly;background-color:{_BORDER};"
    f'background-image:linear-gradient({_BORDER},{_BORDER});">&nbsp;</div>'
)

ImageSource = Callable[[HighlightImage], str]


def _text_block(inner: str) -> str:
    """Wrap a run of text-only markup so Gmail's dark mode cannot dim it."""

    return f"{_TEXT_OPEN}{inner}{_TEXT_CLOSE}"


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


def highlights_question_url(issue: NewsletterIssue) -> str:
    site = issue.branding.course_site_url.rstrip("/")
    return f"{site}/?{urlencode({'q': HIGHLIGHTS_QUESTION})}"


def _in_sentence(name: str) -> str:
    """'The MAS.S60 teaching team' reads as 'the MAS.S60 teaching team' mid-sentence."""

    return f"the {name[4:]}" if name.startswith("The ") else name


def _selection_line(issue: NewsletterIssue) -> str:
    return f"How {_in_sentence(issue.branding.editor_name)} chooses what to highlight"


def _site_label(url: str) -> str:
    parts = urlsplit(url)
    return (parts.netloc + parts.path).rstrip("/") or url


def _project_line(issue: NewsletterIssue, link: ProjectLink) -> str:
    built = issue.built_for(link.project_id) or "Nothing posted for this week yet."
    site = f" {link.post_url or link.site_url}" if (link.post_url or link.site_url) else ""
    return f"{link.label}: {built}{site}"


def render_text(issue: NewsletterIssue) -> str:
    branding = issue.branding
    lines: list[str] = [
        f"{branding.newsletter_name.upper()} · ISSUE {issue.week.number:02d}",
        issue.body.headline,
        "",
        _window_label(issue),
        f"THE ASSIGNMENT: {issue.week.tutorial}",
        "",
        _MARKDOWN_LINK.sub(r"\1 (\2)", issue.body.editorial),
        "",
        f"— {branding.editor_name}",
        "",
        _RULE,
        "",
        "HIGHLIGHTS*",
        "",
    ]
    for index, highlight in enumerate(issue.body.highlights, start=1):
        link = issue.link_for(highlight.project_id)
        label = link.label if link else highlight.project_id
        lines.append(f"{index}. {highlight.headline} — {label}")
        lines.append(f"   {highlight.description}")
        if link and (link.post_url or link.site_url):
            lines.append(f"   Open it: {link.post_url or link.site_url}")
        lines.append("")
    lines.append(f"* {_selection_line(issue)}: {highlights_question_url(issue)}")
    lines.append("")
    lines.append("ALL THE OTHER BUILDS THIS WEEK")
    others = issue.other_projects()
    if others:
        lines.extend(f"- {_project_line(issue, link)}" for link in others)
    else:
        lines.append("- Everyone who posted made the highlights this week.")
    lines.extend(
        [
            "",
            _RULE,
            f'"{issue.quote.text}"',
            f"— {issue.quote.author}, {issue.quote.source}"
            + (f" ({issue.quote.url})" if issue.quote.url else ""),
            "",
            f"This newsletter was created by {branding.editor_name}.",
            "",
            _course_line(issue),
            f"Class website: {branding.course_site_url}",
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


def _prose_html(text: str) -> str:
    """Escape prose, turning only validated Markdown links into anchors."""

    parts: list[str] = []
    position = 0
    for match in _MARKDOWN_LINK.finditer(text):
        parts.append(escape(text[position : match.start()]))
        parts.append(_anchor(match.group(2), escape(match.group(1)), style=_UNDERLINED))
        position = match.end()
    parts.append(escape(text[position:]))
    return "".join(parts)


def _highlight_html(issue: NewsletterIssue, index: int, *, image_src: ImageSource) -> str:
    highlight = issue.body.highlights[index - 1]
    link = issue.link_for(highlight.project_id)
    label = escape(link.label if link else highlight.project_id)
    site_url = (link.post_url or link.site_url) if link else None
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
    text = _text_block(
        f'<p style="margin:0 0 8px 0;{_LABEL}">{index:02d} &middot; {label}</p>'
        f'<h2 style="margin:0 0 10px 0;font-family:{_SANS};font-size:24px;line-height:1.2;'
        f'font-weight:500;color:{_INK};">{escape(highlight.headline)}</h2>'
        f'<p style="margin:0;font-family:{_SANS};font-size:16px;line-height:1.55;'
        f'color:{_INK_SOFT};">{escape(highlight.description)}</p>'
        f"{open_link}"
    )
    return f'<div style="margin:0 0 48px 0;">{figure}{text}</div>'


def render_html(
    issue: NewsletterIssue,
    *,
    image_src: ImageSource | None = None,
    logo_src: str | None = None,
) -> str:
    branding = issue.branding
    source = image_src or relative_image_source(issue)
    highlights = "".join(
        _highlight_html(issue, index, image_src=source)
        for index in range(1, len(issue.body.highlights) + 1)
    )
    others = issue.other_projects()
    # Names that link read as links: the same mid-grey underline as every other link.
    name_style = f"{_LABEL}font-weight:600;{_UNDERLINED}"
    other_names = (
        "".join(
            '<p style="margin:0 0 12px 0;">'
            + (
                _anchor(link.post_url or link.site_url or "", escape(link.label), style=name_style)
                if (link.post_url or link.site_url)
                else f'<span style="{_LABEL}font-weight:600;color:{_INK};">'
                f"{escape(link.label)}</span>"
            )
            # A little air under the underlined name; clients without inline-block just wrap.
            + f'<br><span style="display:inline-block;margin-top:5px;font-family:{_SANS};'
            f'font-size:15px;line-height:1.5;color:{_INK_SOFT};">'
            + escape(issue.built_for(link.project_id) or "Nothing posted for this week yet.")
            + "</span></p>"
            for link in others
        )
        if others
        else f'<p style="margin:0;{_LABEL}">Everyone who posted made the highlights this week.</p>'
    )
    editorial = "".join(
        f'<p style="margin:0 0 18px 0;font-family:{_SANS};font-size:17px;line-height:1.65;'
        f'color:{_INK_SOFT};">{_prose_html(paragraph.strip())}</p>'
        for paragraph in re.split(r"\n\s*\n|\n", issue.body.editorial)
        if paragraph.strip()
    )
    attribution = f"&mdash; {escape(issue.quote.author)}, {escape(issue.quote.source)}"
    if issue.quote.url:
        attribution = _anchor(issue.quote.url, attribution, style=f"{_LABEL}text-decoration:none;")
    rule = _SECTION_RULE
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        # Mac Mail recolors a message with its own color filter to match its light or dark
        # setting, which turned the white text dark. Two guards, each enough on its own in
        # testing: declaring support for both schemes, and switching the filter off.
        '<meta name="color-scheme" content="light dark">'
        '<meta name="supported-color-schemes" content="light dark">'
        # Mail clients that honor a stylesheet get the scheme and a black ground; the inline
        # bgcolor attributes below carry the ground for the ones that strip <style>.
        f"<style>:root{{color-scheme:light dark;}}body,table,td{{background-color:{_GROUND};}}"
        f"a{{color:{_INK};}}{_APPLE_FILTER_OFF}</style>"
        f"<title>{escape(issue.subject)}</title></head>"
        f'<body bgcolor="{_GROUND}" style="-apple-color-filter:none;margin:0;padding:0;'
        f'background-color:{_GROUND};color:{_INK};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'bgcolor="{_GROUND}" style="background-color:{_GROUND};"><tr>'
        # The flat gradient is deliberate: Gmail's app-side dark mode inverts plain background
        # colors on dark emails but leaves gradient backgrounds alone.
        f'<td align="center" bgcolor="{_GROUND}" style="padding:40px 16px;'
        f'background-color:{_GROUND};background-image:linear-gradient({_GROUND},{_GROUND});">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
        f'bgcolor="{_GROUND}" style="max-width:600px;width:100%;background-color:{_GROUND};">'
        f'<tr><td bgcolor="{_GROUND}" style="text-align:left;background-color:{_GROUND};'
        f'background-image:linear-gradient({_GROUND},{_GROUND});color:{_INK};">'
        + (
            f'<img src="{escape(logo_src, quote=True)}" width="600" '
            f'alt="{escape(branding.newsletter_name)}" style="display:block;width:100%;'
            'max-width:600px;height:auto;margin:0 0 22px 0;">'
            if logo_src
            else ""
        )
        + _TEXT_OPEN
        + (
            f'<p style="margin:0 0 28px 0;{_LABEL}">Issue {issue.week.number:02d}</p>'
            if logo_src
            else f'<p style="margin:0 0 28px 0;{_LABEL}">{escape(branding.newsletter_name)} '
            f"&middot; Issue {issue.week.number:02d}</p>"
        )
        + f'<h1 style="margin:0 0 24px 0;font-family:{_SANS};font-size:34px;line-height:1.15;'
        f'font-weight:500;letter-spacing:-0.01em;color:{_INK};">{escape(issue.body.headline)}</h1>'
        f'<p style="margin:0 0 10px 0;{_LABEL}">{escape(_window_label(issue))}</p>'
        # The assignment is context for the issue, so it sits one step greyer than the headline.
        f'<p style="margin:0 0 6px 0;{_SECTION}color:{_MUTED};">The assignment</p>'
        f'<p style="margin:0 0 28px 0;font-family:{_SANS};font-size:20px;line-height:1.4;'
        f'font-weight:400;color:{_MUTED};">{escape(issue.week.tutorial)}</p>'
        f'<p style="margin:0 0 14px 0;{_SUBHEAD}">How the week went</p>'
        f"{editorial}"
        f'<p style="margin:-6px 0 0 0;{_LABEL}">&mdash; {escape(branding.editor_name)}</p>'
        f"{_TEXT_CLOSE}{rule}{_TEXT_OPEN}"
        f'<p style="margin:0 0 24px 0;{_SUBHEAD}">Highlights*</p>'
        f"{_TEXT_CLOSE}"
        f"{highlights}"
        # A footnote to the whole section, marked like the heading and set apart from the
        # last highlight, so it cannot read as that build's link.
        + _text_block(
            f'<p style="margin:0;{_LABEL}">*&nbsp;'
            + _anchor(
                highlights_question_url(issue),
                f"{escape(_selection_line(issue))} &rarr;",
                style=f"{_LABEL}{_UNDERLINED}color:{_MUTED};",
            )
            + "</p>"
        )
        + f"{rule}{_TEXT_OPEN}"
        f'<p style="margin:0 0 18px 0;{_SUBHEAD}">All the other builds this week</p>'
        f"{other_names}"
        f"{_TEXT_CLOSE}{rule}{_TEXT_OPEN}"
        f'<p style="margin:0 0 12px 0;font-family:{_SANS};font-size:21px;line-height:1.4;'
        f'font-weight:300;color:{_INK};">&ldquo;{escape(issue.quote.text)}&rdquo;</p>'
        f'<p style="margin:0;{_LABEL}">{attribution}</p>'
        f"{_TEXT_CLOSE}{rule}{_TEXT_OPEN}"
        # Colophon order: who made this issue, then whose course it is and where to find it.
        f'<p style="margin:0 0 26px 0;{_LABEL}">This newsletter was created by '
        f"{escape(branding.editor_name)}</p>"
        f'<p style="margin:0 0 10px 0;{_LABEL}">{escape(_course_line(issue))}</p>'
        '<p style="margin:0;">'
        + _anchor(
            branding.course_site_url,
            escape(_site_label(branding.course_site_url)),
            style=f"{_LABEL}{_UNDERLINED}",
        )
        + "</p>"
        f"{_TEXT_CLOSE}"
        "</td></tr></table></td></tr></table></body></html>\n"
    )
