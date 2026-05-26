# job-scout — agent reference

Complete working reference for any AI agent (or human) editing or operating this repo. The shorter human pitch lives in [README.md](./README.md); this doc is the deep-dive: every config knob, every scoring component, every Discord convention, every common failure mode.

---

## What this is

`job-scout` is a self-hosted, single-user job radar. Every morning at 08:00 WIB, a cron-fired Python process scrapes four job boards (LinkedIn, Indeed, Glassdoor, Google Jobs) for intern roles in Jakarta, filters out noise, scores each posting against a configurable rubric, summarizes the description into bullets via an LLM oneshot, and posts one Discord embed card per posting with `[💾 Save] [❌ Dismiss] [✅ Applied] [🔗 Open]` triage buttons. A separate `/job` slash command opens a button-driven dashboard for browsing saved/applied/dismissed lists and the full scrape history.

The whole thing is built around one SQLite file (`state.db`) shared between two cooperating processes that live in different runtimes.

---

## Architecture

### Two halves, two runtimes

| Folder | Runtime | Trigger | Responsibility |
|---|---|---|---|
| `skill/` | Direct Python via system cron | `0 8 * * *` (08:00 WIB daily) | Scrape boards → filter → dedupe → score → summarize → post digest cards to `#notification` |
| `plugin/` | Hermes Agent gateway (`hermes-gateway` systemd service, long-running) | `/job` slash command + button clicks + modal submits | Dashboard in `#manage`, triage table CRUD, Add-Manual flow |

The two halves **never import each other**. The cron script runs as a one-shot Python process; the plugin runs inside a long-lived gateway listening to Discord WebSocket events. They communicate only via the shared SQLite file at `~/.agents/skills/job-watcher/state.db` (which is a symlink target → `skill/state.db` in the repo clone).

If you need a function in both halves, the established pattern is **`sys.path` injection from the plugin into `skill/bin/`** — see `plugin/dashboard.py` (`_JW_BIN = Path.home() / ".agents" / "skills" / "job-watcher" / "bin"`) and `plugin/db.py`. Do not copy code; import it through the runtime path.

### Deploy model

The repo is the **source of truth**. On VPS, runtime dirs are symlinks back to this clone:

```
~/.agents/skills/job-watcher       → ~/job-scout/skill
~/.hermes/plugins/job-manage-commands → ~/job-scout/plugin
```

To ship a change:

```bash
# On Mac
edit → git push

# On VPS
ssh vps
cd ~/job-scout && git pull
sudo systemctl restart hermes-gateway   # only if plugin/ changed
# skill/ reloads fresh on each cron tick — no restart needed
```

`install.sh` (at the repo root) creates the symlinks. It refuses to clobber existing real directories — that's intentional, to protect a live `state.db`. First-time deploy steps:

```bash
git clone https://github.com/absolutepraya/job-scout ~/job-scout
cd ~/job-scout
mv ~/.agents/skills/job-watcher ~/.agents/skills/job-watcher.bak.$(date +%s) 2>/dev/null
mv ~/.hermes/plugins/job-manage-commands ~/.hermes/plugins/job-manage-commands.bak.$(date +%s) 2>/dev/null
# Preserve state.db: move it into ~/job-scout/skill/state.db BEFORE running install.sh
./install.sh   # creates symlinks, builds venv
# Restart Hermes + add the cron entry — see "Cron entry" below
```

### End-to-end data flow (worked example)

A posting flows through the system like this:

```
JobSpy returns dict {title, company, location, description, date_posted, ...}
  │
  ▼
title_must_match_any check       — drop if no "intern"/"internship"/"magang"
title_must_not_match check       — drop if matches "sales", "data sci", etc.
company_must_not_match check     — drop if known spam-mill company
  │
  ▼
dedup_key = f"{title.lower()}|{company.lower()}"
SELECT 1 FROM seen_v2 WHERE dedup_key = ?
  │
  ├─ already seen → skip (also skip if in triage table — already handled)
  │
  └─ new
       │
       ▼
classify_workmode → "onsite" / "remote" / "abroad" (relocate)
  │
  ▼
workmode_blacklist check         — drop if "abroad" or "remote" (default config)
  │
  ▼
compute_fit_score (geo, freshness, stack, niche, intern_desc) → int 0..110
  │
  ├─ fit < min_score (40) → INSERT INTO seen_v2 with filtered=1, continue
  │
  └─ fit >= min_score
       │
       ▼
summarize(description) → (exp_bullets, jobdesc_bullets) via hermes oneshot
  │
  ▼
INSERT INTO seen_v2 with filtered=0 + bullets
  │
  ▼
format_posting_card → {embed, components}
  │
  ▼
notify.send_sections → Discord REST → card lands in #notification
  │  (sub-second per card; 0.4s delay between; 429 retry up to 3x)
  │
  ▼
User clicks 💾 Save → button interaction fires
  │
  ▼
plugin/interactions.py:_on_interaction
  → _handle_button_action("save", sid)
  → db.upsert_triage(snap, "saved")
  → edit_message: keep embed, remove buttons, footer "🔖 Saved · HH:MM WIB"
```

---

## SQLite schema (the contract you must not break)

`state.db` has two tables. The dedup contract holds the whole system together.

### `seen_v2` (scrape history, written by `skill/run.py`)

