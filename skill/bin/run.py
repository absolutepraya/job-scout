#!/usr/bin/env python3
"""job-watcher: scrape, filter, score, dedup, group, format, deliver."""
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

import yaml
from jobspy import scrape_jobs

SKILL_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = SKILL_DIR / "bin"
CONFIG_PATH = SKILL_DIR / "config" / "queries.yaml"
STATE_PATH = SKILL_DIR / "state.db"

sys.path.insert(0, str(BIN_DIR))
from scoring import compute_fit_score, classify_workmode, get_flag
try:
    from summarize import summarize as _summarize_desc
except Exception:
    _summarize_desc = lambda desc: ([], [])


def _read_karakeep_proxy():
    karakeep_env = Path.home() / "karakeep" / ".env"
    if not karakeep_env.exists():
        return None
    for line in karakeep_env.read_text().splitlines():
        if line.startswith("CRAWLER_HTTPS_PROXY="):
            return line.split("=", 1)[1].strip()
    return None


PROXY = os.environ.get("KARAKEEP_PROXY") or _read_karakeep_proxy()


def _safe_str(v) -> str:
    if v is None:
        return ""
    s = str(v)
    if s.lower() == "nan":
        return ""
    return s


_INDO_KW = ("indonesia", "jakarta", "tangerang", "bandung", "depok",
            "bekasi", "bali", "surabaya", "yogyakarta", "semarang", "medan")


def _is_local_str(loc):
    if not loc:
        return 0
    l = loc.lower()
    return 1 if any(kw in l for kw in _INDO_KW) else 0


def _migrate_seen_v2(conn):
    """Idempotent: add query_term/fit_score/is_local/exp_bullets/jobdesc_bullets columns + backfill is_local."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(seen_v2)").fetchall()}
    if "query_term" not in cols:
        conn.execute("ALTER TABLE seen_v2 ADD COLUMN query_term TEXT")
    if "fit_score" not in cols:
        conn.execute("ALTER TABLE seen_v2 ADD COLUMN fit_score INTEGER")
    if "is_local" not in cols:
        conn.execute("ALTER TABLE seen_v2 ADD COLUMN is_local INTEGER")
    if "exp_bullets" not in cols:
        conn.execute("ALTER TABLE seen_v2 ADD COLUMN exp_bullets TEXT")
    if "jobdesc_bullets" not in cols:
        conn.execute("ALTER TABLE seen_v2 ADD COLUMN jobdesc_bullets TEXT")
    rows = conn.execute("SELECT rowid, location FROM seen_v2 WHERE is_local IS NULL").fetchall()
    for rowid, loc in rows:
        conn.execute("UPDATE seen_v2 SET is_local = ? WHERE rowid = ?", (_is_local_str(loc), rowid))
    conn.commit()


def init_db():
    conn = sqlite3.connect(STATE_PATH, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_v2 (
            dedup_key TEXT PRIMARY KEY,
            job_url TEXT,
            title TEXT,
            company TEXT,
            location TEXT,
            site TEXT,
            first_seen INTEGER
        )
    """)
    conn.commit()
    return conn


def title_passes(title, must_any: list, must_not: list) -> bool:
    t = _safe_str(title).lower()
    if must_any and not any(kw.lower() in t for kw in must_any):
        return False
    if must_not and any(kw.lower() in t for kw in must_not):
        return False
    return True


def company_passes(company, must_not: list) -> bool:
    if not must_not:
        return True
    c = _safe_str(company).lower()
    return not any(kw.lower() in c for kw in must_not)


def dedup_key(title, company) -> str:
    return f"{_safe_str(title).lower().strip()}|{_safe_str(company).lower().strip()}"


