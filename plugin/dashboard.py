"""job-manage dashboard — view builders for /job in #manage.

Each builder returns a Discord message payload dict:
    {"embeds": [embed_dict], "components": [action_row_dict, ...]}

Per-view button layout:
  Menu    : all 9 navigation buttons + Refresh
  List/Saved/Applied/Dismissed : pagination (Newer / Older) + Menu
  History : day-nav (Older Day / Newer Day) + Menu
  Stats/Config/Help : Menu only

custom_id formats:
  jm:dash:menu
  jm:dash:list:<page>            — page is 0-indexed integer
  jm:dash:saved:<page>
  jm:dash:applied:<page>
  jm:dash:dismissed:<page>
  jm:dash:hist:<yyyymmdd>        — date string YYYYMMDD; "today" if omitted
  jm:dash:stats / :config / :help / :refresh

Phase-1 scope: read-only. List entries sorted by stored score DESC. Score is a
snapshot at save-time — freshness component decays in reality, but we accept
this trade-off for simplicity (see Phase-2 schema notes in the plan).
"""
import sys
import time as _time
from datetime import datetime, timedelta
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

import db as db_mod
import config as config_mod
import commands as cmd_mod

# Make scoring helpers importable for the get_flag function
_JW_BIN = Path.home() / ".agents" / "skills" / "job-watcher" / "bin"
if _JW_BIN.is_dir() and str(_JW_BIN) not in sys.path:
    sys.path.insert(0, str(_JW_BIN))
try:
    from scoring import get_flag as _get_flag  # type: ignore
except Exception:
    def _get_flag(location: str, is_remote: bool = False) -> str:
        return "🏳"


def _escape_md(s: str) -> str:
    """Escape Discord markdown specials in user-supplied strings.
    Most important: underscores trigger italics. Also escape * and ~."""
    if not s:
        return ""
    for ch in ("\\", "_", "*", "~", "`"):
        s = s.replace(ch, "\\" + ch)
    return s


def _live_score(stored: int | None, first_seen_ts: int | None) -> int:
    """Recompute score at render time: replace baked-in freshness (assumed 25
    under the post-2026-05-26 formula) with live freshness derived from first_seen.

    Tiers must match scoring.score_freshness: <5d=25 / <10d=18 / <20d=12 / <30d=6 / older=0.
    Note: postings scraped without a date_posted had freshness baked at the default
    of 12, so this approximation slightly over-subtracts for those — accepted tradeoff.
    """
    if stored is None:
        return 0
    if first_seen_ts is None:
        return int(stored)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    posted = datetime.fromtimestamp(int(first_seen_ts), tz=timezone.utc)
    days = (now - posted).total_seconds() / 86400
    if days < 5:
        fresh = 25
    elif days < 10:
        fresh = 18
    elif days < 20:
        fresh = 12
    elif days < 30:
        fresh = 6
    else:
        fresh = 0
    return max(0, int(stored) - 25 + fresh)


# Colors
COLOR_MENU = 0x5C6772     # muted slate gray — neutral system tone for the main menu
COLOR_SUBVIEW = 0x5865F2  # blurple — sub-views (stats, history, config, help)

PAGE_SIZE = 15


# ---------- Button helpers ----------

def _btn(label, custom_id, emoji=None, style=2, disabled=False, url=None):
    """Build a Discord button component. The `emoji` arg is accepted for
    backward-compat with existing callers but intentionally NOT applied —
    we run text-label-only buttons across the dashboard.
    """
    b = {"type": 2, "style": style, "label": label, "disabled": disabled}
    if url:
        b["url"] = url
    else:
        b["custom_id"] = custom_id
    return b


def _row(*buttons):
    return {"type": 1, "components": list(buttons)}


