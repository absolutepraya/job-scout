# job-watcher

Daily scraper that pulls fresh intern postings from LinkedIn, Indeed, Glassdoor, and Google Jobs across 17 locations, scores each posting against Abhip's resume profile, and delivers a sectioned digest to Discord.

Runs daily at **08:00 WIB** via system cron. Owner: Abhip (Daffa Abhipraya Putra).

## What it does

1. **Scrape:** JobSpy hits 4 job boards across 17 locations × 6 query strings (`software engineer intern`, `ai intern`, `backend intern`, `it intern`, `machine learning intern`, `software engineering intern`)
2. **Filter:** drops postings whose title hits noise keywords (sales, marketing, fashion, HR, etc.) or whose company is on a spam-mill blacklist (Skillzenloop, FetchJobs.co, WebBoost, etc.)
3. **Dedup:** SQLite key on `(title.lower(), company.lower())` collapses cross-region duplicates (LinkedIn often posts the same role across 5+ city URLs)
4. **Score:** computes a `fit_score` 0–100 weighted by geo proximity, posting freshness, stack-match to resume, niche bonus for AI/agent roles, and company tier
5. **Group:** splits postings into `🇮🇩 Local` (Indonesia) vs `🌍 Oversea`, then by role within each
6. **Deliver:** posts to Discord channel via yanto bot (`DISCORD_JOB_WATCHER_CHANNEL` env var), one logical section per message

## Architecture

| File | Responsibility |
|---|---|
| `SKILL.md` | Agent-facing manifest (frontmatter description) |
| `bin/run.py` | Main entry: scrape → filter → dedup → score → format → deliver |
| `bin/scoring.py` | Pure fit-score logic. Imported by `run.py`. Has 24 unit tests. |
| `bin/notify.py` | Discord delivery (auto-detects webhook vs bot token from `~/.hermes/.env`) |
| `bin/test_scoring.py` | Pytest suite for scoring logic |
| `bin/migrate_state_db.py` | One-shot SQLite schema migration (URL-based → title+company dedup) |
| `config/queries.yaml` | Queries, locations, filters, hours_old |
| `state.db` | Runtime SQLite (created on first run, not synced to dotfiles) |
| `.venv/` | Python venv with `python-jobspy`, `pyyaml`, `httpx`, `pytest` (rebuildable, not synced) |

## Setup on a fresh VPS

Assumes `~/.agents/skills/job-watcher/` files already restored from dotfiles backup.

```bash
# 1. Install uv (if not already)
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH=$HOME/.local/bin:$PATH

# 2. Create venv + install deps
uv venv ~/.agents/skills/job-watcher/.venv
uv pip install --python ~/.agents/skills/job-watcher/.venv/bin/python \
  python-jobspy pyyaml httpx pytest

# 3. Verify tests pass
cd ~/.agents/skills/job-watcher/bin
../.venv/bin/python -m pytest test_scoring.py -v

# 4. Initialize state.db (creates empty table; or run migrate_state_db.py if migrating from old schema)
~/.agents/skills/job-watcher/.venv/bin/python ~/.agents/skills/job-watcher/bin/migrate_state_db.py

# 5. Configure Discord delivery
# Edit ~/.hermes/.env and add ONE of:
#   DISCORD_JOB_WATCHER_WEBHOOK=https://discord.com/api/webhooks/.../...
# OR
#   DISCORD_JOB_WATCHER_CHANNEL=<channel_id>   (uses existing DISCORD_BOT_TOKEN)

# 6. Verify proxy reachable (uses ~/karakeep/.env)
cat ~/karakeep/.env | grep CRAWLER_HTTPS_PROXY
# Expected: CRAWLER_HTTPS_PROXY=http://daffaabhi-...:...@<ip>:<port>

# 7. Manual smoke test (dry-run, no state mutation, no Discord post)
~/.agents/skills/job-watcher/.venv/bin/python \
  ~/.agents/skills/job-watcher/bin/run.py --dry-run --site linkedin

# 8. Schedule daily cron (TZ=Asia/Jakarta assumed at top of crontab)
crontab -e
# Add:
0 8 * * * /home/praya/.agents/skills/job-watcher/.venv/bin/python /home/praya/.agents/skills/job-watcher/bin/run.py >> /home/praya/.logs/job-watcher.log 2>&1
```

