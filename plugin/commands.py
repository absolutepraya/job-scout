"""Command handlers for job-manage."""
import time

import db as db_mod
import config as config_mod
from url_utils import strip_brackets, canonicalize_url


COMMAND_ALIASES = {
    "save", "unsave", "applied", "unapply", "dismiss", "apply",
    "saved", "stats", "recent", "help", "config",
    "dismissed", "list",
}


def parse_command(text: str) -> dict:
    """Parse a !command line. Returns dict with cmd + relevant fields."""
    text = text.strip()
    if not text.startswith("!"):
        return {"cmd": "_not_command"}
    body = text[1:].strip()
    parts = body.split(maxsplit=1)
    if not parts:
        return {"cmd": "_unknown"}
    cmd = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    if cmd not in COMMAND_ALIASES:
        return {"cmd": "_unknown"}

    if cmd == "applied" and rest.startswith("list"):
        return {"cmd": "applied_list"}
    if cmd == "dismissed" and rest.startswith("list"):
        return {"cmd": "dismissed_list"}
    if cmd == "config":
        return _parse_config(rest)
    if cmd == "help":
        args = rest.split() if rest else []
        return {"cmd": "help", "args": args}
    if cmd == "stats":
        return {"cmd": "stats"}
    if cmd == "saved":
        return {"cmd": "saved"}
    if cmd == "recent":
        n = 5
        if rest:
            try:
                n = int(rest.split()[0])
            except (ValueError, IndexError):
                pass
        return {"cmd": "recent", "n": n}
    if cmd == "list":
        n = 10
        if rest:
            try:
                n = int(rest.split()[0])
            except (ValueError, IndexError):
                pass
        return {"cmd": "list", "n": n}

    # URL-bearing commands
    note = None
    if " -- " in rest:
        url_part, note = rest.split(" -- ", 1)
    else:
        url_part = rest
    urls = [canonicalize_url(strip_brackets(u)) for u in url_part.split() if u]
    return {"cmd": cmd, "urls": urls, "note": note}


def _parse_config(rest: str) -> dict:
    if not rest:
        return {"cmd": "config_dump"}
    parts = rest.split(maxsplit=2)
    sub = parts[0].lower()
    if sub == "get" and len(parts) >= 2:
        return {"cmd": "config_get", "key": parts[1]}
    if sub == "set" and len(parts) >= 3:
        return {"cmd": "config_set", "key": parts[1], "value": parts[2]}
    if sub == "list" and len(parts) >= 2:
        return {"cmd": "config_list_show", "key": parts[1]}
    if sub == "add" and len(parts) >= 3:
        return {"cmd": "config_add", "key": parts[1], "value": parts[2]}
    if sub == "rm" and len(parts) >= 3:
        return {"cmd": "config_rm", "key": parts[1], "value": parts[2]}
    if sub == "reset" and len(parts) >= 2:
        return {"cmd": "config_reset", "key": parts[1]}
    return {"cmd": "config_help"}


def _format_entry(t: dict) -> str:
    score = t.get("score") or 0
    title = (t.get("title") or "?")[:80]
    co = (t.get("company") or "?")[:40]
    url = t.get("job_url", "")
    return f"⭐ {score} | {title} @ {co}\n<{url}>"


def _humanize_age(ts) -> str:
    if not ts:
        return "?"
    delta = int(time.time()) - int(ts)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


# --- Triage commands ---

def cmd_save(conn, urls, note):
    if not urls:
        return "Usage: !save <url> [-- note]. Type !help save for details."
    results = []
    for url in urls:
        url = canonicalize_url(url)
        snap = db_mod.lookup_seen_v2(conn, url)
        if not snap:
            results.append(f"⚠ {url[:60]}… — not found in scrape history (outside 7-day window or external URL).")
            continue
        existing = db_mod.get_triage(conn, snap["dedup_key"])
        if existing:
            if existing["status"] == "saved":
                results.append(f"🔖 Already saved ({_humanize_age(existing['saved_at'])}): {snap['title']} @ {snap['company']}")
                continue
            if existing["status"] == "applied":
                results.append(f"✅ Already applied {_humanize_age(existing['applied_at'])}. Use !unapply first if you want to re-save: {snap['title']} @ {snap['company']}")
                continue
            db_mod.upsert_triage(conn, snap, "saved", notes=note)
            results.append(f"🚫 Was dismissed {_humanize_age(existing['dismissed_at'])}. Saving anyway: {snap['title']} @ {snap['company']}")
            continue
        db_mod.upsert_triage(conn, snap, "saved", notes=note)
        score_str = f"⭐ {snap.get('score') or 0}"
        results.append(f"🔖 Saved: {snap['title']} @ {snap['company']} ({score_str})")
    return "\n".join(results)