def _menu_buttons():
    """Main dashboard: navigation buttons + Refresh."""
    return [
        _row(
            _btn("Saved",      "jm:dash:saved:0",     "🔖"),
            _btn("Applied",    "jm:dash:applied:0",   "✅"),
            _btn("Dismissed",  "jm:dash:dismissed:0", "❌"),
            _btn("History",    "jm:dash:hist:",       "📚"),
        ),
        _row(
            _btn("Stats",   "jm:dash:stats",   "📊"),
            _btn("Config",  "jm:dash:config",  "⚙️"),
            _btn("Help",    "jm:dash:help",    "❓"),
            _btn("Refresh", "jm:dash:refresh", "🔄"),
        ),
    ]


def _pagination_buttons(view: str, page: int, total_pages: int):
    """Newer / Older + Menu for list views.

    Disabled boundary buttons use unique jm:noop:* custom_ids so Discord
    does not reject the message for duplicated custom_id at the edges.
    """
    newer_disabled = page <= 0
    older_disabled = (page >= total_pages - 1) or total_pages == 0
    newer_target = max(0, page - 1)
    older_target = min(max(0, total_pages - 1), page + 1)
    newer_id = f"jm:noop:{view}:newer" if newer_disabled else f"jm:dash:{view}:{newer_target}"
    older_id = f"jm:noop:{view}:older" if older_disabled else f"jm:dash:{view}:{older_target}"
    return [
        _row(
            _btn("Newer", newer_id, "⬆️", disabled=newer_disabled),
            _btn("Older", older_id, "⬇️", disabled=older_disabled),
            _btn("Menu",  "jm:dash:menu", "⬅️", style=1),
        ),
    ]


def _history_buttons(
    current_date_arg: str,
    older_date_arg: str | None,
    newer_date_arg: str | None,
    page: int,
    total_pages: int,
):
    """History buttons:
       Row 1: Prev page / Next page (within current day)
       Row 2: Older day / Newer day / Menu

    Disabled boundary buttons (edges of page range or day range) use unique
    jm:noop:* custom_ids so Discord does not reject the message for duplicated
    custom_id values.
    """
    prev_disabled = (page <= 0) or total_pages == 0
    next_disabled = (page >= total_pages - 1) or total_pages == 0
    prev_page = max(0, page - 1)
    next_page = min(max(0, total_pages - 1), page + 1)

    prev_id = "jm:noop:hist:prev" if prev_disabled else f"jm:dash:hist:{current_date_arg}:{prev_page}"
    next_id = "jm:noop:hist:next" if next_disabled else f"jm:dash:hist:{current_date_arg}:{next_page}"

    older_id = "jm:noop:hist:older" if older_date_arg is None else f"jm:dash:hist:{older_date_arg}:0"
    newer_id = "jm:noop:hist:newer" if newer_date_arg is None else f"jm:dash:hist:{newer_date_arg}:0"

    return [
        _row(
            _btn("Prev page", prev_id, "⬅️", disabled=prev_disabled),
            _btn("Next page", next_id, "➡️", disabled=next_disabled),
        ),
        _row(
            _btn("Older day", older_id, "⬇️", disabled=older_date_arg is None),
            _btn("Newer day", newer_id, "⬆️", disabled=newer_date_arg is None),
            _btn("All",       "jm:dash:histall:0", "📜"),
            _btn("Menu",      "jm:dash:menu", "⬅️", style=1),
        ),
    ]


def _history_all_buttons(page: int, total_pages: int):
    """Pagination row + return-to-day-grouped row for the All-history view."""
    prev_disabled = (page <= 0) or total_pages == 0
    next_disabled = (page >= total_pages - 1) or total_pages == 0
    prev_target = max(0, page - 1)
    next_target = min(max(0, total_pages - 1), page + 1)
    prev_id = "jm:noop:histall:prev" if prev_disabled else f"jm:dash:histall:{prev_target}"
    next_id = "jm:noop:histall:next" if next_disabled else f"jm:dash:histall:{next_target}"
    return [
        _row(
            _btn("Prev page", prev_id, "⬅️", disabled=prev_disabled),
            _btn("Next page", next_id, "➡️", disabled=next_disabled),
        ),
        _row(
            _btn("By Day", "jm:dash:hist:", "📅"),
            _btn("Menu",   "jm:dash:menu", "⬅️", style=1),
        ),
    ]