## Common operations

```bash
# Manual full run (will deliver to Discord)
~/.agents/skills/job-watcher/.venv/bin/python ~/.agents/skills/job-watcher/bin/run.py

# Dry run — prints digest to stdout, does NOT update state.db or post
~/.agents/skills/job-watcher/.venv/bin/python ~/.agents/skills/job-watcher/bin/run.py --dry-run

# Restrict to one site (useful for debugging)
~/.agents/skills/job-watcher/.venv/bin/python ~/.agents/skills/job-watcher/bin/run.py --dry-run --site linkedin

# Reset state (re-emit all postings on next run — "fresh seed" mode)
rm ~/.agents/skills/job-watcher/state.db

# Count stored postings
~/.agents/skills/job-watcher/.venv/bin/python -c \
  "import sqlite3; c=sqlite3.connect('/home/praya/.agents/skills/job-watcher/state.db'); print(c.execute('SELECT count(*) FROM seen_v2').fetchone()[0])"

# Tail today's log
tail -100 ~/.logs/job-watcher.log

# Re-run tests after editing scoring.py
cd ~/.agents/skills/job-watcher/bin && ../.venv/bin/python -m pytest test_scoring.py -v
```

## Configuration knobs (`config/queries.yaml`)

| Field | Description |
|---|---|
| `hours_old` | Window for "fresh" postings, in hours. Currently 168 (1 week). |
| `limits.linkedin` | Max rows per LinkedIn query (tier-1 locations). Default 25. |
| `limits.indeed` | Default 50. |
| `limits.glassdoor` | Default 25. |
| `limits.google` | Default 25. |
| `limits_tier2.linkedin` | Tier-2 (foreign-country remote) LinkedIn limit. Default 10. |
| `queries` | List of search strings. Each runs against every location. |
| `locations` | List of location dicts. `tier: 1` = full 4-site sweep. `tier: 2` = LinkedIn-only. |
| `title_must_match_any` | Title must contain at least one of these (case-insensitive). |
| `title_must_not_match` | Drop if title contains any of these. |
| `company_must_not_match` | Drop if company name contains any of these (spam-mill blacklist). |
| `min_score` | Drop postings with `fit_score` below this. Set to 0 to disable. Default 40. |
| `workmode_blacklist` | Skip postings with these workmodes. Options: `onsite`, `remote`, `abroad`. Default `[abroad]`. |

## Scoring formula (in `scoring.py`)

```
fit_score = geo + freshness + stack + niche + company   (cap 100)
```

| Component | Max | Notes |
|---|---|---|
| `geo` | 25 | Indonesia=25 (or baseline 25 for any local posting), Singapore/Malaysia=22, Japan/Taiwan/China/HK/Korea=18, EU/UK/Scandinavia=14, Canada/Australia/NZ=10, US=5, anywhere else=0 |
| `freshness` | 20 | <24h=20, <48h=12, <72h=6, older=0 |
| `stack` | 30 | +3 per stack keyword matched in title+description, capped at 30 |
| `niche` | 15 | +15 if title contains "ai engineer" / "ml engineer" / "applied ai" / "agentic" / etc. |
| `company` | 10 | Anthropic/OpenAI/Cohere/Mistral/etc.=10 (S), Cloudflare/Vercel/FAANG=7 (A), regional unicorns + name-brand=4 (B), else 0 |

**Local vs Oversea distinction:** Indonesia postings (detected via location substring) get a flat `geo=25` baseline. Foreign postings use the tiered geo table.

## Troubleshooting

### Cron didn't fire at 08:00 WIB
- Check `crontab -l` has the line with `CRON_TZ=Asia/Jakarta` at top
- Check `~/.logs/job-watcher.log` for last run timestamp
- Check process: `pgrep -af run.py`
- Cron daemon: `systemctl status cron`

