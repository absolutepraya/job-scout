---
name: job-watcher
description: Daily sweep of LinkedIn, Indeed, Glassdoor, Google Jobs for fresh postings matching Abhip's target roles. Dedupes against state.db, posts only new matches to Telegram. Use when user asks to "check jobs", "run job watcher", or when scheduled cron fires.
---

# Job Watcher

Watches 4 job boards (LinkedIn, Indeed, Glassdoor, Google Jobs) for new postings matching configured queries. Runs daily via Hermes cron at 08:00 WIB. Deduplicates against `state.db` and only emits postings unseen on prior runs.

## How it works

1. Reads `config/queries.yaml` for search terms + locations
2. For each (query, location) pair, calls JobSpy with karakeep residential proxy
3. Filters out postings already in `state.db` (SQLite, keyed by canonical job URL)
4. Inserts new postings into `state.db`
5. Formats remaining new postings into a Telegram-ready digest
6. Returns the digest as final message (Hermes pipes it to the cron's delivery channel)

## Files

- `bin/run.py` — main entry point, callable as `python -m job-watcher` or via the bin alias
- `config/queries.yaml` — queries, locations, and filters (edit this to change what's watched)
- `state.db` — SQLite, auto-created on first run (~10KB per 1000 postings)
- `.last_run` — timestamp file for tracking

## Manual invocation

```bash
# Dry-run (print only, don't update state)
~/.agents/skills/job-watcher/bin/run.py --dry-run

# Force-rescan a single board
~/.agents/skills/job-watcher/bin/run.py --site linkedin

# Reset state (re-emit all postings on next run — useful after editing queries)
rm ~/.agents/skills/job-watcher/state.db
```

## Tuning notes

- **hours_old: 72** — captures last 3 days, safe for daily cron with occasional misses
- **LinkedIn limit per query**: 25 (stays under throttle threshold of ~10 pages)
- **Indeed limit per query**: 50 (no rate limit, can pull more)
- **Proxy is required for LinkedIn**: without it, datacenter VPS IP gets banned in ~5 requests
