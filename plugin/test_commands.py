"""Tests for command handlers. Uses in-memory state.db override."""
import pytest
import sqlite3
from pathlib import Path

import db as db_mod
import commands as cmd_mod


@pytest.fixture
def conn(tmp_path):
    """Spin up an isolated state.db with seen_v2 + triage tables + sample data."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE seen_v2 (
            dedup_key TEXT PRIMARY KEY,
            job_url TEXT, title TEXT, company TEXT, location TEXT,
            site TEXT, first_seen INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE triage (
            dedup_key TEXT PRIMARY KEY,
            job_url TEXT NOT NULL,
            title TEXT, company TEXT, location TEXT,
            score INTEGER, workmode TEXT,
            status TEXT NOT NULL CHECK(status IN ('saved', 'applied', 'dismissed')),
            saved_at INTEGER, applied_at INTEGER, dismissed_at INTEGER,
            notes TEXT
        )
    """)
    conn.execute(
        "INSERT INTO seen_v2 VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("ai engineer intern|cohere", "https://linkedin.com/jobs/view/123",
         "AI Engineer Intern", "Cohere", "Toronto, Ontario, Canada",
         "linkedin", 1000000),
    )
    conn.commit()
    yield conn
    conn.close()


def test_parse_save_single_url():
    result = cmd_mod.parse_command("!save https://linkedin.com/jobs/view/123")
    assert result["cmd"] == "save"
    assert result["urls"] == ["https://linkedin.com/jobs/view/123"]
    assert result["note"] is None


def test_parse_save_with_note():
    result = cmd_mod.parse_command("!save https://x.com/1 -- looks great")
    assert result["cmd"] == "save"
    assert result["urls"] == ["https://x.com/1"]
    assert result["note"] == "looks great"


def test_parse_save_multi_url():
    result = cmd_mod.parse_command("!save <https://x.com/1> https://x.com/2")
    assert result["urls"] == ["https://x.com/1", "https://x.com/2"]


def test_parse_case_insensitive():
    assert cmd_mod.parse_command("!SAVE foo")["cmd"] == "save"
    assert cmd_mod.parse_command("!Applied foo")["cmd"] == "applied"


def test_parse_unknown():
    result = cmd_mod.parse_command("!hovercraft")
    assert result["cmd"] == "_unknown"


def test_cmd_save_new(conn):
    msg = cmd_mod.cmd_save(conn, ["https://linkedin.com/jobs/view/123"], note=None)
    assert "Saved" in msg
    assert "AI Engineer Intern" in msg


def test_cmd_save_url_not_found(conn):
    msg = cmd_mod.cmd_save(conn, ["https://x.com/notfound"], note=None)
    assert "not found" in msg.lower()


def test_cmd_save_already_saved(conn):
    cmd_mod.cmd_save(conn, ["https://linkedin.com/jobs/view/123"], note=None)
    msg = cmd_mod.cmd_save(conn, ["https://linkedin.com/jobs/view/123"], note=None)
    assert "already saved" in msg.lower()


def test_cmd_applied_then_unapply(conn):
    cmd_mod.cmd_applied(conn, ["https://linkedin.com/jobs/view/123"])
    triage = db_mod.get_triage(conn, "ai engineer intern|cohere")
    assert triage["status"] == "applied"
    cmd_mod.cmd_unapply(conn, ["https://linkedin.com/jobs/view/123"])
    triage = db_mod.get_triage(conn, "ai engineer intern|cohere")
    assert triage["status"] == "saved"


def test_cmd_dismiss(conn):
    msg = cmd_mod.cmd_dismiss(conn, ["https://linkedin.com/jobs/view/123"])
    assert "Dismissed" in msg
    triage = db_mod.get_triage(conn, "ai engineer intern|cohere")
    assert triage["status"] == "dismissed"


def test_cmd_save_after_dismiss(conn):
    cmd_mod.cmd_dismiss(conn, ["https://linkedin.com/jobs/view/123"])
    msg = cmd_mod.cmd_save(conn, ["https://linkedin.com/jobs/view/123"], note=None)
    assert "dismissed" in msg.lower()
    triage = db_mod.get_triage(conn, "ai engineer intern|cohere")
    assert triage["status"] == "saved"


def test_cmd_unsave(conn):
    cmd_mod.cmd_save(conn, ["https://linkedin.com/jobs/view/123"], note=None)
    msg = cmd_mod.cmd_unsave(conn, ["https://linkedin.com/jobs/view/123"])
    assert "Removed" in msg
    assert db_mod.get_triage(conn, "ai engineer intern|cohere") is None


def test_cmd_stats(conn):
    cmd_mod.cmd_save(conn, ["https://linkedin.com/jobs/view/123"], note=None)
    msg = cmd_mod.cmd_stats(conn)
    assert "1 saved" in msg


def test_cmd_help_general():
    msg = cmd_mod.cmd_help(args=[])
    assert "!save" in msg
    assert "!applied" in msg


def test_cmd_help_specific():
    msg = cmd_mod.cmd_help(args=["save"])
    assert "!save" in msg
    assert "Usage" in msg


def test_url_canonicalization_in_save(conn):
    msg = cmd_mod.cmd_save(conn, ["https://linkedin.com/jobs/view/123?trk=abc"], note=None)
    assert "Saved" in msg
