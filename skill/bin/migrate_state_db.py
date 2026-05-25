"""One-shot: migrate state.db from URL-based dedup to (title,company)-based."""
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "state.db"


def migrate():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='seen_v2'")
    if cur.fetchone():
        print("seen_v2 already exists, migration already applied")
        conn.close()
        return

    cur.execute("""
        CREATE TABLE seen_v2 (
            dedup_key TEXT PRIMARY KEY,
            job_url TEXT,
            title TEXT,
            company TEXT,
            location TEXT,
            site TEXT,
            first_seen INTEGER
        )
    """)

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='seen'")
    if cur.fetchone():
        rows = cur.execute("SELECT job_url, title, company, location, site, first_seen FROM seen").fetchall()
        migrated = 0
        skipped = 0
        for url, title, company, location, site, first_seen in rows:
            t = (title or "").lower().strip()
            c = (company or "").lower().strip()
            dedup_key = f"{t}|{c}"
            try:
                cur.execute(
                    "INSERT INTO seen_v2 VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (dedup_key, url, title, company, location, site, first_seen),
                )
                migrated += 1
            except sqlite3.IntegrityError:
                skipped += 1
        print(f"migrated: {migrated}, deduplicated: {skipped}")
        cur.execute("DROP TABLE seen")
    else:
        print("no old seen table, fresh state")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    migrate()
