# job-scout — agent guide

Working notes for any AI agent editing this repo. The human-facing intro is in [README.md](./README.md).

## Two halves, two runtimes

This repo bundles **two cooperating processes** that share one SQLite file:

| Folder | Runtime | Responsibility |
|---|---|---|
| `skill/` | Direct Python via cron (08:00 WIB daily) | Scrape, score, dedupe, summarize, post the daily digest |
| `plugin/` | Hermes Agent gateway (`hermes-gateway` systemd service) | `/job` slash command, button dashboard, modal Add-Manual, triage table maintenance |

They are NOT importable from one another. The cron `skill/bin/run.py` does not talk to Hermes; the plugin does not invoke the scraper. They communicate through the shared SQLite file at `~/.agents/skills/job-watcher/state.db` (which is the symlink target → `skill/state.db`).

If you need a function in both halves, the established pattern is **`sys.path` injection from the plugin into `skill/bin/`** — see `plugin/dashboard.py` (`_JW_BIN = Path.home() / ".agents" / "skills" / "job-watcher" / "bin"`) and `plugin/db.py`. Do not copy code; import it.

## Deploy model (read this before editing)

The repo is the **source of truth**. On VPS, runtime dirs are symlinks back to this clone:

```
~/.agents/skills/job-watcher       → ~/job-scout/skill
~/.hermes/plugins/job-manage-commands → ~/job-scout/plugin
```

To ship a change: edit on Mac → push → `ssh vps 'cd ~/job-scout && git pull'` → `sudo systemctl restart hermes-gateway` (only if `plugin/` changed; `skill/` is reloaded fresh on each cron tick).

`install.sh` (at the repo root) creates the symlinks. It refuses to clobber existing real directories — that's intentional, to protect a live `state.db`.

## SQLite schema (the contract you must not break)

`state.db` has two tables. The dedup contract holds the whole system together.

### `seen_v2` (scrape history, written by `skill/`)
Columns: `dedup_key TEXT PRIMARY KEY, job_url, title, company, location, site, first_seen, query_term, fit_score, is_local, exp_bullets, jobdesc_bullets`.

**Dedup key format** — `f"{title.lower().strip()}|{company.lower().strip()}"`. This is what lets a posting that appears in LinkedIn AND Indeed AND across 5 city URLs collapse to one row. Both `skill/bin/run.py:dedup_key` and `plugin/interactions.py` (the manual-Add modal handler) must compute it the same way. Don't change one without the other.

### `triage` (saved/applied/dismissed, written by `plugin/`)
Columns: `dedup_key TEXT PRIMARY KEY, job_url, title, company, location, score, workmode, status (saved|applied|dismissed), saved_at, applied_at, dismissed_at, notes`.

Same `dedup_key`, so a scrape and a save can join. Use `plugin/db.py:upsert_triage`, never raw SQL.

Idempotent migrations run on every `open_db()` call (`plugin/db.py:_migrate_seen_v2`, `skill/bin/run.py:_migrate_seen_v2` — they're duplicated on purpose since they can't import each other; keep them in sync when adding columns).

## Score formula

Implemented in `skill/bin/scoring.py:compute_fit_score`. Updated 2026-05-26 rebalance.

| Component | Max | Rule |
|---|---|---|
| Geo | 35 | Indonesia = 35 baseline. Else 30 (SG/MY), 25 (JP/TW/CN/HK/KR), 20 (UK/EU), 15 (CA/AU/NZ), 8 (US) |
| Freshness | 25 | `<5d`=25 · `<10d`=18 · `<20d`=12 · `<30d`=6 · older=0 |
| Stack | 30 | +3 per word-boundary regex match on title+description (caps at 30). See `STACK_KEYWORDS`. |
| Niche title | 15 | +15 if title hits any of `NICHE_TITLE_KEYWORDS` (AI engineer, ML engineer, LLM engineer, etc.) |
| Intern desc | 5 | +5 if description contains "entry level" / "early career" / "intern program" / "internship program" / "graduate program" |

Company-tier scoring was **dropped** in the 2026-05-26 rebalance. The function still exists for callers but `compute_fit_score` no longer sums it.

**Delivery gate**: `config/queries.yaml:min_score` (default 40). Rows below it don't reach Discord.

**Live recount**: `plugin/dashboard.py:_live_score` recomputes the freshness component at render time using `first_seen` as a `date_posted` proxy. The stored `fit_score` has the +25 freshness max baked in; `_live_score` subtracts that and adds the live-derived value. If you change the freshness tier values in `scoring.py`, mirror them in `_live_score` and update the assumed-baked constant (25).

## Discord conventions

- Button `custom_id` format: `jm:<action>:<arg1>[:<arg2>...]` — namespace `jm:` is mandatory; the dispatcher in `plugin/interactions.py:_on_interaction` routes by `action`.
- Discord rejects messages with duplicate `custom_id` even on disabled buttons. Boundary pagination buttons use `jm:noop:<view>:<dir>` to stay unique; the handler defers silently. Don't reintroduce shared `custom_id`s when adding new pagination.
- Color tiers (in `skill/bin/run.py` + `plugin/interactions.py`, must stay in sync):
  - HIGH `#377AAF` for `score >= 70`
  - MED `#3BBBB3` for `50–69`
  - LOW `#79DC96` for `< 50`
  - RED `#F93827` after Dismiss click
  - BLUE `#2C5EAD` after Applied click

## Don't

- Don't commit `state.db` or `*.bak.*` (`.gitignore` covers them — verify before pushing).
- Don't hardcode channel IDs or tokens. All Discord IDs come from env vars at `~/.hermes/.env` (loaded via `skill/bin/notify.py:_load_env_from_file`).
- Don't change `dedup_key` formatting in one half without the other — it's the join key for everything.
- Don't import across `skill/` ↔ `plugin/` directly. Use the `sys.path` injection pattern already in `plugin/dashboard.py` / `plugin/db.py`.
- Don't add backwards-compat shims for missing columns. Use the migration pattern in `_migrate_seen_v2`.
- Don't break the no-em-dash rule in user-facing copy (Discord card descriptions, embed titles). Em dashes leak the AI tone.

## Testing

- `cd skill/bin && ../../skill/.venv/bin/python -m pytest test_scoring.py -v` (24 scoring unit tests, fast)
- `cd plugin && python -m pytest test_*.py -v` (commands + config + url helpers)
- No e2e tests. Live verification happens via `/job` in Discord and watching the 08:00 cron.

## Common operations

- Dry-run scrape (no DB writes, no Discord post): `~/job-scout/skill/.venv/bin/python ~/job-scout/skill/bin/run.py --dry-run --site linkedin`
- Reset scrape history (re-deliver everything on next cron): `rm ~/job-scout/skill/state.db` (also drops `triage` — careful)
- Tail cron log: `tail -100 ~/.logs/job-watcher.log`
- Hermes plugin reload: `sudo systemctl restart hermes-gateway` then `grep "attached on_interaction" ~/.hermes/logs/agent.log | tail -1` to confirm fresh listener attach.
