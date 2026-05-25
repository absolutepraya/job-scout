"""SQLite helpers for triage state."""
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path.home() / ".agents" / "skills" / "job-watcher" / "state.db"

# Add the job-watcher bin to sys.path so we can import its scoring module
_JOB_WATCHER_BIN = Path.home() / ".agents" / "skills" / "job-watcher" / "bin"
if _JOB_WATCHER_BIN.is_dir() and str(_JOB_WATCHER_BIN) not in sys.path:
    sys.path.insert(0, str(_JOB_WATCHER_BIN))

# Query strings used by the cron (matches queries.yaml order)
_QUERY_HEURISTICS = [
    # (canonical name, list of substring matches in title)
    ("ai intern",                 ["ai ", "artificial intelligence", "ml ", "machine learning",
                                    "generative", "gen ai", "llm", "deep learning"]),
    ("machine learning intern",   ["machine learning", "ml engineer"]),
    ("backend intern",            ["backend", "back-end", "back end", "server-side", "api ", "sde "]),
    ("software engineering intern", ["software engineering"]),
    ("software engineer intern",  ["software engineer", "swe", "se intern", "fullstack", "full-stack",
                                    "full stack", "frontend", "front-end", "front end"]),
    ("it intern",                 ["it intern", "information technology", "it support", "helpdesk",
                                    "infrastructure", "devops", "sre", "cybersecurity", "security"]),
]


def _guess_query_term(title: str) -> str:
    if not title:
        return "_unsorted"
    t = title.lower()
    for canon, kws in _QUERY_HEURISTICS:
        for kw in kws:
            if kw in t:
                return canon
    return "_unsorted"


INDO_LOCATION_KEYWORDS = (
    "indonesia", "jakarta", "tangerang", "bandung", "depok",
    "bekasi", "bali", "surabaya", "yogyakarta", "semarang", "medan",
)


def _is_local_str(location: str | None) -> int:
    if not location:
        return 0
    loc = location.lower()
    return 1 if any(kw in loc for kw in INDO_LOCATION_KEYWORDS) else 0


def open_db() -> sqlite3.Connection:
    """Open state.db with WAL mode + 5s busy timeout. Auto-creates triage table.
    Also runs idempotent seen_v2 schema migration (adds query_term, fit_score, is_local).
    """
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _ensure_triage_table(conn)
    _migrate_seen_v2(conn)
    return conn


