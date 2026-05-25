# job-scout

A self-hosted daily intern-job radar for Indonesia. Scrapes LinkedIn, Indeed, Glassdoor, and Google Jobs every morning at 08:00 WIB, scores each posting against a configurable rubric, and posts a Discord card-per-posting digest with one-click triage buttons. A separate `/job` slash command opens a button-driven dashboard for browsing the persisted scrape history, top saved jobs, and triage stats.

Built for one user (me). Open-sourced because the architecture might be useful to others. Personal config lives in `skill/config/queries.yaml` — fork and edit for your own search.

## Why this exists

Job boards are noisy. LinkedIn shows the same posting under 5 city URLs. Indeed mixes "sales intern" into "software engineer intern" results. Email alerts arrive untriaged and pile up. The goal was a delivery surface that:

1. Dedupes across boards and locations (one posting = one card, regardless of how it's listed)
2. Scores by fit (geo, freshness, stack match, intern-program signals) so the best 5 of 50 surface first
3. Lives in Discord where I already triage things, with `[💾 Save] [❌ Dismiss] [✅ Applied] [🔗 Open]` buttons on each card
4. Keeps a queryable history so I can pull up "all postings ever, ranked by score" weeks later

## Architecture

```
┌────────────────────────── 08:00 WIB cron ──────────────────────────┐
│                                                                    │
│   ┌─ skill/bin/run.py ─────────────────────────────────────────┐   │
│   │  JobSpy → 4 boards × N queries × Jakarta                   │   │
│   │     │                                                      │   │
│   │     ▼                                                      │   │
│   │  title/company filter (queries.yaml blacklist)             │   │
│   │     │                                                      │   │
│   │     ▼                                                      │   │
│   │  dedup against state.db (seen_v2)                          │   │
│   │     │                                                      │   │
│   │     ▼                                                      │   │
│   │  compute_fit_score (scoring.py)                            │   │
│   │     │                                                      │   │
│   │     ▼                                                      │   │
│   │  summarize description (hermes oneshot → Copilot gpt-5.4)  │   │
│   │     │                                                      │   │
│   │     ▼                                                      │   │
│   │  format_posting_card  →  Discord card (notify.py)          │   │
│   └────────────────────────────────────────────────────────────┘   │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                  ┌──────── state.db ────────┐
                  │  seen_v2  (scrape log)   │
                  │  triage   (save/applied) │
                  └──────────────────────────┘
                              ▲
                              │
┌─── hermes-gateway systemd service ─────────────────────────────────┐
│                                                                    │
│   ┌─ plugin/__init__.py ────────────────────────────────────────┐  │
│   │  /job slash command  →  dashboard embed in #manage          │  │
│   └─────────────────────────────────────────────────────────────┘  │
│                                                                    │
│   ┌─ plugin/interactions.py ────────────────────────────────────┐  │
│   │  on_interaction listener (button clicks + modal submits)    │  │
│   │  • triage: 💾 Save / ❌ Dismiss / ✅ Applied → upsert triage │  │
│   │  • dashboard: 🔖 Saved / ✅ Applied / 📚 History / 📊 Stats  │  │
│   │  • ➕ Add Manual modal → log a job from anywhere            │  │
│   └─────────────────────────────────────────────────────────────┘  │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

## How it works (the 60-second version)

1. **Scrape** — JobSpy hits LinkedIn (via residential proxy), Indeed, Glassdoor, and Google Jobs for each query × location pair in `skill/config/queries.yaml`.
2. **Filter** — drops postings whose title hits the noise blacklist (sales, marketing, HR, finance, data sci, etc.) and whose company is on a spam-mill blacklist.
3. **Dedupe** — SQLite `seen_v2` table keyed by `f"{title.lower()}|{company.lower()}"` collapses cross-board, cross-region duplicates.
4. **Score** — `compute_fit_score` returns 0–110 based on geo, freshness, stack-match regex, niche AI keywords, and intern-program signals. Rows below `min_score: 40` don't deliver.
5. **Deliver** — one Discord embed card per posting, color-coded by score tier, with 4 triage buttons. On zero-new days, a small heartbeat to a separate channel proves the cron ran.

## Preview

A daily digest card in `#notification`:

```
┌──────────────────────────────────────────────────────────────┐
│ [60] Software Engineering Intern (AI Taskforce) @ StraitsX  │
│                                                              │
│ 🇮🇩 Jakarta, Indonesia (⚲ onsite)                            │
│ Today · Stack: `py` `agent` `llm`                            │
│                                                              │
│ Experience:                                                  │
│ • Final-year CS Bachelor                                     │
│ • 0-1 yrs Python + AI internship                             │
│ • Familiarity with LLM frameworks                            │
│                                                              │
│ Job desc:                                                    │
│ • Build internal AI taskforce tools                          │
│ • Prototype LLM agents for ops workflows                     │
│ • Collaborate with platform team                             │
│                                                              │
│   [💾] [❌] [✅] [🔗]                                          │
└──────────────────────────────────────────────────────────────┘
```

The `/job` dashboard in `#manage`:

```
┌─ 💼 Job Manager ──────────────────────────────────────────────┐
│ Quick dashboard for your job search.                          │
│                                                               │
│ 🔖 Top 5 Saved (by score)                                     │
│ • `[72]` Backend Eng Intern @ ByteDance                       │
│ • `[68]` Fullstack Eng Intern @ tiket.com                     │
│                                                               │
│ ✅ Recently Applied                                            │
│ • `[60]` SE Intern @ Grab · 1d ago                            │
│                                                               │
│ 📊 8 saved · 3 applied · 12 dismissed · 361 in scrape history │
│                                                               │
│  [🔖] [✅] [❌] [📚]                                            │
│  [📊] [⚙️] [❓] [🔄]                                            │
└───────────────────────────────────────────────────────────────┘
```

## Quick install

Tested on Debian 12 VPS with [Hermes Agent](https://github.com/Nous-Research/hermes-agent) installed and a Discord bot already wired in.

```bash
# 1. Clone
git clone https://github.com/absolutepraya/job-scout ~/job-scout
cd ~/job-scout

# 2. Configure
cp .env.example ~/.hermes/.env.append   # then merge into ~/.hermes/.env

# 3. Move any existing runtime dirs aside (script refuses to clobber)
mv ~/.agents/skills/job-watcher ~/.agents/skills/job-watcher.bak 2>/dev/null || true
mv ~/.hermes/plugins/job-manage-commands ~/.hermes/plugins/job-manage-commands.bak 2>/dev/null || true

# 4. Symlink + build venv
./install.sh

# 5. Restart Hermes so it picks up the plugin
sudo systemctl restart hermes-gateway

# 6. Schedule the daily cron (TZ=Asia/Jakarta recommended)
crontab -e
# Add:
# 0 8 * * * $HOME/.agents/skills/job-watcher/.venv/bin/python $HOME/.agents/skills/job-watcher/bin/run.py >> $HOME/.logs/job-watcher.log 2>&1
```

In Discord, run `/job` in your manage channel to confirm the dashboard loads.

## Customizing for your own search

Everything tunable lives in **`skill/config/queries.yaml`**:

| Field | What it controls |
|---|---|
| `queries` | Search terms (one JobSpy call per term × location × site) |
| `locations` | Where to scrape — `Jakarta, Indonesia` is the only default since this is a local-only build |
| `hours_old` | Max age in hours JobSpy will return (168 = 1 week) |
| `min_score` | Delivery threshold; postings under this don't reach Discord (default 40) |
| `workmode_blacklist` | `[abroad, remote]` by default — drops anything not onsite-in-Indonesia |
| `title_must_match_any` | Hard requirement: title must contain one of these (`intern`, `internship`, `magang`) |
| `title_must_not_match` | Noise blacklist — `sales`, `marketing`, `data sci`, `creator`, etc. |
| `company_must_not_match` | Known spam-mill companies that flood job sites |

Score formula tuning lives in **`skill/bin/scoring.py`** — `GEO_WEIGHTS`, `STACK_KEYWORDS`, `NICHE_TITLE_KEYWORDS`, `INTERN_DESC_SIGNALS`. Reweight to match your own stack and target.

Color tiers (HIGH ≥70 / MED 50–69 / LOW <50) are duplicated in `skill/bin/run.py` and `plugin/interactions.py` — change both.

## Repo layout

```
job-scout/
├── AGENTS.md              # agent-facing working notes (read before editing)
├── CLAUDE.md              # symlink → AGENTS.md
├── README.md              # this file
├── LICENSE                # MIT
├── .env.example
├── .gitignore
├── requirements.txt
├── install.sh             # symlinks runtime dirs into this clone
│
├── skill/                 # the cron scraper (runs as direct Python)
│   ├── SKILL.md
│   ├── bin/
│   │   ├── run.py         # main entry: scrape → score → deliver
│   │   ├── scoring.py     # compute_fit_score + 24 unit tests
│   │   ├── notify.py      # Discord delivery (bot token or webhook)
│   │   ├── summarize.py   # description → bullets via hermes oneshot
│   │   ├── migrate_state_db.py
│   │   └── test_scoring.py
│   └── config/
│       └── queries.yaml
│
└── plugin/                # the hermes plugin (runs inside hermes-gateway)
    ├── plugin.yaml
    ├── __init__.py
    ├── dashboard.py       # view builders (menu, saved, applied, history, all)
    ├── interactions.py    # button + modal dispatcher
    ├── commands.py        # legacy text-mode triage helpers
    ├── db.py              # triage table CRUD + migrations
    ├── config.py          # queries.yaml read/write
    ├── url_utils.py
    ├── discord_send.py
    └── test_*.py
```

See [AGENTS.md](./AGENTS.md) for the full SQLite schema contract, deploy model, and the score formula breakdown.

## Acknowledgments

- **[JobSpy](https://github.com/cullenwatson/JobSpy)** does the heavy lifting on board scraping.
- **[Hermes Agent](https://github.com/Nous-Research/hermes-agent)** by Nous Research hosts the Discord plugin and provides the `hermes -z PROMPT` oneshot used for description summarization.
- **GitHub Copilot gpt-5.4** (via Hermes oneshot) handles the per-posting bullet summarization at scrape time.
- LinkedIn scraping uses a residential proxy from [Karakeep](https://karakeep.app) — datacenter VPS IPs get banned by LinkedIn in ~5 requests without one.

## License

MIT. See [LICENSE](./LICENSE).
