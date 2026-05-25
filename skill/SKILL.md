---
name: job-watcher
description: Daily sweep of LinkedIn, Indeed, Glassdoor, and Google Jobs for fresh postings matching configured queries. Dedupes against state.db, scores each posting against a configurable rubric, and posts only new matches to Discord. Use when the user asks to "check jobs", "run job watcher", or when scheduled cron fires.
---

# job-watcher (skill half of job-scout)

Watches 4 job boards for new postings matching `config/queries.yaml`. Runs daily via system cron at 08:00 WIB. Deduplicates against `state.db` and only emits postings unseen on prior runs.

## How it works

1. Reads `config/queries.yaml` for search terms + locations + filters
2. For each (query, location) pair, calls JobSpy via a residential proxy
3. Filters by title blacklist, company blacklist, and workmode (drops abroad/remote when configured)
4. Dedupes against `state.db.seen_v2` keyed on `f"{title.lower()}|{company.lower()}"`
5. Scores each new posting via `scoring.compute_fit_score` (geo, freshness, stack match, niche AI keywords, intern-program signals)
6. Summarizes the description into bullet points via `summarize.py` (calls `hermes -z PROMPT` against Copilot gpt-5.4)
7. Formats one Discord card per posting and delivers via `notify.py`

## Files

- `bin/run.py` — main entry point (scrape → filter → dedupe → score → summarize → deliver)
- `bin/scoring.py` — pure scoring logic, with unit tests
- `bin/summarize.py` — description → bullets via the local `hermes` CLI
- `bin/notify.py` — Discord delivery (bot token or webhook)
- `bin/migrate_state_db.py` — one-shot SQLite schema bootstrap
- `bin/test_scoring.py` — pytest suite (24 cases)
- `config/queries.yaml` — queries, locations, filters, score gate
- `state.db` — runtime SQLite (auto-created; gitignored)

## Manual invocation

```bash
# Dry-run — print to stdout, don't update state, don't post
~/.agents/skills/job-watcher/.venv/bin/python ~/.agents/skills/job-watcher/bin/run.py --dry-run

# Restrict to one site for debugging
~/.agents/skills/job-watcher/.venv/bin/python ~/.agents/skills/job-watcher/bin/run.py --dry-run --site linkedin

# Reset state (re-emit all postings on next run)
rm ~/.agents/skills/job-watcher/state.db
```

## Tuning knobs (`config/queries.yaml`)

- `hours_old` — max age in hours JobSpy will return (168 = 1 week)
- `min_score` — delivery threshold; postings under this don't reach Discord
- `workmode_blacklist` — `[abroad, remote]` drops anything that isn't onsite-local
- `title_must_match_any` — hard requirement (e.g. `intern`, `internship`, `magang`)
- `title_must_not_match` — substring noise blacklist
- `company_must_not_match` — spam-mill blacklist
- `limits` — per-site result cap per query
- `queries` — search terms
- `locations` — `name` + optional `country_indeed` per JobSpy

## Notes

- **LinkedIn limit per query**: 25 (stays under throttle threshold of ~10 pages)
- **Indeed limit per query**: 50 (no rate limit, can pull more)
- **Glassdoor**: returns "not available for INDONESIA" — kept in the rotation in case it changes upstream
- **Proxy is required for LinkedIn** — datacenter IPs get banned in ~5 requests without a residential proxy. See `bin/run.py:_read_karakeep_proxy` or set `CRAWLER_HTTPS_PROXY` in `~/.hermes/.env`