def cmd_unsave(conn, urls):
    if not urls:
        return "Usage: !unsave <url>"
    results = []
    for url in urls:
        url = canonicalize_url(url)
        snap = db_mod.lookup_seen_v2(conn, url)
        if not snap:
            results.append(f"⚠ {url[:60]}… — not in scrape history.")
            continue
        n = db_mod.delete_triage(conn, snap["dedup_key"])
        if n == 0:
            results.append(f"🗑️ {snap['title']} @ {snap['company']} was not saved.")
        else:
            results.append(f"🗑️ Removed from saved: {snap['title']} @ {snap['company']}")
    return "\n".join(results)


def cmd_applied(conn, urls):
    if not urls:
        return "Usage: !applied <url>"
    results = []
    for url in urls:
        url = canonicalize_url(url)
        snap = db_mod.lookup_seen_v2(conn, url)
        if not snap:
            results.append(f"⚠ {url[:60]}… — not in scrape history.")
            continue
        db_mod.upsert_triage(conn, snap, "applied")
        results.append(f"✅ Marked applied: {snap['title']} @ {snap['company']}")
    return "\n".join(results)


def cmd_unapply(conn, urls):
    if not urls:
        return "Usage: !unapply <url>"
    results = []
    for url in urls:
        url = canonicalize_url(url)
        snap = db_mod.lookup_seen_v2(conn, url)
        if not snap:
            results.append(f"⚠ {url[:60]}… — not in scrape history.")
            continue
        existing = db_mod.get_triage(conn, snap["dedup_key"])
        if not existing or existing["status"] != "applied":
            results.append(f"↩️ {snap['title']} @ {snap['company']} is not in applied state.")
            continue
        db_mod.upsert_triage(conn, snap, "saved")
        results.append(f"↩️ Reverted to saved: {snap['title']} @ {snap['company']}")
    return "\n".join(results)


def cmd_dismiss(conn, urls):
    if not urls:
        return "Usage: !dismiss <url>"
    results = []
    for url in urls:
        url = canonicalize_url(url)
        snap = db_mod.lookup_seen_v2(conn, url)
        if not snap:
            results.append(f"⚠ {url[:60]}… — not in scrape history.")
            continue
        db_mod.upsert_triage(conn, snap, "dismissed")
        results.append(f"🚫 Dismissed: {snap['title']} @ {snap['company']}")
    return "\n".join(results)


def cmd_apply(conn, urls):
    return "🚧 Auto-apply not yet built. Use !applied <url> after manual apply."


# --- List commands ---

def cmd_saved(conn):
    rows = db_mod.list_by_status(conn, "saved")
    if not rows:
        return "🔖 No saved jobs yet. Save one with !save <url>."
    lines = [f"## 🔖 Saved ({len(rows)})", ""]
    for r in rows[:50]:
        lines.append(_format_entry(r))
    if len(rows) > 50:
        lines.append(f"\n(showing 50 of {len(rows)} — use !recent <n> to filter)")
    return "\n".join(lines)


def cmd_applied_list(conn):
    rows = db_mod.list_by_status(conn, "applied")
    if not rows:
        return "✅ No applied jobs yet."
    lines = [f"## ✅ Applied ({len(rows)})", ""]
    for r in rows[:50]:
        lines.append(_format_entry(r))
    if len(rows) > 50:
        lines.append(f"\n(showing 50 of {len(rows)})")
    return "\n".join(lines)


def cmd_dismissed_list(conn):
    rows = db_mod.list_by_status(conn, "dismissed")
    if not rows:
        return "🚫 No dismissed jobs."
    lines = [f"## 🚫 Dismissed ({len(rows)})", ""]
    for r in rows[:50]:
        lines.append(_format_entry(r))
    if len(rows) > 50:
        lines.append(f"\n(showing 50 of {len(rows)})")
    return "\n".join(lines)


def cmd_recent(conn, n):
    rows = db_mod.list_by_status(conn, "saved", limit=n)
    if not rows:
        return "🔖 No saved jobs."
    lines = [f"## 🔖 Last {len(rows)} saved", ""]
    for r in rows:
        lines.append(_format_entry(r))
    return "\n".join(lines)


def cmd_list(conn, n):
    """List last N untriaged postings from scrape history."""
    rows = conn.execute(
        "SELECT dedup_key, job_url, title, company, location, site, first_seen "
        "FROM seen_v2 WHERE dedup_key NOT IN (SELECT dedup_key FROM triage) "
        "ORDER BY first_seen DESC LIMIT ?",
        (int(n),)
    ).fetchall()
    if not rows:
        return "📋 No untriaged postings in scrape history."
    lines = [f"## 📋 Scrape history (last {len(rows)} untriaged)", ""]
    for r in rows:
        title = (r[2] or "?")[:80]
        co = (r[3] or "?")[:40]
        url = r[1] or ""
        lines.append(f"• {title} @ {co}")
        lines.append(f"  <{url}>")
    return "\n".join(lines)


