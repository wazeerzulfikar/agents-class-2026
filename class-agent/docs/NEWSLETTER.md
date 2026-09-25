# The Class Runtime: instructor-run weekly newsletter

The newsletter is a Course Agent capability for instructors, and also a staff command. An
instructor can ask the Course Agent to draft, review, and send the week's issue, or run
`python -m course_server.newsletter` directly; both paths run the same deterministic pipeline. It
never runs for students, and nothing is emailed until the instructor approves it in the platform's
confirmation surface.

## Through the Course Agent

The `instructor-newsletter` skill (audience `instructors`) teaches the agent the workflow, and
platform code exposes three tools only to authenticated instructors:

| Tool | What platform code does |
| --- | --- |
| `instructor.draft_newsletter` | Starts a background draft job (about five minutes) for the last finished week, or a given week. Returns a job id; the agent tells the instructor to ask again shortly. |
| `instructor.newsletter_status` | Reports the latest or a named job, and once it is done returns the draft: headline, editorial, highlights with students and reasons, the closing quote, the scoreboard, and the HTML/PDF/text file paths. The agent presents this faithfully and never rewrites the copy. |
| `instructor.send_newsletter` | Freezes a recipient snapshot (every active student account plus `NEWSLETTER_RECIPIENTS`, or the named test addresses) and emits `instructor.newsletter.confirmation_requested`. The web app shows a Send or Cancel card. |

Send marks the issue `approved` through
`POST /api/v1/conversations/{conversation_id}/newsletter/{issue_id}/confirmation`, which checks the
instructor login, conversation ownership, and the confirmation id; the mail worker then delivers the
approved issue to its snapshot on its next cycle and marks it `sent`. Cancel returns the issue to
`draft`. The agent does not choose the highlights, write the copy, or pick the recipients; those
remain the pipeline's and the instructor's decisions. Job records live under
`var/newsletter/jobs/` and issues carry their approval snapshot, so the status is inspectable from
files as well as from the agent.

## From the command line

## What an issue contains

Every issue follows the same skimmable shape:

1. A punny headline about the week's assignment, the week's dates, the assignment itself, and an
   editorial of up to 150 words on how the class did against what was taught. The editorial call
   receives the text of that week's slide deck from `shared/course/slides/week-NN/` and the
   syllabus learning goals alongside the anonymized staff notes on every submission, and writes
   two paragraphs: what went well and which lecture ideas the class absorbed, then the blind spots
   (lecture ideas the submissions missed or misapplied) and common blockers, framed as what to
   practice next. It never names a student, never counts who submitted, and never mentions the
   featured projects; code rejects names, participation tallies, overlong copy, and dense prose
   (average sentence over 18 words, any sentence over 26, semicolons, or a Flesch reading ease
   below 50), feeding the exact problem back to the model. Build references are linked to the
   student's site. It may add up to two external reference links in Markdown form; each URL is
   checked to resolve before it is accepted.
2. Four highlights (configurable). Each one shows an image from the student's own post, names
   the student, and gives a headline plus two sentences: what the build is and what makes it
   interesting, then specifically how it does what the assignment asked. A link opens the site.
3. Every other student who posted work that week, with one sentence on what they built and a
   link to their site. Students with nothing beyond the starter template are left off the list.
4. A closing quote. Preferably a line from a student's own post that week, featured or not: the
   scoring pass asks each project for up to three sentences with personality (surprising, funny,
   candid, vivid; never a definition), code verifies each appears verbatim in that student's
   collected prose, and the editorial call picks the one with the most personality, attributed and
   linked to the student's site. When no verified candidate exists, a curated quote from an AI or computing
   pioneer is used instead, rotated across issues.
5. The course line (`MAS.S60 · AI Agents for Cognitive Augmentation · MIT, Fall 2026`) and a link
   to the class website, `https://cognitive-agents.media.mit.edu`.

The email subject is `The Class Runtime from MAS.S60`. Each message is sent as plain text with an
HTML alternative. The HTML follows the course site's look: black ground, ivory Helvetica display
text, small letter-spaced monospace labels, muted secondary text, and fine rules.

## How highlights are chosen

Every active project is scored, one model call each, against a fixed rubric from its bounded
evidence: `interest` (how interesting the idea and the result are), `execution` (complete,
working, documented), and `goal_fit` (did the student properly do what the week asked). The same
call produces the staff notes reused everywhere else: one sentence on what the student built, what
went well, and what they struggled with. The editorial and headline are written from those notes
across the whole class, so the model reads every submission before it writes a word. Platform
code computes the total (goal fit 40%, interest 40%, execution 20%), ranks every project that met
the goal-fit floor ahead of every project that did not, excludes students featured in the last
`NEWSLETTER_HIGHLIGHT_COOLDOWN_ISSUES` sent issues, breaks ties deterministically, and takes the
top `NEWSLETTER_HIGHLIGHT_COUNT`. The model then writes copy for exactly those projects in that
order; a response that changes the set or order is re-prompted once and then rejected. The full
scoreboard with rationales is stored in the issue and printed by `draft` and `show`, so the choice
is inspectable.

## Highlight images

For each featured build the finder opens the student's site in headless Chromium (the same
Playwright dependency the agent's browser uses; `BROWSER_EXECUTABLE_PATH` is honored when it
exists), follows same-site links that name the week to the student's post, and measures the visual
elements rendered there: images, SVG figures, canvases, and videos. Visible elements at least 300
by 160 CSS pixels with a sane aspect ratio are candidates, largest first, and each is captured at
2x as its own element screenshot, so vector figures and live canvases work as well as photos. The
model picks the capture that best represents the build (rendered results, demos, diagrams over
logos, icons, and portraits). The chosen capture is flattened onto black, downscaled to 1200 pixels
wide, encoded as JPEG, stored beside the issue with its source and page URL, and embedded in the
email as an inline `cid:` image. Root-page visuals are considered only when the site has no week post. When a post
has no usable visual, the week page (or, on single-page sites, the week section) is captured
instead and recorded as a screenshot. A site that cannot
be inspected is skipped and noted in the draft log; the highlight is still written.