### Discord delivery silent / errored
- Verify env vars: `grep DISCORD ~/.hermes/.env`
- Bot mode needs both `DISCORD_BOT_TOKEN` and `DISCORD_JOB_WATCHER_CHANNEL`
- Webhook mode needs `DISCORD_JOB_WATCHER_WEBHOOK`
- Test manually:
  ```bash
  ~/.agents/skills/job-watcher/.venv/bin/python -c "
  import sys; sys.path.insert(0, '/home/praya/.agents/skills/job-watcher/bin')
  import notify; print(notify.send_sections(['test message']))
  "
  ```

### Too many junk postings
- Tighten `title_must_not_match` in `config/queries.yaml`
- Add spam companies to `company_must_not_match`
- Restart not needed — config is read fresh on every run

### LinkedIn rate-limit / 429s
- Karakeep proxy (residential Indonesian IP) bypasses most. Check `~/karakeep/.env` has `CRAWLER_HTTPS_PROXY`.
- Reduce `limits.linkedin` in `config/queries.yaml` if persistent
- Reduce `locations` tier-2 entries

### Glassdoor errors ("not available for INDONESIA" / "API error")
- Expected — Glassdoor has limited country coverage and recent API changes (see JobSpy issue tracker). Errors are non-fatal; other sites continue.

### Indeed SSL errors
- Transient. Caught and logged. Retries naturally on next run.

### JobSpy crash on NaN values
- Already handled by `_safe_str()` helper in `run.py`. If new crash arises, wrap the offending field with `_safe_str()`.

## Tradeoffs documented

- **`linkedin_fetch_description=True`** is enabled (see `run.py:fetch_site`). This adds O(n) requests per LinkedIn query → run time grows from ~15min to ~25-30min, but stack-match scoring becomes meaningful (titles alone rarely list TypeScript/Python/etc.).
- **State migration is one-way:** `migrate_state_db.py` collapses 1.0-schema rows into 2.0-schema. No rollback path — if migration fails or you want to start fresh, just `rm state.db`.
- **No browser-based scraping:** sticks to JobSpy's REST + HTML parsing approach. If LinkedIn tightens anti-bot beyond proxy bypass, may need to migrate to Patchright/Playwright (heavier).

## Where things live

| Thing | Path |
|---|---|
| Skill code | `~/.agents/skills/job-watcher/` |
| Runtime venv | `~/.agents/skills/job-watcher/.venv/` |
| SQLite state | `~/.agents/skills/job-watcher/state.db` |
| Run logs | `~/.logs/job-watcher.log` |
| Discord env | `~/.hermes/.env` (`DISCORD_JOB_WATCHER_CHANNEL`, `DISCORD_BOT_TOKEN`) |
| Proxy env | `~/karakeep/.env` (`CRAWLER_HTTPS_PROXY`) |
| Cron schedule | `crontab -l` |
| Dotfiles backup | `~/.dotfiles/vps/agents/skills/job-watcher/` (excludes venv + state.db) |

## Spec & plan

Original design spec and implementation plan documented at:
`~/Documents/Projects/job-watcher-spec/` (on Mac)
- `2026-05-17-job-watcher-digest-redesign.md` — design spec
- `2026-05-17-job-watcher-digest-redesign-plan.md` — implementation plan

---

## Discord triage interface (#manage channel)

A companion Hermes plugin at `~/.hermes/plugins/job-manage-commands/` lets you triage daily digests via `!commands` in the Discord `#manage` channel. Postings you `!save` / `!applied` / `!dismiss` are excluded from future daily digests.

### Commands

**Triage:**
- `!save <url> [<url2>...] [-- note]` — bookmark posting(s). Multi-URL supported.
- `!unsave <url>` — remove from saved
- `!applied <url>` — mark applied
- `!unapply <url>` — revert applied → saved
- `!dismiss <url>` — hide from future digests permanently
- `!apply <url>` — Phase-2.5 stub (Playwright auto-apply, not yet built)