def _migrate_seen_v2(conn: sqlite3.Connection) -> None:
    """Add query_term, fit_score, is_local columns to seen_v2 if missing.
    Backfill is_local from location for rows where it's still NULL.
    Idempotent: safe to run on every open_db().
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(seen_v2)").fetchall()}
    if "query_term" not in cols:
        conn.execute("ALTER TABLE seen_v2 ADD COLUMN query_term TEXT")
    if "fit_score" not in cols:
        conn.execute("ALTER TABLE seen_v2 ADD COLUMN fit_score INTEGER")
    if "is_local" not in cols:
        conn.execute("ALTER TABLE seen_v2 ADD COLUMN is_local INTEGER")
    # Backfill is_local from location for any rows where it's NULL
    rows = conn.execute("SELECT rowid, location FROM seen_v2 WHERE is_local IS NULL").fetchall()
    for rowid, loc in rows:
        conn.execute(
            "UPDATE seen_v2 SET is_local = ? WHERE rowid = ?",
            (_is_local_str(loc), rowid),
        )

    # Backfill query_term from title heuristics
    rows = conn.execute("SELECT rowid, title FROM seen_v2 WHERE query_term IS NULL").fetchall()
    for rowid, title in rows:
        conn.execute(
            "UPDATE seen_v2 SET query_term = ? WHERE rowid = ?",
            (_guess_query_term(title or ""), rowid),
        )

    # Backfill fit_score using scoring.compute_fit_score (no description = stack_match=0)
    try:
        import scoring as _scoring
        rows = conn.execute(
            "SELECT rowid, location, title, company, first_seen FROM seen_v2 WHERE fit_score IS NULL"
        ).fetchall()
        for rowid, loc, title, co, fs in rows:
            date_str = None
            if fs:
                try:
                    date_str = datetime.fromtimestamp(int(fs), tz=timezone.utc).strftime("%Y-%m-%d")
                except Exception:
                    date_str = None
            posting = {
                "title": title or "",
                "company": co or "",
                "location": loc or "",
                "description": "",
                "date_posted": date_str,
                "is_remote": False,
            }
            try:
                score, _, _ = _scoring.compute_fit_score(posting)
            except Exception:
                score = 0
            conn.execute(
                "UPDATE seen_v2 SET fit_score = ? WHERE rowid = ?",
                (int(score), rowid),
            )
    except ImportError:
        pass  # scoring module unavailable; leave NULL

    conn.commit()


def _ensure_triage_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS triage (
            dedup_key      TEXT PRIMARY KEY,
            job_url        TEXT NOT NULL,
            title          TEXT,
            company        TEXT,
            location       TEXT,
            score          INTEGER,
            workmode       TEXT,
            status         TEXT NOT NULL CHECK(status IN ('saved', 'applied', 'dismissed')),
            saved_at       INTEGER,
            applied_at     INTEGER,
            dismissed_at   INTEGER,
            notes          TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_triage_status ON triage(status)")
    conn.commit()


def lookup_seen_v2(conn: sqlite3.Connection, url: str) -> dict | None:
    """Look up a posting by URL in seen_v2. Returns dict or None."""
    row = conn.execute(
        "SELECT dedup_key, job_url, title, company, location, site FROM seen_v2 WHERE job_url = ?",
        (url,)
    ).fetchone()
    if not row:
        return None
    return {
        "dedup_key": row[0],
        "job_url": row[1],
        "title": row[2],
        "company": row[3],
        "location": row[4],
        "site": row[5],
    }


def get_triage(conn: sqlite3.Connection, dedup_key: str) -> dict | None:
    row = conn.execute(
        "SELECT dedup_key, job_url, title, company, location, score, workmode, status, "
        "saved_at, applied_at, dismissed_at, notes FROM triage WHERE dedup_key = ?",
        (dedup_key,)
    ).fetchone()
    if not row:
        return None
    return {
        "dedup_key": row[0], "job_url": row[1], "title": row[2], "company": row[3],
        "location": row[4], "score": row[5], "workmode": row[6], "status": row[7],
        "saved_at": row[8], "applied_at": row[9], "dismissed_at": row[10], "notes": row[11],
    }


def upsert_triage(conn: sqlite3.Connection, snapshot: dict, status: str, notes: str | None = None) -> None:
    """Insert or update triage row. Sets appropriate timestamp for the new status."""
    now = int(time.time())
    existing = get_triage(conn, snapshot["dedup_key"])
    saved_at = existing["saved_at"] if existing else None
    applied_at = existing["applied_at"] if existing else None
    dismissed_at = existing["dismissed_at"] if existing else None
    if status == "saved":
        saved_at = now
        dismissed_at = None
    elif status == "applied":
        applied_at = now
    elif status == "dismissed":
        dismissed_at = now
    conn.execute("""
        INSERT INTO triage (dedup_key, job_url, title, company, location, score, workmode,
                            status, saved_at, applied_at, dismissed_at, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(dedup_key) DO UPDATE SET
            status = excluded.status,
            saved_at = excluded.saved_at,
            applied_at = excluded.applied_at,
            dismissed_at = excluded.dismissed_at,
            notes = COALESCE(excluded.notes, notes)
    """, (
        snapshot["dedup_key"], snapshot["job_url"], snapshot["title"], snapshot["company"],
        snapshot["location"], snapshot.get("score"), snapshot.get("workmode"),
        status, saved_at, applied_at, dismissed_at, notes,
    ))
    conn.commit()


def delete_triage(conn: sqlite3.Connection, dedup_key: str) -> int:
    """Delete a triage row. Returns affected row count."""
    cur = conn.execute("DELETE FROM triage WHERE dedup_key = ?", (dedup_key,))
    conn.commit()
    return cur.rowcount


def list_by_status(conn: sqlite3.Connection, status: str, limit: int | None = None) -> list[dict]:
    """List triage rows filtered by status, sorted by relevant timestamp DESC."""
    sort_col = {"saved": "saved_at", "applied": "applied_at", "dismissed": "dismissed_at"}[status]
    sql = (
        f"SELECT dedup_key, job_url, title, company, location, score, workmode, "
        f"status, saved_at, applied_at, dismissed_at, notes FROM triage "
        f"WHERE status = ? ORDER BY {sort_col} DESC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, (status,)).fetchall()
    return [
        {
            "dedup_key": r[0], "job_url": r[1], "title": r[2], "company": r[3],
            "location": r[4], "score": r[5], "workmode": r[6], "status": r[7],
            "saved_at": r[8], "applied_at": r[9], "dismissed_at": r[10], "notes": r[11],
        }
        for r in rows
    ]


def count_by_status(conn: sqlite3.Connection) -> dict:
    """Returns {'saved': N, 'applied': M, 'dismissed': K, 'seen': T}."""
    out = {"saved": 0, "applied": 0, "dismissed": 0, "seen": 0}
    for row in conn.execute("SELECT status, COUNT(*) FROM triage GROUP BY status"):
        out[row[0]] = row[1]
    out["seen"] = conn.execute("SELECT COUNT(*) FROM seen_v2").fetchone()[0]
    return out