| Column | Type | Notes |
|---|---|---|
| `dedup_key` | TEXT PRIMARY KEY | `f"{title.lower().strip()}\|{company.lower().strip()}"` — case-insensitive, whitespace-trimmed |
| `job_url` | TEXT | First URL we saw for this dedup_key. Subsequent dupes don't overwrite. |
| `title` | TEXT | As returned by JobSpy. Mixed case preserved. |
| `company` | TEXT | Same. |
| `location` | TEXT | E.g. "Jakarta, Jakarta, Indonesia". |
| `site` | TEXT | `linkedin` / `indeed` / `glassdoor` / `google` |
| `first_seen` | INTEGER (Unix epoch) | Set once on insert. Used as `date_posted` proxy for live-score recount. |
| `query_term` | TEXT | Which search query found it (e.g. `"ai intern"`). Used to group cards in the digest. |
| `fit_score` | INTEGER | Stored at scrape time with `+25` freshness baked in (the max). Plugin's `_live_score` decays from there. |
| `is_local` | INTEGER (0/1) | Cached `is_local(posting)` — Indonesia substring match. |
| `exp_bullets` | TEXT (nullable) | Newline-joined Experience bullets from `summarize.py`. |
| `jobdesc_bullets` | TEXT (nullable) | Newline-joined Job desc bullets. |
| `filtered` | INTEGER (0/1, default 0) | Set to 1 when posting passed title/company filter but scored below `min_score`. Audit-only; not delivered. |

**Dedup key format is THE contract.** Both `skill/bin/run.py:dedup_key` and `plugin/interactions.py` (the manual-Add modal handler) must compute it the same way. Don't change one without the other. The `lower().strip()` matters — "ByteDance" and "bytedance " hash to the same key.

### `triage` (saved/applied/dismissed, written by `plugin/db.py`)

| Column | Type | Notes |
|---|---|---|
| `dedup_key` | TEXT PRIMARY KEY | Same format as `seen_v2.dedup_key` — joinable. |
| `job_url`, `title`, `company`, `location` | TEXT | Snapshotted at triage time (so the row stands alone even if `seen_v2` row is later deleted). |
| `score` | INTEGER (nullable) | Fit-score snapshot. NULL for manually-Added rows (no scrape). |
| `workmode` | TEXT (nullable) | `onsite` / `remote` / `relocate`. Auto-classified from location string for manual Adds. |
| `status` | TEXT NOT NULL | `saved` / `applied` / `dismissed` (CHECK constraint enforces). |
| `saved_at`, `applied_at`, `dismissed_at` | INTEGER (nullable) | Set when transitioning into that state. Multi-state rows (e.g. saved then applied) keep both timestamps. |
| `notes` | TEXT (nullable) | Free-text from the Add Manual modal. |

Always use `plugin/db.py:upsert_triage(conn, snapshot, status, notes=None)` — never raw SQL.

### Migrations

`_migrate_seen_v2` runs on every `open_db()` call. Idempotent ALTER TABLE chain. Duplicated in **both** `skill/bin/run.py` and `plugin/db.py` since they can't import each other. When adding a column: edit both, in the same commit.

---

## Configuration reference

All tunables live in `skill/config/queries.yaml`. The whole file is read fresh on every cron tick — no restart needed for config changes.

| Field | Type | Default | What it controls |
|---|---|---|---|
| `hours_old` | int | 168 | JobSpy's `hours_old` window. 168 = 7 days. Lower for tighter freshness, higher for more rows. |
| `min_score` | int | 40 | Delivery gate. Rows scoring under this get `filtered=1` and don't reach Discord. |
| `workmode_blacklist` | list | `[abroad, remote]` | Drops postings whose `classify_workmode(rec)` returns one of these. Options: `onsite`, `remote`, `abroad`. |
| `limits` | dict | `{linkedin: 25, indeed: 50, glassdoor: 25, google: 25}` | Per-site result cap per `(query, location)` pair (tier-1 locations). |
| `limits_tier2` | dict | `{linkedin: 10}` | Per-site cap for tier-2 locations. Only `linkedin` is fetched for tier-2; other sites skipped. |
| `queries` | list[str] | 8 entries | One JobSpy call per `(query, location, site)` triple. See below. |
| `locations` | list[dict] | `[Jakarta, Indonesia]` | `{name, country_indeed?, is_remote?, tier?}`. `tier:2` = oversea (LinkedIn-only). |
| `title_must_match_any` | list[str] | `[intern, internship, magang]` | Hard requirement. Title must contain at least one (case-insensitive). |
| `title_must_not_match` | list[str] | ~40 entries | Noise blacklist. Substring match. Drops if any matches. |
| `company_must_not_match` | list[str] | ~30 entries | Spam-mill blacklist. Substring match on company. |

### Current `queries` list

```yaml
queries:
  - "software engineer intern"
  - "ai intern"
  - "backend intern"
  - "it intern"
  - "machine learning intern"
  - "software engineering intern"
  - "automation engineer intern"
  - "solutions consultant intern"
```

Each fires against each `location` × each `site`. With one location and 4 sites, that's 8 × 1 × 4 = 32 JobSpy calls per run (~45-60 seconds total).

### `title_must_not_match` rationale (selected entries)