def fetch_site(site: str, query: str, location: dict, limit: int, hours_old: int):
    try:
        kwargs = dict(
            site_name=[site],
            search_term=query,
            location=location["name"],
            results_wanted=limit,
            hours_old=hours_old,
            proxies=[PROXY] if PROXY else None,
        )
        if site in ("indeed", "glassdoor"):
            kwargs["country_indeed"] = location.get("country_indeed", "USA")
        if location.get("is_remote"):
            kwargs["is_remote"] = True
        if site == "linkedin":
            kwargs["linkedin_fetch_description"] = True
        df = scrape_jobs(**kwargs)
        return df.to_dict("records") if df is not None and len(df) > 0 else []
    except Exception as e:
        print(f"  [{site}] error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return []


def _age_str(date_posted) -> str:
    s = _safe_str(date_posted)
    if not s:
        return "?"
    try:
        from datetime import datetime, timezone
        posted = datetime.fromisoformat(s[:10]).replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        days = (now - posted).days
        if days < 1:
            return "today"
        if days == 1:
            return "1d"
        return f"{days}d"
    except Exception:
        return "?"


WORKMODE_DISPLAY = {
    "remote":   ("ᯤ", "remote"),
    "onsite":   ("⚲", "onsite"),
    "relocate": ("✈︎", "abroad"),
}


def _format_location(location: str) -> str:
    """Shorten location string. 'Jakarta, Jakarta, Indonesia' -> 'Jakarta, Indonesia'."""
    if not location:
        return "?"
    parts = [p.strip() for p in location.split(",") if p.strip()]
    if len(parts) >= 3:
        return f"{parts[0]}, {parts[-1]}"
    return location


import hashlib as _hashlib


# Color palette (kept in sync with plugin interactions.py).
# Tier names are score-tiers, not paint names — high>=70, med 50-69, low<50.
COLOR_HIGH = 0x377AAF   # score >=70
COLOR_MED  = 0x3BBBB3   # score 50-69
COLOR_LOW  = 0x79DC96   # score <50


def _short_id(dedup_key: str) -> str:
    return _hashlib.sha1(dedup_key.encode("utf-8")).hexdigest()[:12]


def _color_for_score(score: int) -> int:
    if score >= 70:
        return COLOR_HIGH
    if score >= 50:
        return COLOR_MED
    return COLOR_LOW


def _age_display(age: str) -> str:
    """Capitalize 'today' -> 'Today'. Leave others as-is."""
    if age == "today":
        return "Today"
    return age


def format_posting_card(posting: dict) -> dict:
    """Return a Discord message payload (embed + buttons) for one posting."""
    title = _safe_str(posting.get("title")) or "?"
    if len(title) > 200:
        title = title[:197] + "…"
    company = _safe_str(posting.get("company")) or "?"
    if len(company) > 60:
        company = company[:57] + "…"
    url = _safe_str(posting.get("job_url"))
    location = _safe_str(posting.get("location"))
    is_remote_flag = posting.get("is_remote") is True
    flag = get_flag(location, is_remote=is_remote_flag)
    loc_text = "Remote" if (is_remote_flag and not location) else _format_location(location)
    workmode = classify_workmode(posting)
    glyph, mode_word = WORKMODE_DISPLAY[workmode]
    age = _age_display(_age_str(posting.get("date_posted")))
    stack = posting.get("_stack_matches", []) or []
    score = posting.get("_fit_score", 0)
    dk = dedup_key(title, company)
    sid = _short_id(dk)

    # Line 1: location + workmode
    line1 = f"{flag} {loc_text} ({glyph} {mode_word})"

    # Line 2: age · stack (skip entirely if both unknown)
    parts = []
    if age and age != "?":
        parts.append(age)
    if stack:
        stack_part = " ".join(f"`{s}`" for s in stack[:3])
        parts.append(f"Stack: {stack_part}")
    line2 = " · ".join(parts) if parts else ""

    desc_lines = [line1]
    if line2:
        desc_lines.append(line2)
    exp_bullets = posting.get("_exp_bullets") or []
    jobdesc_bullets = posting.get("_jobdesc_bullets") or []
    desc_lines.append("")
    desc_lines.append("**Experience:**")
    if exp_bullets:
        for b in exp_bullets:
            desc_lines.append(f"- {b}")
    else:
        desc_lines.append("- _(no description)_")
    desc_lines.append("")
    desc_lines.append("**Job desc:**")
    if jobdesc_bullets:
        for b in jobdesc_bullets:
            desc_lines.append(f"- {b}")
    else:
        desc_lines.append("- _(no description)_")

    embed = {
        "title": f"[{score}] {title} @ {company}",
        "url": url or None,
        "description": "\n".join(desc_lines),
        "color": _color_for_score(score),
    }
    # Discord rejects empty url
    if not embed["url"]:
        del embed["url"]

    components = [{
        "type": 1,
        "components": [
            {"type": 2, "style": 2, "custom_id": f"jm:save:{sid}", "emoji": {"name": "💾"}},
            {"type": 2, "style": 2, "custom_id": f"jm:dismiss:{sid}", "emoji": {"name": "❌"}},
            {"type": 2, "style": 2, "custom_id": f"jm:applied:{sid}", "emoji": {"name": "✅"}},
        ]
    }]
    if url:
        components[0]["components"].append({"type": 2, "style": 5, "url": url, "emoji": {"name": "🔗"}})

    return {"embeds": [embed], "components": components}


def format_digest(new_postings: list, total_scraped: int, locations_count: int) -> list:
    """Returns list of Discord message payloads.
    Each item is either {"content": "..."} (text divider) or {"embeds":..., "components":...} (posting card).
    """
    from datetime import datetime
    now = datetime.now()
    date_str = now.strftime("%d %B %Y · %H:%M WIB")

    if not new_postings:
        return [{
            "content": (
                f"## 🆕 0 new postings\n"
                f"{date_str} · {total_scraped} scraped · 0 unique · 4 sites · {locations_count} locations"
            )
        }]

    local_postings = [p for p in new_postings if p.get("_is_local")]
    oversea_postings = [p for p in new_postings if not p.get("_is_local")]

    messages = []

    # M1: Header
    messages.append({
        "content": (
            f"## 🆕 {len(new_postings)} new postings\n"
            f"{date_str} · {total_scraped} scraped · {len(new_postings)} unique · 4 sites · {locations_count} locations\n\n"
            f"Workmode legend:\n  ⚲ onsite\n  ᯤ remote\n  ✈︎ abroad"
        )
    })

    # Local section
    if local_postings:
        messages.append({"content": f"## 🇮🇩 Local ({len(local_postings)})"})
        role_buckets_local = {}
        for p in local_postings:
            role_buckets_local.setdefault(p["_query"], []).append(p)
        for role, items in role_buckets_local.items():
            items.sort(key=lambda x: (-x["_fit_score"], _safe_str(x.get("date_posted"))))
            messages.append({"content": f"`{role.upper()}` ({len(items)})"})
            for p in items:
                messages.append(format_posting_card(p))

    # Oversea per role
    if oversea_postings:
        role_buckets_o = {}
        for p in oversea_postings:
            role_buckets_o.setdefault(p["_query"], []).append(p)
        for role, items in role_buckets_o.items():
            items.sort(key=lambda x: (-x["_fit_score"], _safe_str(x.get("date_posted"))))
            messages.append({"content": f"## 🌍 Oversea · `{role.upper()}` ({len(items)})"})
            for p in items:
                messages.append(format_posting_card(p))

    return messages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="don't update state.db, don't post")
    ap.add_argument("--site", help="restrict to one site")
    args = ap.parse_args()

    if not PROXY:
        print("WARNING: no proxy set", file=sys.stderr)

    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    sites = [args.site] if args.site else ["linkedin", "indeed", "glassdoor", "google"]

    conn = init_db()
    _migrate_seen_v2(conn)
    cur = conn.cursor()
    new_postings = []
    total_scraped = 0

    print(f"[{time.strftime('%H:%M:%S')}] starting job-watcher v3", flush=True)
    print(f"  sites: {sites}", flush=True)
    print(f"  queries: {cfg['queries']}", flush=True)

    for query in cfg["queries"]:
        for location in cfg["locations"]:
            tier = location.get("tier", 1)
            loc_sites = sites if tier == 1 else [s for s in sites if s == "linkedin"]
            limits_map = cfg["limits"] if tier == 1 else cfg.get("limits_tier2", cfg["limits"])
            for site in loc_sites:
                limit = limits_map.get(site, 10)
                print(f"\n[{time.strftime('%H:%M:%S')}] {site} | '{query}' | {location['name']} (t{tier})", flush=True)
                records = fetch_site(site, query, location, limit, cfg["hours_old"])
                print(f"  fetched {len(records)} rows", flush=True)
                total_scraped += len(records)

                min_score = int(cfg.get("min_score", 0))
                workmode_blacklist = [w.lower() for w in cfg.get("workmode_blacklist", [])]
                for rec in records:
                    url = rec.get("job_url") or rec.get("job_url_direct")
                    title = _safe_str(rec.get("title"))
                    company = _safe_str(rec.get("company"))
                    if not title or not company:
                        continue
                    if not title_passes(title, cfg.get("title_must_match_any", []), cfg.get("title_must_not_match", [])):
                        continue
                    if not company_passes(company, cfg.get("company_must_not_match", [])):
                        continue
                    key = dedup_key(title, company)
                    cur.execute("SELECT 1 FROM seen_v2 WHERE dedup_key = ?", (key,))
                    if cur.fetchone():
                        continue
                    # Skip if already triaged (saved/applied/dismissed)
                    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='triage'")
                    if cur.fetchone():
                        cur.execute("SELECT 1 FROM triage WHERE dedup_key = ?", (key,))
                        if cur.fetchone():
                            # Compute these for the seen_v2 row even though we won't deliver
                            workmode = classify_workmode(rec)
                            fit, _stack, is_local_posting = compute_fit_score(rec)
                            if not args.dry_run:
                                cur.execute(
                                    "INSERT OR IGNORE INTO seen_v2 "
                                    "(dedup_key, job_url, title, company, location, site, first_seen, query_term, fit_score, is_local) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    (key, url, title, company, rec.get("location"), site, int(time.time()),
                                     query, fit, 1 if is_local_posting else 0),
                                )
                            continue
                    workmode = classify_workmode(rec)
                    if workmode in workmode_blacklist:
                        continue
                    fit, stack_matches, is_local_posting = compute_fit_score(rec)
                    if fit < min_score:
                        continue
                    rec["_query"] = query
                    rec["site"] = site
                    rec["_fit_score"] = fit
                    rec["_stack_matches"] = stack_matches
                    rec["_is_local"] = is_local_posting
                    # Summarize description into brief bullets via hermes oneshot.
                    # Empty lists on failure / no description — card renders "(no description)".
                    description = str(rec.get("description") or "")
                    exp_b, jd_b = _summarize_desc(description)
                    rec["_exp_bullets"] = exp_b
                    rec["_jobdesc_bullets"] = jd_b
                    new_postings.append(rec)
                    if not args.dry_run:
                        cur.execute(
                            "INSERT OR IGNORE INTO seen_v2 "
                            "(dedup_key, job_url, title, company, location, site, first_seen, query_term, fit_score, is_local, exp_bullets, jobdesc_bullets) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (key, url, title, company, rec.get("location"), site, int(time.time()),
                             query, fit, 1 if is_local_posting else 0,
                             "\n".join(exp_b) if exp_b else None,
                             "\n".join(jd_b) if jd_b else None),
                        )
                time.sleep(2)

    if not args.dry_run:
        conn.commit()
    conn.close()

    messages = format_digest(new_postings, total_scraped, len(cfg["locations"]))
    for msg in messages:
        print("=" * 60)
        if isinstance(msg, dict):
            if "content" in msg:
                print(msg["content"])
            elif "embeds" in msg:
                e = msg["embeds"][0]
                print(f"[CARD] {e.get('title')}")
                print(e.get('description', ''))
        else:
            print(msg)

    if not args.dry_run:
        try:
            import notify
            if new_postings:
                # Normal digest -> main job-watcher channel (env-default).
                notify.send_sections(messages)
            else:
                # No new postings today -> small heartbeat to the heartbeat
                # channel so we know the cron actually ran.
                from datetime import datetime as _dt
                _stamp = _dt.now().strftime("%d %b %Y · %H:%M WIB")
                heartbeat = (
                    f"🟢 job-watcher heartbeat · {_stamp}\n"
                    f"{total_scraped} scraped · 0 new (all duplicates) · "
                    f"{len(cfg['locations'])} location · 4 sites"
                )
                hb_channel = os.environ.get("DISCORD_JOB_HEARTBEAT_CHANNEL")
                notify.send_sections([heartbeat], channel_id_override=hb_channel)
        except Exception as e:
            print(f"[notify] failed: {type(e).__name__}: {e}", file=sys.stderr)

    return messages


if __name__ == "__main__":
    main()