def _menu_only_buttons():
    return [_row(_btn("Menu", "jm:dash:menu", "⬅️", style=1))]


def _applied_buttons(page: int, total_pages: int):
    """Pagination row + a second row with the Add-Manual button.

    Keeps pagination identical to other list views; the manual-add lives
    on its own row so it doesn't crowd the navigation controls.
    """
    pagi = _pagination_buttons("applied", page, total_pages)
    pagi.append(_row(_btn("Add Manual", "jm:add:applied", "➕", style=1)))
    return pagi


# ---------- Formatting helpers ----------

def _short_entry(t: dict, max_title: int = 70) -> str:
    score = t.get("score") or 0
    title = (t.get("title") or "?")
    if len(title) > max_title:
        title = title[:max_title - 1] + "…"
    co = (t.get("company") or "?")
    if len(co) > 35:
        co = co[:34] + "…"
    return f"• `[{score}]` {_escape_md(title)} @ {_escape_md(co)}"


def _truncate_desc(desc: str, max_chars: int = 4000) -> str:
    if len(desc) <= max_chars:
        return desc
    return desc[:max_chars - 50] + "\n\n_…truncated._"


def _humanize_age_ts(ts):
    if not ts:
        return ""
    return cmd_mod._humanize_age(ts)


# ---------- View builders ----------

def build_menu(conn) -> dict:
    """Main dashboard: top-5 saved (by score), recent applied, counts."""
    saved_rows = sorted(
        db_mod.list_by_status(conn, "saved"),
        key=lambda r: -(r.get("score") or 0),
    )
    top_saved = saved_rows[:5]
    recent_applied = db_mod.list_by_status(conn, "applied", limit=5)
    counts = db_mod.count_by_status(conn)

    lines = []

    lines.append("**Saved** (top 5 by score)")
    if not top_saved:
        lines.append("_None yet._")
    else:
        for t in top_saved:
            lines.append(_short_entry(t))

    lines.append("")
    lines.append("**Applied** (recent 5)")
    if not recent_applied:
        lines.append("_None yet._")
    else:
        for t in recent_applied[:5]:
            age = _humanize_age_ts(t.get("applied_at"))
            entry = _short_entry(t)
            lines.append(f"{entry}{' · ' + age if age else ''}")

    lines.append("")
    lines.append(
        f"Counts: {counts['saved']} saved · {counts['applied']} applied · "
        f"{counts['dismissed']} dismissed · {counts['seen']} in history"
    )

    embed = {
        "title": "Job Manager",
        "description": _truncate_desc("\n".join(lines)),
        "color": COLOR_MENU,
    }
    return {"embeds": [embed], "components": _menu_buttons()}


# ---- List views (paginated, sorted by score DESC) ----

def _build_list_view_generic(
    rows: list,
    page: int,
    view_id: str,
    title_emoji: str,
    title_word: str,
    empty_msg: str,
    ts_field: str | None,
) -> dict:
    # Sort by stored score DESC, then by ts_field DESC (recent first as tie-breaker)
    if ts_field:
        rows.sort(key=lambda r: (-(r.get("score") or 0), -(r.get(ts_field) or 0)))
    else:
        rows.sort(key=lambda r: -(r.get("score") or 0))

    total = len(rows)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    slice_rows = rows[start:end]

    lines = []
    if total == 0:
        lines.append(empty_msg)
    else:
        lines.append(f"**{total} {title_word.lower()}** · page {page + 1}/{total_pages}")
        lines.append("")
        for r in slice_rows:
            entry = _short_entry(r)
            if ts_field:
                age = _humanize_age_ts(r.get(ts_field))
                if age:
                    entry += f" · {age}"
            url = r.get("job_url")
            lines.append(entry)
            if url:
                lines.append(f"  <{url}>")

    embed = {
        "title": f"{title_emoji} {title_word}",
        "description": _truncate_desc("\n".join(lines)),
        "color": COLOR_SUBVIEW,
    }
    return {
        "embeds": [embed],
        "components": _pagination_buttons(view_id, page, total_pages),
    }