| Pattern | Why blocked |
|---|---|
| `sales`, `marketing`, `marketer` | Non-engineering roles |
| `hr intern`, `human resources`, `people & culture`, `people ops` | HR roles |
| `finance intern`, `accounting intern`, `tax intern`, `internal audit` | Finance roles |
| `legal intern` | Legal roles |
| `design intern`, `graphic`, `motion designer` | Design roles (separate track) |
| `content intern`, `content creator`, `social media`, `creator` | Content/marketing roles |
| `business development`, `bd intern`, `partnerships intern` | BD roles |
| `data sci`, `data ana` | Data science / data analyst roles (substring matches `data sci*` and `data ana*`) |
| `operation`, `operations intern` | Ops roles |
| `strategy intern`, `strategic` | Strategy roles |
| `entry level`, `fresher` | Often non-tech entry roles |
| `html developer`, `html/css`, `wordpress`, `shopify`, `data annotation` | Spam-mill bait |

Adding a new entry: lowercase, substring-match, case-insensitive. Test against a few real titles before deploying.

### `company_must_not_match` rationale

Known spam-mill companies that flood job boards with fake postings: `Skillzenloop`, `FetchJobs.co`, `WebBoost`, `UM IT`, `LeoStoy`, `ArGo Intern`, `Pythrust`, `Infrabyte`, `Techskill`, `Wake Up Whistle`, `Zenithbyte`, `Webs IT Solution`, `DataAnnotation`, `Ededge`, `Skillfied Mentor`, `Digital Nexus AI`, `SortifAI`, `Joy_`, `Built By The Trades`, `Stealth`, `Stealth Startup`, `TalentXM`, `KPN Corp`, `Inficore`, `Faceify Labs`, `Coffeee.io`, `Teesta Investment`, `Sparrow`, `Cresta`, `Latin Leap`.

When LinkedIn surfaces a new spammer, add them here. They typically post 20-50 fake "AI intern" / "remote dev" listings per week.

### `locations`

Current state ships with one location:

```yaml
locations:
  - {name: "Jakarta, Indonesia", country_indeed: "Indonesia", is_remote: false, tier: 1}
```

Tier-2 oversea locations were removed in the 2026-05-22 cleanup; `workmode_blacklist: [abroad, remote]` would drop them anyway.

---

## Score formula

Implemented in `skill/bin/scoring.py:compute_fit_score`. Returns `(total: int, top3_stack_matches: list[str], is_local: bool)`.

### Components (post-2026-05-26 rebalance)

| Component | Max | Formula | Source |
|---|---|---|---|
| **Geo** | 35 | `35` if local Indonesia, else `score_geo(location)` lookup against `GEO_WEIGHTS` | `scoring.py:194-215` |
| **Freshness** | 25 | `<5d`=25 · `<10d`=18 · `<20d`=12 · `<30d`=6 · older=0. **Defaults to 12 when `date_posted` is missing/nan** | `scoring.py:118-141` |
| **Stack** | 30 | `+3` per word-boundary regex match on title+description (`STACK_KEYWORDS`), capped at 30 | `scoring.py:144-150` |
| **Niche** | 15 | `+15` if title hits `NICHE_TITLE_KEYWORDS`. **+8 if only description hits** (half-weight, 2026-05-26) | `scoring.py:152-168` |
| **Intern desc** | 5 | `+5` if description contains any of `INTERN_DESC_SIGNALS` | `scoring.py:170-188` |

Total possible: ~110. Color tiers (HIGH ≥70 / MED 50-69 / LOW <50) cover the realistic range.

**Company-tier scoring was dropped** in the 2026-05-26 rebalance. `score_company()` still exists for callers, but `compute_fit_score` no longer sums it. Energy/industrial companies (Halliburton, Schlumberger, Petronas, etc.) and tech tier-1 (Anthropic, OpenAI, Cloudflare) used to get +4 to +10; that's gone.

### Geo tiers (`GEO_WEIGHTS`)

| Score | Locations |
|---|---|
| 35 | Indonesia, Jakarta, Tangerang, Bandung, Depok, Bekasi, Bali, Surabaya |
| 30 | Singapore, Malaysia, Kuala Lumpur, Penang |
| 25 | Japan/Tokyo/Osaka, Taiwan/Taipei/Hsinchu, China/Shanghai/Beijing/Shenzhen, Hong Kong, South Korea/Seoul |
| 20 | UK, Germany, Netherlands, France, Sweden, Norway, Denmark, Finland, Italy, Spain, Belgium, Switzerland, Ireland |
| 15 | Canada/Toronto/Vancouver, Australia/Sydney/Melbourne, New Zealand/Auckland |
| 8 | USA, NYC, SF, Seattle, LA, Boston, Chicago, Austin, Atlanta |
| 0 | Everything else |

When `is_local=True` (Indonesia substring match in `location`), geo is hardcoded to 35 regardless of which keyword matched. This is the local baseline that ensures Jakarta postings always have a strong geo floor.

### Stack keywords (`STACK_KEYWORDS`)

Word-boundary regex matches on lowercased `title + " " + description`. Each match adds +3, capped at 30.