**Lists:**
- `!saved` — all saved jobs
- `!applied list` — all applied jobs
- `!dismissed list` — all dismissed jobs
- `!recent [N]` — last N saved (default 5)
- `!stats` — counts of saved / applied / dismissed / scrape history

**Config (edits queries.yaml live, no restart needed):**
- `!config` — list all editable keys + values
- `!config get <key>` — print one value
- `!config set <key> <value>` — set scalar (int): `min_score`, `hours_old`
- `!config list <key>` — show list entries
- `!config add <key> <value>` — append to list (workmode_blacklist, title/company blacklists, queries)
- `!config rm <key> <value>` — remove from list
- `!config reset <key>` — restore default

**Editable keys:** `min_score`, `hours_old`, `workmode_blacklist`, `title_must_not_match`, `company_must_not_match`, `queries`. Other keys (`locations`, `limits.*`, cron) edit directly in queries.yaml or crontab.

**Meta:**
- `!help` / `!help <command>` — command reference

### Schema

New `triage` table in `state.db`:

```sql
CREATE TABLE triage (
    dedup_key TEXT PRIMARY KEY,    -- "<title>|<company>" (same as seen_v2)
    job_url TEXT NOT NULL,
    title TEXT, company TEXT, location TEXT,
    score INTEGER, workmode TEXT,
    status TEXT NOT NULL CHECK(status IN ('saved', 'applied', 'dismissed')),
    saved_at INTEGER, applied_at INTEGER, dismissed_at INTEGER,
    notes TEXT
);
```

`dedup_key` matches `seen_v2.dedup_key` — cross-platform duplicates (same title+company on LinkedIn AND Indeed) collapse to one entry. The cron's `run.py` reads this table and skips postings with any status from the daily digest.

### Plugin internals

Files at `~/.hermes/plugins/job-manage-commands/`:
- `plugin.yaml` — Hermes manifest (registers `pre_gateway_dispatch` hook)
- `__init__.py` — hook entry, channel + sender filter, dispatch
- `commands.py` — all command handlers (~400 lines)
- `db.py` — SQLite helpers (WAL mode + 5s busy timeout)
- `config.py` — ruamel.yaml round-trip + validators
- `discord.py` — Discord REST poster (chunking + 429 retry)
- `url_utils.py` — URL canonicalization (strip `<>` brackets + tracking params)
- `test_*.py` — pytest suites (40 tests total)

Dependency: `ruamel.yaml` (installed in Hermes venv at `~/.hermes/hermes-agent/venv/`).

Concurrency: both `run.py` and the plugin enable WAL mode on `state.db`. 5s busy timeout, single retry on lock. Collision rare in practice (cron runs ~30min once daily; plugin activity sparse).

### To disable

Remove `- job-manage-commands` from `plugins.enabled` in `~/.hermes/config.yaml` and restart: `sudo systemctl restart hermes-gateway`. Plugin files remain on disk but are inert.

### Troubleshooting

**Plugin not responding to commands:**
- Check user_id is in `DISCORD_ALLOWED_USERS` env: `grep DISCORD_ALLOWED_USERS ~/.hermes/.env`
- Check channel ID matches: `grep DISCORD_JOB_MANAGE_CHANNEL ~/.hermes/.env` should be `1505829728043663501`
- Check plugin enabled: `grep job-manage-commands ~/.hermes/config.yaml`
- Tail logs: `sudo journalctl -u hermes-gateway -f`

**SQLite "database is locked":**
- Plugin retries once with 100ms backoff. If persistent, the cron is mid-run — wait ~30 min for it to finish.

**!config set didn't take effect:**
- queries.yaml is read fresh on every cron run, so changes apply to NEXT daily digest, not retroactively.
- Verify the change persisted: `ssh vps 'grep "^min_score:" ~/.agents/skills/job-watcher/config/queries.yaml'`

**Tests:**
```bash
cd ~/.hermes/plugins/job-manage-commands && ~/.hermes/hermes-agent/venv/bin/python -m pytest -v
```
All 40 tests should pass.
