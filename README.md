# job-agent

Watches LinkedIn for freshly posted jobs that fit you, scores each one against
your resume, and pops a desktop notification for the good ones.

Default search: **"Python Developer"** and **"FastAPI Developer"**, posted in the
past 24 hours, Entry + Mid-Senior level, under 10 applicants, title mentioning
python / fastapi / django. Everything is configurable from the command line.

## How it works

```
every 5 min ──▶ open Chromium (your logged-in profile)
              ──▶ run each keyword search, page by page
              ──▶ keep jobs whose title matches, skip ones seen in earlier runs
              ──▶ open the detail pane of each NEW job (applicants, description)
              ──▶ score it 0-100 against resume.json
              ──▶ save data/jobs/*.json + *.csv, remember the ids in seen_jobs.json
              ──▶ desktop notification for new jobs scoring ≥ 50
```

| File | Purpose |
|------|---------|
| `app/main.py` | Entry point: CLI, the 5-minute `--watch` loop, wiring |
| `app/config.py` | Search criteria, defaults, LinkedIn URL building, paths |
| `app/browser.py` | Launches Chromium on the persistent (logged-in) profile |
| `app/linkedin.py` | Scrapes the results list and each job's detail pane |
| `app/matcher.py` | Scores a job against `resume.json` (skills, title, years) |
| `app/notify.py` | Desktop notifications (`notify-send` on Linux, `osascript` on macOS) |
| `app/report.py` | Colourful terminal output (`rich`): scan log lines and the per-scan report |
| `app/scan.py` | One scan (search → score → save → notify), shared by the CLI and the service |
| `app/api.py` | FastAPI app: JSON API + serves the dashboard |
| `app/service.py` | Runs scans for the API: one at a time in a worker thread, on a timer and on demand |
| `app/settings.py` | Service settings from `JOB_AGENT_*` environment variables |
| `app/web/index.html` | The dashboard (plain HTML + JS, talks to `/api/*`) |
| `app/models.py` | The `Job` record and small text parsers |
| `app/storage.py` | JSON/CSV output and the seen-jobs store |
| `resume.json` | Your profile: target titles, core and secondary skills, when your experience started |
| `data/browser/` | Chromium profile with your LinkedIn session (never share it) |
| `data/jobs/` | Results: `jobs_<stamp>.json/.csv`, `latest.json`, `seen_jobs.json`, and `jobs_db.json` (every job ever found, for the dashboard) |
| `logs/` | `job_agent.log`, rotated daily |

## Setup (once)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Linux desktop notifications need libnotify (usually already present):
notify-send "test" "hello" || sudo apt install libnotify-bin

# Sign in to LinkedIn once, in the window that opens, then press Enter:
python -m app.main --keep-open
```

Then edit `resume.json` if you want to change the skills/titles used for
scoring. Aliases are the words a job ad might use for that skill.
`experience_since` (`"2024-05"`) is the month your first job started; your years
of experience are computed from it on every run, so they keep growing.

## Run

```bash
source venv/bin/activate

# one check, browser visible, prints and saves every match
python -m app.main

# keep checking every 5 minutes with no browser window (Ctrl+C to stop)
python -m app.main --watch --headless

