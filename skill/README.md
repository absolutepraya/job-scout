# skill — the cron half

Daily scraper for job-scout. Runs as a standalone Python process invoked by system cron at 08:00 WIB. For the full project overview see the [root README](../README.md); this doc covers operating just the skill.

## Architecture

| File | Role |
|---|---|
| `bin/run.py` | Entry point. Scrape → filter → dedupe → score → summarize → deliver. |
| `bin/scoring.py` | Pure `compute_fit_score`. 24 unit tests cover the formula. |
| `bin/summarize.py` | Per-posting Experience / Job desc bullets via the local `hermes` CLI. |
| `bin/notify.py` | Discord delivery (bot token or webhook). Loads `~/.hermes/.env`. |
| `bin/migrate_state_db.py` | One-shot SQLite schema bootstrap for fresh installs. |
| `bin/test_scoring.py` | Pytest suite for the scoring rubric. |
| `config/queries.yaml` | Queries, locations, filters, score gate. |
| `state.db` | Runtime SQLite — auto-created, gitignored. |

## Setup

Run `install.sh` from the repo root — it builds the venv via `uv`. If you want to do it by hand:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv $REPO_ROOT/skill/.venv
uv pip install --python $REPO_ROOT/skill/.venv/bin/python -r $REPO_ROOT/requirements.txt
```

Then put delivery credentials in `~/.hermes/.env`:

```
DISCORD_BOT_TOKEN=...
DISCORD_JOB_WATCHER_CHANNEL=<channel_id_for_daily_digest>
DISCORD_JOB_HEARTBEAT_CHANNEL=<channel_id_for_zero_new_days>
CRAWLER_HTTPS_PROXY=http://user:pass@host:port
```

(The proxy is required for LinkedIn — datacenter IPs get banned in ~5 requests without a residential one.)

Cron entry (`TZ=Asia/Jakarta` assumed at the top of the crontab):

```
0 8 * * * $HOME/.agents/skills/job-watcher/.venv/bin/python $HOME/.agents/skills/job-watcher/bin/run.py >> $HOME/.logs/job-watcher.log 2>&1
```

## Common operations

```bash
# Dry-run — don't update state.db, don't post
$HOME/.agents/skills/job-watcher/.venv/bin/python \
  $HOME/.agents/skills/job-watcher/bin/run.py --dry-run

# Restrict to one site for debugging
$HOME/.agents/skills/job-watcher/.venv/bin/python \
  $HOME/.agents/skills/job-watcher/bin/run.py --dry-run --site linkedin

# Reset state (re-deliver everything on next run)
rm $HOME/.agents/skills/job-watcher/state.db

# Count rows in seen_v2
sqlite3 $HOME/.agents/skills/job-watcher/state.db "SELECT count(*) FROM seen_v2;"

# Tail today's log
tail -100 $HOME/.logs/job-watcher.log

# Re-run scoring tests
cd $HOME/.agents/skills/job-watcher/bin && \
  ../.venv/bin/python -m pytest test_scoring.py -v
```

## Configuration knobs (`config/queries.yaml`)

| Field | Description |
|---|---|
| `hours_old` | JobSpy `hours_old` window. `168` = 1 week. |
| `min_score` | Delivery threshold (default `40`). Rows under this don't reach Discord. |
| `workmode_blacklist` | List of workmodes to drop. Options: `onsite`, `remote`, `abroad`. |
| `title_must_match_any` | Hard requirement — title must contain one of these (case-insensitive). |
| `title_must_not_match` | Noise blacklist. Substring match, case-insensitive. |
| `company_must_not_match` | Spam-mill blacklist. Substring match, case-insensitive. |
| `limits` | Per-site result cap per query (tier-1 locations). |
| `limits_tier2` | Per-site cap for tier-2 locations (slimmer, since usually remote+oversea). |
| `queries` | Search terms — one JobSpy call per `(query, location, site)` triple. |
| `locations` | `[{name, country_indeed?, is_remote?, tier?}]`. `tier:1` uses all sites; `tier:2` uses LinkedIn only. |

## Score formula

See the [root AGENTS.md](../AGENTS.md#score-formula) for the full breakdown. Quick reference:

- Geo (35 max), Freshness (25 max, looser windows), Stack (30 max), Niche (15 max), Intern desc (5 max)
- Company-tier scoring was dropped in the 2026-05-26 rebalance.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `0 unique` every run | `seen_v2` already has every posting in the current `hours_old` window. Expected — wait for fresh listings or `rm state.db`. |
| LinkedIn returns 0 rows | Proxy missing / expired. Check `CRAWLER_HTTPS_PROXY` in `~/.hermes/.env`. |
| `[glassdoor] error: not available for INDONESIA` | Upstream limitation. Glassdoor stays in the rotation in case it changes. |
| Plugin doesn't see new rows | The plugin and the cron share `state.db` through the symlink path. Confirm `~/.agents/skills/job-watcher` resolves to the repo's `skill/`. |
| Description bullets are empty | `summarize.py` couldn't reach the local `hermes` CLI (subprocess timeout / not installed). Check `~/.hermes/hermes-agent/hermes` exists. |