| Short | Pattern |
|---|---|
| `ts`, `js`, `py`, `java` | TypeScript, JavaScript, Python, Java |
| `sql` | `sql`/`postgres`/`postgresql` |
| `react`, `rn`, `next`, `remix`, `tanstack`, `tailwind`, `framer` | React, React Native, Next.js, Remix, TanStack, Tailwind, Framer |
| `express`, `elysia`, `fastapi`, `django` | Backend frameworks |
| `supabase`, `redis`, `docker` | Infra |
| `cf` | Cloudflare / Workers / Durable Objects / AI Gateway |
| `vercel`, `bun`, `gh` (GitHub Actions), `trigger.dev` | Modern tooling |
| `sentry`, `posthog`, `playwright` | Observability / testing |
| `agent`, `rag`, `mcp`, `llm`, `genai` | AI agentic |
| `azure`, `gcp`, `aws`, `gemini`, `openai` | Cloud / model providers |

Bias: these reflect the resume of the project's primary user. To rebalance for your own stack, edit `STACK_KEYWORDS` in `scoring.py` — keep regex word boundaries to avoid false positives.

### Niche title keywords (`NICHE_TITLE_KEYWORDS`)

Substring match (case-insensitive). Title hit = +15, description-only hit = +8 (half-weight).

`ai engineer`, `ml engineer`, `applied ai`, `ai agent`, `agentic`, `llm engineer`, `genai engineer`, `gen ai`, `generative ai`, `forward deployed`, `applied scientist`

The half-weight description hit was added in the 2026-05-26 fix-pack after Astra's Otopprentice IT/Intern - AOP posting got dropped: title was generic but the description body listed "AI Engineer Intern (Karawang)" as a sub-role.

### Intern description signals (`INTERN_DESC_SIGNALS`)

Substring match on description. Hit adds +5 (binary, not cumulative). Catches structured intern programs vs one-off listings.

`entry level`, `early career`, `intern program`, `internship program`, `graduate program`

Indonesian equivalents (`program magang`, `lulusan baru`, `fresh graduate`) are **not** included by default. Consider adding if Indonesia-only target.

### Delivery gate

`config/queries.yaml:min_score` (default `40`). Rows scoring under this get `filtered=1` (logged for audit) but don't reach Discord. To loosen: drop `min_score` to 35 or 30. To tighten: raise to 50 (will drop most no-description postings).

### Live recount

`plugin/dashboard.py:_live_score(stored, first_seen_ts)` recomputes the freshness component at render time. The stored `fit_score` has the +25 freshness max baked in; `_live_score` subtracts that and adds live freshness derived from `first_seen` (used as `date_posted` proxy).

If you change the freshness tier values in `scoring.py`, mirror them in `_live_score` and update the assumed-baked constant (25). Both files have a comment pointing at each other.

### Worked examples