# tune it
python -m app.main --watch --headless --interval 600 --min-score 60
python -m app.main --keywords "Django Developer" --keywords "Backend Engineer" --title-keyword django --title-keyword backend
python -m app.main --location "Bengaluru"
python -m app.main --years-tolerance 0.5        # stricter: skip jobs asking > 0.5 yr more than you have
python -m app.main --experience-level entry_level --experience-level associate   # LinkedIn's own level filter
python -m app.main --refresh-seen          # re-open and re-score jobs already seen (after editing resume.json)
python -m app.main --no-details            # faster: titles only, no applicant counts or scoring
python -m app.main --help                  # everything else
```

To keep it running after you close the terminal:

```bash
nohup venv/bin/python -m app.main --watch --headless > /dev/null 2>&1 &
```

(For a proper background service use a `systemd --user` unit; it must export
`DISPLAY` and `DBUS_SESSION_BUS_ADDRESS` so `notify-send` can reach your desktop.)

## Web dashboard and API (FastAPI)

```bash
source venv/bin/activate
uvicorn app.api:app --port 8000
```

Open <http://localhost:8000>. The service scans every 5 minutes on its own
(headless, one scan at a time) and sends the same desktop notifications as the
CLI. The page shows every job ever found, best score first, with clickable
links, filters (min score, only new, hide too-senior, search), a **Scan now**
button, an editor for `resume.json`, and the scan history. Interactive API
docs are at <http://localhost:8000/docs>.

| Endpoint | |
|---|---|
| `GET /api/status` | scanner state, last scan, next scan, profile, settings |
| `POST /api/scan` | start a scan now (`409` if one is running) |
| `GET /api/jobs?min_score=50&only_new=true&include_skipped=false&q=fastapi` | jobs, best first |
| `GET /api/jobs/{id}` | one job with its description |
| `GET /api/profile` · `PUT /api/profile` | read / replace `resume.json` (validated first) |
| `GET /api/scans` | scans run since the service started |

Settings are environment variables (defaults shown):

```bash
JOB_AGENT_INTERVAL=300 JOB_AGENT_AUTO_SCAN=1 JOB_AGENT_HEADLESS=1 JOB_AGENT_MIN_SCORE=50 \
JOB_AGENT_KEYWORDS="Python Developer,FastAPI Developer" JOB_AGENT_MAX_PAGES=10 JOB_AGENT_NOTIFY=1 \
JOB_AGENT_YEARS_TOLERANCE=1 JOB_AGENT_RESUME=resume.json JOB_AGENT_VERBOSE=0 uvicorn app.api:app --port 8000
```

The CLI and the service share the scanner and the browser profile, so run one
or the other, not both. Sign in to LinkedIn with the CLI first
(`python -m app.main --keep-open`). Note that the API layer is for convenience,
not speed: a scan takes as long as LinkedIn takes to serve the pages either way.

## The match score

| Part | Points | How |
|------|--------|-----|
| Title | 0–20 | title contains one of `target_titles` |
| Core skills | 0–45 | share of `core_skills` mentioned in title + description |
| Secondary skills | 0–20 | number of `secondary_skills` mentioned (6 = full marks) |
| Experience | 0…+15 | years the ad asks for vs yours: within your years +15, up to `years_tolerance` more +10, unstated +8 |

**Experience is also a hard gate.** An ad asking for more years than you have
plus `years_tolerance` is marked `experience_fit = false`, capped at
25 points, never notified, and listed under *Skipped* in the terminal. Years
are read from phrases like "3+ years of experience", "2-4 yrs", "7+ yoe" in the
title and description; an ad that states no years is not skipped.
`years_tolerance` is `0` in `resume.json`: with 2.4 years, ads asking "2+ years",
"1-3 years" or "0-2 years" pass and "3+ years" is skipped. Set it to `1` (or run
with `--years-tolerance 1`) to also accept ads asking one year more than you have.

Verdicts: **strong** ≥ 70, **good** ≥ 50, **weak** ≥ 30, else **poor**. An ad
whose description never mentions Python/FastAPI/Django is capped at 30. Each
saved job lists the skills it matched, the core skills it never mentioned, and
the years it asked for, so you can see *why* it scored what it did.

The terminal report after each scan has three groups: **Notified** (new, fits
your experience, score ≥ `--min-score`), **Other matches**, and **Skipped**.
Links are underlined blue and clickable in most terminals. Set `NO_COLOR=1` to
turn colour off.

Only jobs not seen before are opened, scored and announced. Delete
`data/jobs/seen_jobs.json` to start over.

## Exit codes

`2` browser could not launch (profile already open?) · `3` LinkedIn wants a sign-in
(`--keep-open`) · `4` LinkedIn changed its results layout · `5` `resume.json`
invalid · `6` `--watch` gave up after 5 failed checks in a row.
