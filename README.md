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
                  (a detail pane that failed to load is retried on the next scans)
              ──▶ desktop notification for new jobs that fit your experience and score ≥ 50
                  (≥ 70 when the ad states no years)
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
| `app/scan.py` | One scan (search → score → save → notify) |
| `app/models.py` | The `Job` record and small text parsers |
| `app/storage.py` | JSON/CSV output, the seen-jobs store and the detail-pane retry counter |
| `tests/` | Unit tests (`python -m unittest discover -v`); `test_matcher.py` holds the real phrases the years parser must read |
| `resume.json` | Your profile: target titles, core and secondary skills, when your experience started |
| `data/browser/` | Chromium profile with your LinkedIn session (never share it) |
| `data/jobs/` | Results: `jobs_<stamp>.json/.csv`, `latest.json`, `seen_jobs.json`, `pending_details.json` (panes still to retry) |
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
python -m app.main --unknown-years-min-score 50 # notify ads that state no years at the normal bar (default 70; 101 = never)
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
are read from phrases like "3+ years of experience", "Experience: 5-8 years",
"8 - 9 Years", "minimum 5 years", "between 3 and 5 years" or "7+ yoe" in the
title and description. When an ad states several numbers, the first one in the
text decides, except that an explicitly labelled one ("Experience:", "Minimum",
"Total …") beats an unlabelled one — so "1-3 years of Mendix" cannot override
"3-5 years of software engineering". The deciding phrase is quoted in the job's
notes, e.g. `asks 5+ yrs («Experience: 5-8 years»)`.

An ad that states no years is not skipped, with two exceptions: a title saying
Senior / Sr / Lead / Principal / Staff / Architect / Head of / Manager / Director
is treated as asking for more than you have, and any other such ad is only
notified when it scores at least `--unknown-years-min-score` (default 70).
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

Only jobs not seen before are opened and announced. Every job found is scored;
one whose detail pane did not load is scored from its title alone (its notes say
so), is not announced yet, and its pane is retried on the next scans — after 3
failed attempts it is remembered as is. Delete `data/jobs/seen_jobs.json` and
`pending_details.json` to start over.

## Tests

```bash
venv/bin/python -m unittest discover -v
```

`tests/test_matcher.py` is the regression corpus for the years parser: every
phrase in it came from a real posting, with the number of years it asks for.
When a job is scored wrongly, add its phrase there first.

## Exit codes

`2` browser could not launch (profile already open?) · `3` LinkedIn wants a sign-in
(`--keep-open`) · `4` LinkedIn changed its results layout · `5` `resume.json`
invalid · `6` `--watch` gave up after 5 failed checks in a row.