**Astra Otopprentice IT/Intern - AOP @ PT Astra International Tbk (Jakarta):**
- Geo: 35 (local)
- Freshness: 12 (LinkedIn returned `date_posted=nan` → missing-date default)
- Stack: 3 (`py` matched once)
- Niche: 8 (no title hit, but description has "AI Engineer Intern" → half-weight)
- Intern desc: 0 (description is Bahasa Indonesia; English signals don't match)
- **Total: 58** → MED tier, delivers

**Halliburton A427-ESG-Intern (Tangerang):**
- Geo: 35 (local)
- Freshness: 12 (no date_posted)
- Stack: 0 (description is HR boilerplate)
- Niche: 0
- Intern desc: 5 (description has "Entry level for professional work")
- **Total: 52** → MED tier, delivers

**Typical fresh local SE intern with rich description:**
- Geo: 35, Freshness: 25, Stack: 9 (e.g., `py`, `sql`, `docker`), Niche: 0, Intern desc: 5 = **74 → HIGH tier**

**Typical fresh AI engineer intern:**
- Geo: 35, Freshness: 25, Stack: 12 (`py`, `agent`, `llm`, `openai`), Niche: 15 (title hit), Intern desc: 5 = **92 → HIGH tier**

---

## Discord delivery

### `notify.py` mechanics

- Reads `~/.hermes/.env` on import (`_load_env_from_file`). Required vars: `DISCORD_BOT_TOKEN`, `DISCORD_JOB_WATCHER_CHANNEL`. Optional: `DISCORD_JOB_WATCHER_WEBHOOK` (alternative path; takes precedence if set).
- `send_sections(messages, channel_id_override=None)` iterates and posts each section. Text-only messages over 1900 chars are split at line boundaries.
- 429 retry: up to 3 attempts with `retry_after` honored from response. Other failures log to stderr and continue (one bad card doesn't poison the whole digest).
- 0.4s sleep between sends to stay under Discord's per-channel rate limit.

### Message ordering in the daily digest

```
1. Header (text): "## 🆕 N new postings"
2. (if local postings exist) "## 🇮🇩 Local (N)"
   2a. Per-role: "`SOFTWARE ENGINEER INTERN` (N)" header
   2b. One card per posting in that role (sorted by fit_score DESC, then date_posted DESC)
3. (if oversea postings exist) Per-role: "## 🌍 Oversea · `ROLE NAME` (N)"
   3a. One card per posting
```

Role headers use backtick + uppercase format. Cards are individual embeds with 4 buttons each.

### Heartbeat on zero-new days

If `len(new_postings) == 0`, instead of staying silent, `run.py` posts a small heartbeat to `DISCORD_JOB_HEARTBEAT_CHANNEL`:

```
🟢 job-watcher heartbeat · DD Mon YYYY · HH:MM WIB
N scraped · 0 new (all duplicates) · 1 location · 4 sites
```

This proves the cron ran. Without it, "0 new" days are indistinguishable from "cron broken" days. Implemented in `run.py:main()`.

### Card format (`format_posting_card`)

Embed:
- Title: `[score] {title} @ {company}` (truncated to 200 chars title / 60 company)
- URL: posting URL (clickable title)
- Description (3-7 lines):
  - Line 1: `{flag} {location} ({glyph} {workmode})`
  - Line 2 (skip if both unknown): `{age} · Stack: {stack1} {stack2} {stack3}`
  - Blank line
  - `**Experience:**` + up to 3 bullets (or `_(no description)_`)
  - Blank line
  - `**Job desc:**` + up to 3 bullets (or `_(no description)_`)
- Color: tier-based (see Discord conventions below)

Components (one action row, 3-4 buttons):
- `💾` (style 2, custom_id `jm:save:<sid>`)
- `❌` (style 2, custom_id `jm:dismiss:<sid>`)
- `✅` (style 2, custom_id `jm:applied:<sid>`)
- `🔗` (style 5 link button to job_url, only added if URL present)

`<sid>` is `sha1(dedup_key)[:12]` — 12-char hash. The full `dedup_key` can exceed Discord's 100-char custom_id limit, so we hash and reverse-look-up on click.

### Workmode glyphs

| Glyph | Mode |
|---|---|
| `⚲` | onsite |
| `ᯤ` | remote |
| `✈︎` | abroad (relocate-required) |

`classify_workmode(posting)`: `is_remote=True` or "remote" in location → "remote"; any Indonesian city keyword → "onsite"; everything else → "relocate".

---

## Summarizer (`summarize.py`)

Per-posting Experience + Job desc bullets via the local `hermes` CLI in oneshot mode.

- Shells out: `hermes -z PROMPT --provider copilot -m gpt-5.4 --ignore-rules --ignore-user-config`
- Prompt embedded in the file — few-shot example baked in (`Workshop Backend Engineering Intern...`)
- Strict output format requested: `EXPERIENCE:\n- bullet\n- bullet\n\nJOB DESC:\n- bullet\n- bullet`
- Max 6 words per bullet, max 3 bullets per section (enforced via prompt, parsed via `_parse_sections`)
- Timeout: 45s. Failure modes (timeout, non-zero rc, parse error, description <80 chars) all return `([], [])` — card renders `_(no description)_`.
- Called only for delivered postings (fit ≥ min_score), not for filtered=1 rows. ~45 oneshot calls per cron run × 5-10s each = ~5-7 min of wall time. Acceptable for a once-daily 08:00 job.

### Cost

GitHub Copilot oneshot via Hermes — free if you have a Copilot subscription. No per-call Anthropic/OpenAI cost.

---

## Discord conventions

### Custom_id namespace

```
jm:                              ← mandatory namespace prefix
├─ save:<sid>                    ← daily-digest card triage (sid = sha1[:12])
├─ dismiss:<sid>
├─ applied:<sid>
├─ dash:                         ← dashboard view router
│   ├─ menu                      ← main dashboard
│   ├─ refresh                   ← alias for menu, used by 🔄 button
│   ├─ saved:<page>              ← saved-list view, page index
│   ├─ applied:<page>            ← applied-list view
│   ├─ dismissed:<page>          ← dismissed-list view
│   ├─ hist:<yyyymmdd>:<page>    ← history per-day view, page within day
│   ├─ histall:<page>            ← all-history flat view
│   ├─ stats                     ← counts breakdown
│   ├─ config                    ← read-only config dump
│   └─ help                      ← in-Discord help
├─ add:<status>                  ← Add Manual button (currently status="applied" only)
├─ addmodal:<status>             ← Modal submit (matches the modal's custom_id)
└─ noop:<view>:<direction>       ← disabled boundary buttons (e.g. jm:noop:saved:newer)
                                   ack-deferred silently
```

The dispatcher in `plugin/interactions.py:_on_interaction` routes by parsing parts.

### Why noop buttons

Discord rejects messages with **duplicate `custom_id` values across components, even on disabled buttons**. At pagination boundaries, both Prev and Next would resolve to page 0, causing a 400 error. We give disabled buttons unique `jm:noop:<view>:<dir>` IDs and ack-defer them in the handler. Don't reintroduce shared IDs.

### Color tiers

Must stay in sync across `skill/bin/run.py` and `plugin/interactions.py`:

| Tier | Hex | Score range | Trigger |
|---|---|---|---|
| HIGH | `#377AAF` (deep blue) | `>= 70` | Initial card color |
| MED | `#3BBBB3` (teal) | `50-69` | Initial card color |
| LOW | `#79DC96` (green) | `< 50` | Initial card color |
| RED | `#F93827` | (any) | After ❌ Dismiss click |
| BLUE | `#2C5EAD` | (any) | After ✅ Applied click |

(Save click doesn't change color — only adds the "🔖 Saved · HH:MM WIB" footer.)

---

## Dashboard view catalog

`/job` in the manage channel posts a single embed that edits in place when buttons are clicked. Every view returns `{"embeds": [embed], "components": [row, ...]}`.

| View | Builder | Embed shows | Buttons |
|---|---|---|---|
| Menu | `build_menu` | Top 5 saved (by stored score), Recent 5 applied, counts breakdown | 8 nav buttons (Saved/Applied/Dismissed/History/Stats/Config/Help/Refresh) |
| Saved | `build_saved_view` | Saved list, paginated 15/page, sorted by stored score DESC | Pagination + Menu |
| Applied | `build_applied_view` | Applied list, paginated 15/page | Pagination + Menu + **➕ Add Manual** (only here) |
| Dismissed | `build_dismissed_view` | Dismissed list | Pagination + Menu |
| History | `build_history_view` | One day's scraped postings, grouped by Local/Oversea then by role | Prev page / Next page within day + Older day / Newer day / 📜 All / Menu |
| All History | `build_history_all_view` | All seen_v2 rows flat, sorted by **live** score DESC | Prev/Next page + 📅 By Day / Menu |
| Stats | `build_stats_view` | Triage counts + scrape history totals | Menu only |
| Config | `build_config_view` | Read-only dump of editable `queries.yaml` keys | Menu only |
| Help | `build_help_view` | What each button does | Menu only |

All views exclude `seen_v2.filtered=1` rows (`WHERE COALESCE(filtered, 0) = 0`) — sub-threshold rows are audit-only, not user-facing.

### Add Manual modal

Trigger: ➕ button on the Applied view (`jm:add:applied`).

Discord modal opens with 5 text inputs (Discord's max):

1. Title* (required, max 200)
2. Company* (required, max 100)
3. URL* (required, max 400) — must be a valid URL since it doubles as the dedup join hint
4. Location (optional, max 200) — auto-classified into workmode via `_classify_workmode_from_location`
5. Notes (optional paragraph, max 1000) — saved to `triage.notes`

Submit handler (`_handle_add_modal_submit`) computes `dedup_key` the same way `run.py` does, calls `upsert_triage(snap, "applied")`, replies with ephemeral confirmation. If the same posting later appears in a scrape, `seen_v2` and `triage` join naturally on `dedup_key`.

---

## Environment variables

All consumed via `~/.hermes/.env` (loaded by `notify.py:_load_env_from_file` and by Hermes itself for the plugin).

| Var | Required | Used by | What |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | Yes (or webhook) | `notify.py`, plugin | Yanto Discord bot auth |
| `DISCORD_JOB_WATCHER_CHANNEL` | Yes (or webhook) | `notify.py` | Channel ID for daily digest delivery |
| `DISCORD_JOB_MANAGE_CHANNEL` | Yes | plugin (`__init__.py`) | Channel ID where `/job` posts the dashboard |
| `DISCORD_JOB_HEARTBEAT_CHANNEL` | Recommended | `run.py` | Channel ID for "0 new" heartbeat messages |
| `DISCORD_ALLOWED_USERS` | Yes | plugin (`interactions.py`) | Comma-separated Discord user IDs allowed to click buttons |
| `DISCORD_JOB_WATCHER_WEBHOOK` | Optional | `notify.py` | Alternative delivery path; takes precedence over bot token if set |
| `CRAWLER_HTTPS_PROXY` | Required for LinkedIn | `run.py` | Residential HTTPS proxy. Datacenter VPS IPs get banned in ~5 LinkedIn requests without one. |

`run.py` also reads the proxy from `~/karakeep/.env:CRAWLER_HTTPS_PROXY` as a fallback if not set in `~/.hermes/.env`.

---

## Cron entry

```
0 8 * * * $HOME/.agents/skills/job-watcher/.venv/bin/python $HOME/.agents/skills/job-watcher/bin/run.py >> $HOME/.logs/job-watcher.log 2>&1
```

Recommended crontab header:

```
TZ=Asia/Jakarta
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
```

The absolute path passes through the symlink to the repo's `skill/.venv` and `skill/bin/run.py`. Symlink stability lets you `git pull` updates without touching the cron entry.

---

## Common operations

```bash
# === On VPS ===

# Dry-run scrape (no DB writes, no Discord post)
~/.agents/skills/job-watcher/.venv/bin/python \
  ~/.agents/skills/job-watcher/bin/run.py --dry-run

# Dry-run restricted to one site (faster for debugging)
~/.agents/skills/job-watcher/.venv/bin/python \
  ~/.agents/skills/job-watcher/bin/run.py --dry-run --site linkedin

# Force a live run RIGHT NOW (will deliver to Discord!)
~/.agents/skills/job-watcher/.venv/bin/python \
  ~/.agents/skills/job-watcher/bin/run.py

# Tail the cron log
tail -100 ~/.logs/job-watcher.log

# Inspect seen_v2 state
sqlite3 ~/.agents/skills/job-watcher/state.db \
  "SELECT status, COUNT(*) FROM triage GROUP BY status; SELECT COUNT(*) FROM seen_v2 WHERE filtered=0; SELECT COUNT(*) FROM seen_v2 WHERE filtered=1;"

# Audit: what got filtered today?
sqlite3 ~/.agents/skills/job-watcher/state.db \
  "SELECT title, company, fit_score FROM seen_v2 WHERE filtered=1 AND date(first_seen,'unixepoch','localtime')=date('now','localtime') ORDER BY fit_score DESC;"

# Reset scrape history (re-deliver everything on next cron — also drops triage, careful!)
rm ~/.agents/skills/job-watcher/state.db

# Re-run scoring tests
cd ~/.agents/skills/job-watcher/bin && ../.venv/bin/python -m pytest test_scoring.py -v

# === Hermes plugin lifecycle ===

# Restart gateway (after plugin/ code change)
sudo systemctl restart hermes-gateway

# Verify plugin re-attached
grep "attached on_interaction" ~/.hermes/logs/agent.log | tail -1

# Tail plugin runtime log
tail -f ~/.hermes/logs/agent.log

# === Deploy ===

# After editing on Mac + git push
ssh vps 'cd ~/job-scout && git pull && sudo systemctl restart hermes-gateway'
```

---

## Testing

| Suite | Command | Coverage |
|---|---|---|
| Scoring | `cd skill/bin && ../.venv/bin/python -m pytest test_scoring.py -v` | 25 cases covering geo tiers, freshness buckets (including missing-date default), stack regex, niche (title + desc), company tiers, full integration scenarios including the Astra regression test |
| Plugin URL utils | `cd plugin && python -m pytest test_url_utils.py -v` | URL parsing helpers |
| Plugin commands | `cd plugin && python -m pytest test_commands.py -v` | Legacy text-mode command helpers (not actively used since dashboard) |
| Plugin config | `cd plugin && python -m pytest test_config.py -v` | `queries.yaml` read/write via ruamel |

No e2e tests. Live verification:
- `/job` in `#manage` → dashboard renders
- Force a live `run.py` run during the day → see Discord card appear

---

## Migration history (chronological)

| Date | Change | Why |
|---|---|---|
| 2026-05-16 | Initial release: scrape→filter→score→deliver pipeline | Bootstrap |
| 2026-05-17 | Card-per-posting embeds with triage buttons (replaced bulk digest text) | Faster triage |
| 2026-05-18 | Hermes plugin half: `/job` dashboard with button views | One-tap browse without typing |
| 2026-05-22 | `workmode_blacklist: [abroad, remote]`, locations stripped to Jakarta only, title blacklist expanded (data sci, people ops, etc.), summarizer wired via hermes oneshot, color tiers introduced, role headers in backticked uppercase | Tighten to local on-site only; richer cards |
| 2026-05-23 | Energy/industrial company tier added (Halliburton + 15 others); `score_intern_desc` added | Catch Halliburton-class postings |
| 2026-05-24 | Heartbeat channel for zero-new days; notify.py supports channel_id override | Visibility into idle cron runs |
| 2026-05-25 | seen_v2 cleanup (351 rows deleted); 2 new queries (`automation engineer intern`, `solutions consultant intern`) | Curated history after filter changes |
| 2026-05-26 (am) | Repo extracted to `github.com/absolutepraya/job-scout`, VPS runtime symlinked to clone; ➕ Add Manual modal on Applied view; 📜 All History view with live-score recount; secrets scrubbed from public source | Source-of-truth consolidation |
| 2026-05-26 (pm) | **Rebalance** (originally targeted 05-26 but didn't land until now): geo local 25→35, freshness max 20→25 days-based (5/10/20/30d tiers), company tier dropped from total; **Astra fix-pack**: missing-date freshness default = 12, `score_niche` scans description with half-weight (+8); `filtered` column added for sub-threshold audit trail | Catch Astra-class postings; richer score envelope |

---

## Known limitations + tradeoffs

| Limitation | Why | Workaround |
|---|---|---|
| LinkedIn returns `date_posted=nan` for many postings | LinkedIn API quirk — we don't get a posting date | `score_freshness` defaults to 12 (middle tier). Score still reflects "we don't know the age" rather than "definitely old". |
| Glassdoor returns "not available for INDONESIA" | Upstream Glassdoor restriction | Kept in rotation in case it changes; effectively 0 rows from Glassdoor. |
| Description bullets ONLY for LinkedIn rows | JobSpy only fetches descriptions when `linkedin_fetch_description=True`. Indeed/Google return title+company+location only. | Cards from non-LinkedIn sources render `_(no description)_` for both Experience and Job desc sections. |
| Stored `fit_score` includes baked +25 freshness | `_live_score` contract assumption | Live-recount slightly over-subtracts for missing-date postings (they were baked at +12, not +25). Acceptable error: ~13 points on rare rows. |
| `score_company` no longer counted | 2026-05-26 rebalance dropped it | Energy/industrial postings rely on geo + freshness + intern_desc. Halliburton-class still passes via the +5 intern signal. |
| Indonesian language signals not detected for intern desc | `INTERN_DESC_SIGNALS` is English-only | Add `"program magang"`, `"lulusan baru"`, `"fresh graduate"` if needed. |
| Astra "Otopprentice" niche detected via desc only | Title is a branded program name, not a recognizable role | +8 desc-only niche keeps it at 58 (MED). Won't reach HIGH without title containing AI/ML/agent keywords. |
| Discord `custom_id` 100-char limit | Hard Discord rule | Triage buttons hash `dedup_key` to `sha1[:12]` and reverse-look-up via brute-force scan of seen_v2 + triage. Slow at scale (>10k rows), fine at current scale (<1k). |
| Sub-threshold rows fill seen_v2 over time | `filtered=1` audit rows accumulate | Periodically run `DELETE FROM seen_v2 WHERE filtered=1 AND first_seen < strftime('%s','now','-30 days')` if storage matters (state.db is <1MB at current rate). |
| Cron path uses `$HOME` literally | Symlink resolves so it works | If you ever move the clone or change usernames, the cron entry still resolves via the symlink. |

---

## Don't

- **Don't commit `state.db` or `*.bak.*`.** `.gitignore` covers them — verify before pushing.
- **Don't hardcode channel IDs or tokens.** All Discord IDs come from env vars at `~/.hermes/.env`.
- **Don't change `dedup_key` formatting in one half without the other.** It's the join key for everything.
- **Don't import across `skill/` ↔ `plugin/` directly.** Use the `sys.path` injection pattern in `plugin/dashboard.py` / `plugin/db.py`.
- **Don't add backwards-compat shims for missing columns.** Use the migration pattern in `_migrate_seen_v2` (both halves).
- **Don't break the no-em-dash rule** in user-facing copy (Discord card descriptions, embed titles). Em dashes leak the AI tone.
- **Don't reintroduce shared `custom_id`s** when adding pagination. Use `jm:noop:<view>:<dir>` for disabled boundary buttons.
- **Don't change the freshness tier values in `scoring.py`** without also updating `_live_score` in `plugin/dashboard.py` AND the assumed-baked constant (currently 25).
- **Don't run `run.py` without `--dry-run` while testing** unless you want Discord posts.
- **Don't delete `state.db` casually** — it drops `triage` (your saved/applied list) too.
- **Don't add stack keywords without word boundaries.** Bare substring like `r"java"` would match "javascript" — always use `r"\bjava\b"`.

---

## Troubleshooting

| Symptom | Likely cause | Diagnosis |
|---|---|---|
| `0 unique` every run | `seen_v2` already has every posting in the current `hours_old` window | Expected. Wait for fresh listings or `rm state.db`. |
| `0 new postings` and nothing in Discord | All scraped rows were duplicates AND no heartbeat channel set | `tail ~/.logs/job-watcher.log` to confirm cron ran. Set `DISCORD_JOB_HEARTBEAT_CHANNEL` for visibility. |
| LinkedIn returns 0 rows | Proxy missing/expired | Check `CRAWLER_HTTPS_PROXY` in `~/.hermes/.env` or `~/karakeep/.env`. Test: `curl --proxy $PROXY https://www.linkedin.com` should return HTML. |
| `[glassdoor] error: not available for INDONESIA` | Upstream restriction | Ignore. Glassdoor stays in the rotation. |
| Plugin doesn't see new rows | Symlink broken | `ls -la ~/.agents/skills/job-watcher` should show `→ ~/job-scout/skill`. Re-run `~/job-scout/install.sh`. |
| Description bullets are empty for delivered postings | `summarize.py` timeout or hermes CLI broken | Check `which hermes` works on VPS. Try a manual oneshot: `hermes -z "say PONG" --provider copilot -m gpt-5.4 --ignore-rules --ignore-user-config`. Expected: `PONG`. |
| Button click does nothing | Plugin not loaded or listener not attached | `grep "attached on_interaction" ~/.hermes/logs/agent.log \| tail -1` — should show today's date. If not, `sudo systemctl restart hermes-gateway`. |
| Button click returns "⚠ Not allowed" | User not in `DISCORD_ALLOWED_USERS` allowlist | Add the Discord user ID to `~/.hermes/.env`. |
| "Component custom id cannot be duplicated" error in Discord | Two buttons in the same message share a `custom_id` | Likely a pagination boundary case. Check `jm:noop:*` is used for disabled buttons. |
| Posting got `[?]` score in History | Old row migrated without a fit_score backfill | Run the scoring backfill (see migration history). New scrapes get scores correctly. |
| `/job` shows "⚠ Failed to switch view" | Plugin code error | Tail `~/.hermes/logs/agent.log` during the click for the traceback. |
| Astra-class posting still missing despite catch fixes | JobSpy genuinely didn't return it from any of the 8 queries | Confirm with: `cd ~/job-scout/skill/.venv/bin && python /tmp/probe_astra.py` (see Astra probe script in repo history). If JobSpy returns 0, the posting fell off LinkedIn's `it intern` rankings — out of our control. |

---

## Quick start for a new agent

1. Read this entire file (you're already here).
2. Skim `README.md` for the human-facing pitch + architecture diagram.
3. Skim `skill/config/queries.yaml` to see the actual queries and filters in use.
4. Skim `skill/bin/scoring.py` for the formula implementation.
5. If editing code: edit on Mac in `/Users/absolutepraya/Documents/Projects/Job Watcher/watcher`, commit, push, then `ssh vps 'cd ~/job-scout && git pull'`. Restart hermes-gateway only if `plugin/` changed.
6. If debugging Discord: check `~/.hermes/logs/agent.log` on VPS.
7. If debugging cron: check `~/.logs/job-watcher.log` on VPS.
8. If unsure about scoring: run `cd skill/bin && python -m pytest test_scoring.py -v` to confirm tests still pass and to inspect the test expectations.