## Where each decision lives

| Decision | Owner |
| --- | --- |
| Which week is "last week", and its assignment goal | Platform code, parsed from `shared/course/schedule/schedule.md` |
| Which repositories exist and what can be read | The existing read-only GitHub catalog (`mitmedialab/agents2026-*`) |
| Which projects are eligible to be featured | Platform code: the project changed something during the week and was not featured in the last `NEWSLETTER_HIGHLIGHT_COOLDOWN_ISSUES` sent issues |
| Rubric scores and notes per project, the editorial, the prose, the image choice among measured candidates | The configured model, from bounded evidence |
| Weights, goal-fit floor, ranking, cooldown, final selection and order | Platform code (`score.py`) |
| Highlight set/order, editorial length, brevity limits, link-free prose, no staff vocabulary ("brief", "rubric", "score") | Validated in code; a violating response is re-prompted once with the concrete problems, then rejected |
| Links, project list, quote, footer, HTML escaping, image fetching and re-encoding | Platform code; the model cannot add links, addresses, or image URLs |
| Whether anything is emailed, and to whom | The instructor, at `send` time |

The model receives only repository names, derived labels, deployed site URLs, commit subjects,
bounded Markdown/text documents from `weekly_builds/weekNN/`, and bounded deployed site text.
It does not receive credentials, student email addresses, or account data.

## Data sources and bounds

For each course repository the collector reads, through the existing catalog:

- the recursive tree, to count files under `weekly_builds/weekNN/` and `website/`;
- up to six `.md`, `.txt`, or `.rst` documents from that week's folder, shallowest and README
  first, each capped at 4,000 characters and 9,000 characters per project;
- the 30 most recent commits, filtered to the week's window (class day through the day before the
  next class, in `NEWSLETTER_TIMEZONE`);
- the deployed site's readable text, capped at 2,500 characters, using the same public-network
  fetch rules as the agent's web tools.

Credential-like paths are refused by the catalog before any content is requested. One unreadable
repository or site is noted in the evidence and does not stop the draft.

## Commands

```bash
# Draft the most recently finished week (run from class-agent/ with .env configured).
uv run python -m course_server.newsletter draft

# Draft a specific week, or pretend today is a given date when choosing the week.
uv run python -m course_server.newsletter draft --week 1
uv run python -m course_server.newsletter draft --as-of 2026-09-22

# Review a stored issue again, as text or HTML, or export a single-page PDF to share.
uv run python -m course_server.newsletter show 2026-week01
uv run python -m course_server.newsletter show 2026-week01 --html
uv run python -m course_server.newsletter pdf 2026-week01
uv run python -m course_server.newsletter list

# Send yourself a test copy; the issue stays a draft.
uv run python -m course_server.newsletter send 2026-week01 --test-to you@mit.edu

# Approve and send. Recipients are NEWSLETTER_RECIPIENTS plus --to plus, optionally, every
# active student account. The command prints the full text and recipient list, then waits
# for you to type SEND. Pass --yes only in a scripted context.
uv run python -m course_server.newsletter send 2026-week01 --to-active-students
uv run python -m course_server.newsletter send 2026-week01 --to list@example.edu --yes
```

If the draft is not right, edit nothing by hand; run `draft --week N` again to regenerate it. A week
that was already sent is refused unless `--force` is passed, which produces a fresh draft.

Drafting requires `GITHUB_STUDENT_PROJECTS_ENABLED=true`, a read-only `GITHUB_TOKEN`, and the
model settings used by the agent. Sending requires the same `MAIL_*` settings as the mail worker;
`--to-active-students` also requires `DATABASE_URL`. `show` and `list` need neither.

Like the other CLIs, the command loads `.env` without overriding variables already exported in
the shell. If a stale `OPENAI_API_KEY` is exported, the model request fails with
`AuthenticationError`; run `unset OPENAI_API_KEY` first so the `.env` value is used.

## Storage

`NEWSLETTER_DATA_PATH` (default `var/newsletter/`, ignored by Git) holds `issues/<year>-weekNN.json`
plus `.txt`, `.html`, and `.pdf` renderings for review (the PDF is one tall page rendered by headless
Chromium, produced after each draft and on demand with `pdf`), and
`issues/<year>-weekNN/<project>.jpg` images that the `.html` preview references relatively, so it
opens correctly in a browser. The JSON record stores the week, the validated
model copy, the project roster with links, the quote, status (`draft` or `sent`), and one delivery
row per recipient with the provider message ID or the sanitized error class. Sent issues are the
source of the fairness rule and of quote rotation, so keep this directory in staff backups.

Each recipient receives a separate message; other recipients' addresses are never included.
Provider acceptance is recorded per recipient. A failed recipient is recorded and the issue stays a
draft only if nobody received it; re-running `send` on a sent issue is refused.

## Cost and failure behavior

Each draft makes one scoring request per active project, one editorial request, one image-choice
request per featured build with candidates, and one highlight-copy request (each text request is
retried once if the response breaks a rule), plus a few
hundred GitHub reads across the class and one headless browser session per featured build. The `draft` command prints the total requests and input/output tokens it used; a week-1 run with 27 active projects used about 32 requests, 78,000 input tokens, and 11,000 output tokens, roughly $0.30 at GPT-5.6 Terra prices. Provider failures
surface as sanitized errors without response bodies. No newsletter code runs inside the API
process or the Course Agent runtime.