def cmd_stats(conn):
    c = db_mod.count_by_status(conn)
    return f"🔖 {c['saved']} saved · ✅ {c['applied']} applied · 🚫 {c['dismissed']} dismissed · 📋 {c['seen']} in scrape history"


# --- Config commands ---

def cmd_config_dump():
    lines = ["## ⚙️ Editable config", ""]
    for key in config_mod.EDITABLE_KEYS:
        v = config_mod.get_value(key)
        lines.append(f"`{key}` = `{v}`")
    return "\n".join(lines)


def cmd_config_get(key):
    if key not in config_mod.EDITABLE_KEYS:
        return f"⚠ {key} not editable. Editable keys: {', '.join(config_mod.EDITABLE_KEYS)}"
    v = config_mod.get_value(key)
    return f"`{key}` = `{v}`"


def cmd_config_set(key, value):
    try:
        if config_mod.EDITABLE_KEYS.get(key) == "int":
            old, new = config_mod.set_value(key, value)
        else:
            return f"⚠ Use !config add/rm for list configs. !config set is for scalars (int) only."
    except config_mod.ValidationError as e:
        return f"❌ {e}"
    return f"✓ `{key}`: {old} → {new}"


def cmd_config_list_show(key):
    if key not in config_mod.EDITABLE_KEYS:
        return f"⚠ {key} not editable."
    if config_mod.EDITABLE_KEYS[key] != "list":
        return f"⚠ {key} is not a list config. Use !config get {key}."
    v = config_mod.get_value(key) or []
    if not v:
        return f"`{key}` is empty."
    lines = [f"## ⚙️ {key} ({len(v)} entries)", ""]
    for entry in v:
        lines.append(f"• {entry}")
    return "\n".join(lines)


def cmd_config_add(key, value):
    try:
        old, new = config_mod.list_add(key, value)
    except config_mod.ValidationError as e:
        return f"❌ {e}"
    return f"✓ Added to `{key}`. Now {len(new)} entries."


def cmd_config_rm(key, value):
    try:
        old, new = config_mod.list_rm(key, value)
    except config_mod.ValidationError as e:
        return f"❌ {e}"
    return f"✓ Removed from `{key}`. Now {len(new)} entries."


def cmd_config_reset(key):
    try:
        old, new = config_mod.reset(key)
    except config_mod.ValidationError as e:
        return f"❌ {e}"
    return f"✓ `{key}` reset: {old} → {new}"


def cmd_config_help():
    return (
        "## ⚙️ Config commands\n"
        "• `!config` — list all editable keys + values\n"
        "• `!config get <key>` — print one value\n"
        "• `!config set <key> <value>` — set scalar (int)\n"
        "• `!config list <key>` — show list-type entries\n"
        "• `!config add <key> <value>` — append to list\n"
        "• `!config rm <key> <value>` — remove from list\n"
        "• `!config reset <key>` — restore default\n"
        "\nEditable: " + ", ".join(config_mod.EDITABLE_KEYS)
    )


# --- Help ---

HELP_DETAIL = {
    "save": "Usage: `/job save <url> [<url2>...] [-- note]`. Bookmark posting(s). URLs must be in scrape history.",
    "unsave": "Usage: `/job unsave <url>`. Remove from saved (deletes row entirely).",
    "applied": "Usage: `/job applied <url>` to mark applied, or `/job applied list` to list all applied.",
    "unapply": "Usage: `/job unapply <url>`. Revert applied → saved.",
    "dismiss": "Usage: `/job dismiss <url>`. Hide from future digests permanently.",
    "apply": "Usage: `/job apply <url>`. Auto-apply via Playwright (Phase 2.5 — not yet built).",
    "saved": "Usage: `/job saved`. List all saved jobs.",
    "recent": "Usage: `/job recent [N]`. Last N saved (default 5).",
    "list": "Usage: `/job list [N]`. Last N untriaged postings from scrape history (default 10).",
    "stats": "Usage: `/job stats`. Print counts.",
    "config": "Usage: `/job config [get/set/list/add/rm/reset] [key] [value]`. Type `/job config` for current values.",
    "help": "Usage: `/job help` or `/job help <command>`.",
}


def cmd_help(args):
    if not args:
        return (
            "## 💼 job-manage commands\n"
            "**Triage:** `/job save` `/job unsave` `/job applied` `/job unapply` `/job dismiss` `/job apply`\n"
            "**Lists:** `/job saved` `/job applied list` `/job dismissed list` `/job list` `/job recent` `/job stats`\n"
            "**Config:** `/job config` `/job config get/set/list/add/rm/reset`\n"
            "**Meta:** `/job help` `/job help <command>`\n"
            "\nMost commands accept multiple URLs in one message. URLs in `<>` brackets are stripped automatically."
        )
    target = args[0].lower()
    return HELP_DETAIL.get(target, f"⚠ Unknown command: /job {target}. Type /job help for the list.")