def build_saved_view(conn, page: int = 0) -> dict:
    rows = db_mod.list_by_status(conn, "saved")
    return _build_list_view_generic(
        rows, page, "saved", "🔖", "Saved",
        "_No saved jobs yet._",
        "saved_at",
    )


def build_applied_view(conn, page: int = 0) -> dict:
    rows = db_mod.list_by_status(conn, "applied")
    payload = _build_list_view_generic(
        rows, page, "applied", "✅", "Applied",
        "_No applied jobs yet._",
        "applied_at",
    )
    # Override the default pagination row with our Applied-specific row
    # (pagination + Add Manual button).
    total = len(rows)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page_clamped = max(0, min(page, total_pages - 1))
    payload["components"] = _applied_buttons(page_clamped, total_pages)
    return payload


def build_dismissed_view(conn, page: int = 0) -> dict:
    rows = db_mod.list_by_status(conn, "dismissed")
    return _build_list_view_generic(
        rows, page, "dismissed", "❌", "Dismissed",
        "_No dismissed jobs yet._",
        "dismissed_at",
    )


# ---- History view (grouped by day) ----

def _list_dates(conn) -> list[str]:
    """Return all dates (yyyy-mm-dd) that have at least 1 delivered (non-filtered)
    row in seen_v2, newest first."""
    rows = conn.execute(
        "SELECT DISTINCT date(first_seen, 'unixepoch', 'localtime') AS d "
        "FROM seen_v2 WHERE COALESCE(filtered, 0) = 0 ORDER BY d DESC"
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def _date_yyyymmdd(d: str) -> str:
    return d.replace("-", "")


def _yyyymmdd_to_date(s: str) -> str:
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


HISTORY_PAGE_SIZE = 15


def build_history_view(conn, date_arg: str = "", page_arg: str = "") -> dict:
    """Postings scraped on a given day, grouped by Local/Oversea + role,
    sorted by fit_score within each group. Paginated 15 per page within the day.
    """
    dates = _list_dates(conn)
    if not dates:
        embed = {
            "title": "Scrape History",
            "description": "_No scrape history yet._",
            "color": COLOR_SUBVIEW,
        }
        return {"embeds": [embed], "components": _menu_only_buttons()}

    if not date_arg:
        current = dates[0]
    else:
        current = _yyyymmdd_to_date(date_arg)
        if current not in dates:
            current = dates[0]

    idx = dates.index(current)
    newer = dates[idx - 1] if idx > 0 else None
    older = dates[idx + 1] if idx + 1 < len(dates) else None

    raw_rows = conn.execute(
        "SELECT title, company, job_url, location, is_local, query_term, fit_score, first_seen "
        "FROM seen_v2 "
        "WHERE date(first_seen, 'unixepoch', 'localtime') = ? "
        "AND COALESCE(filtered, 0) = 0",
        (current,)
    ).fetchall()
    # Recompute score with live freshness so old entries decay correctly.
    rows = [
        (r[0], r[1], r[2], r[3], r[4], r[5], _live_score(r[6], r[7]))
        for r in raw_rows
    ]

    try:
        d_obj = datetime.strptime(current, "%Y-%m-%d")
        date_heading = d_obj.strftime("%d %B %Y")
    except Exception:
        date_heading = current

    # Build the FULL grouped/sorted list, then paginate at the entry level
    role_order = ["software engineer intern", "ai intern", "backend intern",
                  "it intern", "machine learning intern", "software engineering intern"]

    def ordered_buckets(rows):
        buckets = {}
        for r in rows:
            q = r[5] or "_unsorted"
            buckets.setdefault(q, []).append(r)
        for q in buckets:
            buckets[q].sort(key=lambda x: -(x[6] or 0))
        ordered = [(k, buckets[k]) for k in role_order if k in buckets]
        ordered += [(k, buckets[k]) for k in buckets if k not in role_order]
        return ordered

    local_rows = [r for r in rows if r[4] == 1]
    oversea_rows = [r for r in rows if r[4] != 1]
    local_groups = ordered_buckets(local_rows)
    oversea_groups = ordered_buckets(oversea_rows)

    # Flatten into ordered (section_label, role_label, entry) tuples
    flat = []
    for q, items in local_groups:
        for r in items:
            flat.append(("🇮🇩 Local", q, r))
    for q, items in oversea_groups:
        for r in items:
            flat.append(("🌍 Oversea", q, r))

    total = len(flat)
    total_pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    page = int(page_arg) if page_arg.isdigit() else 0
    page = max(0, min(page, total_pages - 1))
    start = page * HISTORY_PAGE_SIZE
    end = start + HISTORY_PAGE_SIZE
    slice_flat = flat[start:end]

    body = [f"**{total} postings on {date_heading}** · page {page + 1}/{total_pages}"]
    if total == 0:
        body.append("\n_No postings this day._")
    else:
        last_section = None
        last_role = None
        for section, role, r in slice_flat:
            if section != last_section:
                body.append(f"\n## {section}")
                last_section = section
                last_role = None
            if role != last_role:
                role_lbl = "UNCATEGORIZED" if role == "_unsorted" else role.upper()
                count = sum(1 for s, r, _ in flat if s == section and r == role)
                body.append(f"\n`{role_lbl}` ({count})")
                last_role = role
            title = (r[0] or "?")[:70]
            co = (r[1] or "?")[:35]
            url = r[2] or ""
            location = r[3] or ""
            score = r[6]
            flag = _get_flag(location, is_remote="remote" in location.lower() if location else False)
            prefix = f"`[{score}]`" if score is not None else "`[?]`"
            line = f"• {prefix} {flag} {_escape_md(title)} @ {_escape_md(co)}"
            if url:
                line += f"\n  <{url}>"
            body.append(line)

    embed = {
        "title": "Scrape History",
        "description": _truncate_desc("\n".join(body)),
        "color": COLOR_SUBVIEW,
    }
    older_arg = _date_yyyymmdd(older) if older else None
    newer_arg = _date_yyyymmdd(newer) if newer else None
    current_arg = _date_yyyymmdd(current)
    return {
        "embeds": [embed],
        "components": _history_buttons(current_arg, older_arg, newer_arg, page, total_pages),
    }


def build_history_all_view(conn, page_arg: str = "") -> dict:
    """All delivered seen_v2 entries flat, sorted by live score DESC, paginated.
    Sub-threshold (filtered=1) rows are excluded — view them with raw SQL if needed."""
    raw_rows = conn.execute(
        "SELECT title, company, job_url, location, is_local, query_term, fit_score, first_seen "
        "FROM seen_v2 WHERE COALESCE(filtered, 0) = 0"
    ).fetchall()
    # Compute live score (decays freshness from first_seen) then sort DESC by it.
    rows = sorted(
        (
            (r[0], r[1], r[2], r[3], r[4], r[5], _live_score(r[6], r[7]), r[7])
            for r in raw_rows
        ),
        key=lambda x: (-(x[6] or 0), -(x[7] or 0)),
    )

    total = len(rows)
    total_pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    page = int(page_arg) if page_arg.isdigit() else 0
    page = max(0, min(page, total_pages - 1))
    start = page * HISTORY_PAGE_SIZE
    end = start + HISTORY_PAGE_SIZE
    slice_rows = rows[start:end]

    body = [f"**{total} postings · all time** · ranked by score · page {page + 1}/{total_pages}"]
    if total == 0:
        body.append("\n_No scrape history yet._")
    else:
        body.append("")
        for r in slice_rows:
            title = (r[0] or "?")[:70]
            co = (r[1] or "?")[:35]
            url = r[2] or ""
            location = r[3] or ""
            score = r[6]
            is_remote = "remote" in location.lower() if location else False
            flag = _get_flag(location, is_remote=is_remote)
            prefix = f"`[{score}]`" if score is not None else "`[?]`"
            line = f"• {prefix} {flag} {_escape_md(title)} @ {_escape_md(co)}"
            if url:
                line += f"\n  <{url}>"
            body.append(line)

    embed = {
        "title": "All History",
        "description": _truncate_desc("\n".join(body)),
        "color": COLOR_SUBVIEW,
    }
    return {
        "embeds": [embed],
        "components": _history_all_buttons(page, total_pages),
    }


# ---- Stats / Config / Help ----

def build_stats_view(conn) -> dict:
    counts = db_mod.count_by_status(conn)
    total_triaged = counts["saved"] + counts["applied"] + counts["dismissed"]
    untriaged = max(0, counts["seen"] - total_triaged)
    lines = [
        "**Triage breakdown**",
        f"🔖  **{counts['saved']}** saved",
        f"✅  **{counts['applied']}** applied",
        f"❌  **{counts['dismissed']}** dismissed",
        "",
        "**Scrape history**",
        f"📋  **{untriaged}** still untriaged",
        f"📚  **{counts['seen']}** total ever scraped",
    ]
    embed = {
        "title": "Stats",
        "description": "\n".join(lines),
        "color": COLOR_SUBVIEW,
    }
    return {"embeds": [embed], "components": _menu_only_buttons()}


def build_config_view(conn) -> dict:
    lines = ["Current `queries.yaml` settings (read-only here; edit via SSH for now):", ""]
    for key in config_mod.EDITABLE_KEYS:
        v = config_mod.get_value(key)
        if isinstance(v, list):
            if not v:
                lines.append(f"**`{key}`**: _empty_")
            else:
                lines.append(f"**`{key}`** ({len(v)} entries):")
                for item in v[:20]:
                    lines.append(f"  • `{item}`")
                if len(v) > 20:
                    lines.append(f"  _…{len(v) - 20} more._")
        else:
            lines.append(f"**`{key}`**: `{v}`")
        lines.append("")
    embed = {
        "title": "Config (read-only)",
        "description": _truncate_desc("\n".join(lines)),
        "color": COLOR_SUBVIEW,
    }
    return {"embeds": [embed], "components": _menu_only_buttons()}


def build_help_view(conn) -> dict:
    lines = [
        "Click any button to switch views. Sub-views show only context-relevant buttons + back to menu.",
        "",
        "**From the menu:**",
        "🔖  **Saved** — your bookmarks (paginated 15/page, sorted by score)",
        "✅  **Applied** — postings you've applied to",
        "❌  **Dismissed** — postings you marked as not-interested",
        "📚  **History** — scrape history by day, grouped Local/Oversea + role, sorted by score",
        "📊  **Stats** — counts breakdown",
        "⚙️  **Config** — current `queries.yaml` (read-only)",
        "❓  **Help** — this view",
        "🔄  **Refresh** — re-fetch the dashboard",
        "",
        "**Triage** happens by clicking buttons on daily digest cards in #notification:",
        "💾  Save · ❌ Dismiss · ✅ Applied · 🔗 Open",
        "",
        "_Scores are snapshots at save-time._",
    ]
    embed = {
        "title": "Help",
        "description": "\n".join(lines),
        "color": COLOR_SUBVIEW,
    }
    return {"embeds": [embed], "components": _menu_only_buttons()}


# Dispatcher
def build(view: str, conn, arg: str = "") -> dict:
    """Look up + invoke a view builder by name."""
    if view in ("menu", "refresh"):
        return build_menu(conn)
    if view == "saved":
        page = int(arg) if arg.isdigit() else 0
        return build_saved_view(conn, page)
    if view == "applied":
        page = int(arg) if arg.isdigit() else 0
        return build_applied_view(conn, page)
    if view == "dismissed":
        page = int(arg) if arg.isdigit() else 0
        return build_dismissed_view(conn, page)
    if view == "hist":
        # arg may be "yyyymmdd" or "yyyymmdd:N" (page number)
        date_arg = ""
        page_arg = ""
        if arg:
            if ":" in arg:
                date_arg, page_arg = arg.split(":", 1)
            else:
                date_arg = arg
        return build_history_view(conn, date_arg, page_arg)
    if view == "histall":
        return build_history_all_view(conn, arg or "")
    if view == "stats":
        return build_stats_view(conn)
    if view == "config":
        return build_config_view(conn)
    if view == "help":
        return build_help_view(conn)
    return build_menu(conn)
