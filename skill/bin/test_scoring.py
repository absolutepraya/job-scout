"""Unit tests for scoring.py"""
from datetime import datetime, timezone
from scoring import (
    compute_fit_score, score_geo, score_freshness, score_stack,
    score_niche, score_company, classify_workmode, get_flag,
)


NOW = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)


def test_geo_indonesia():
    assert score_geo("Jakarta, Jakarta, Indonesia") == 35
    assert score_geo("Tangerang, Banten, Indonesia") == 35


def test_geo_singapore():
    assert score_geo("Singapore, Singapore") == 30


def test_geo_japan():
    assert score_geo("Tokyo, Japan") == 25


def test_geo_uk():
    assert score_geo("London, England, UK") == 20


def test_geo_canada():
    assert score_geo("Toronto, Ontario, Canada") == 15


def test_geo_us_low():
    assert score_geo("Remote, US") == 8
    assert score_geo("San Francisco, CA") == 8


def test_geo_unknown_zero():
    assert score_geo("Lagos, Nigeria") == 0
    assert score_geo("") == 0


def test_freshness_buckets():
    # NOW = 2026-05-17 12:00 UTC. Days-based tiers (2026-05-26 rebalance).
    assert score_freshness("2026-05-17", NOW) == 25  # 0.5d ago, <5d
    assert score_freshness("2026-05-13", NOW) == 25  # 4.5d ago, <5d
    assert score_freshness("2026-05-12", NOW) == 18  # 5.5d ago, <10d
    assert score_freshness("2026-05-05", NOW) == 12  # 12.5d ago, <20d
    assert score_freshness("2026-04-25", NOW) == 6   # 22.5d ago, <30d
    assert score_freshness("2026-03-01", NOW) == 0   # ~77d ago, >=30d
    # Missing/unparseable date -> 12 (middle tier default; JobSpy already filters
    # to hours_old=168 so the row is recent enough — see Astra Otopprentice case)
    assert score_freshness(None) == 12
    assert score_freshness("nan") == 12


def test_stack_match_basic():
    score, matches = score_stack("AI Engineer Intern", "Python, FastAPI, Postgres, AWS")
    assert "py" in matches
    assert "fastapi" in matches
    assert "sql" in matches
    assert "aws" in matches
    assert score >= 12


def test_stack_match_cap():
    title = "Fullstack"
    desc = "TypeScript JavaScript Python Java SQL React Next.js Remix Tailwind Express FastAPI PostgreSQL Redis Docker Cloudflare"
    score, _ = score_stack(title, desc)
    assert score == 30


def test_stack_react_native_distinct():
    _, m1 = score_stack("React Engineer", "")
    _, m2 = score_stack("React Native Engineer", "")
    assert "react" in m1 and "rn" not in m1
    assert "rn" in m2 and "react" not in m2


def test_niche_bonus():
    # Title hit -> full 15
    assert score_niche("AI Engineer Intern") == 15
    assert score_niche("AI Agent Intern") == 15
    assert score_niche("Backend Intern") == 0
    # Description-only hit -> half (8). Catches multi-track postings like Astra's
    # Otopprentice where the body lists 'AI Engineer Intern' as a sub-role.
    assert score_niche("Otopprentice IT / Intern", "Workshop AI Engineer Intern (Karawang)") == 8
    assert score_niche("Backend Intern", "looking for a generative ai intern") == 8
    # Title hit wins over description hit (no double-count)
    assert score_niche("AI Engineer Intern", "More AI agent stuff") == 15


def test_company_tier_s():
    assert score_company("Anthropic") == 10
    assert score_company("OpenAI Inc") == 10


def test_company_tier_a():
    assert score_company("Cloudflare") == 7
    assert score_company("Google LLC") == 7


def test_company_tier_b():
    assert score_company("ByteDance") == 4
    assert score_company("tiket.com") == 4


def test_company_unknown():
    assert score_company("Random Co") == 0


def test_compute_fit_score_full():
    posting = {
        "title": "AI Agent Engineer Intern",
        "company": "Cloudflare",
        "location": "Singapore, Singapore",
        "description": "Build AI agents using Cloudflare Workers, Python, TypeScript, RAG",
        "is_remote": True,
        "date_posted": "2026-05-17",
    }
    score, matches, local = compute_fit_score(posting, NOW)
    # 30 geo (SG) + 25 fresh + stack (py, ts, cf, agent, rag = 15) + 15 niche
    # = 85; company tier dropped in 2026-05-26 rebalance.
    assert score >= 80
    assert local is False


def test_compute_fit_score_local():
    posting = {
        "title": "AI Engineer Intern",
        "company": "tiket.com",
        "location": "Jakarta, Jakarta, Indonesia",
        "description": "Build AI agents using Python, TypeScript",
        "is_remote": False,
        "date_posted": "2026-05-17",
    }
    score, _, local = compute_fit_score(posting, NOW)
    assert local is True
    # 35 local + 25 fresh + ~9 stack (py, ts, agent) + 15 niche = ~84
    # (company tier dropped 2026-05-26)
    assert score >= 75


def test_compute_fit_score_missing_date_with_desc_niche():
    """Astra Otopprentice case: local, no date, generic title, niche in description."""
    posting = {
        "title": "Otopprentice IT / Intern - AOP",
        "company": "PT Astra International Tbk",
        "location": "Jakarta, Jakarta, Indonesia",
        "description": "Workshop AI Engineer Intern (Karawang) — implementasi python",
        "is_remote": False,
        "date_posted": None,
    }
    score, _, local = compute_fit_score(posting, NOW)
    # 35 local + 12 fresh default + 3 stack (py) + 8 niche-in-desc + 0 intern = 58
    assert local is True
    assert score >= 50


def test_workmode_remote():
    assert classify_workmode({"is_remote": True, "location": "Remote, US"}) == "remote"
    assert classify_workmode({"location": "Remote"}) == "remote"


def test_workmode_onsite_indo():
    assert classify_workmode({"location": "Jakarta, Indonesia"}) == "onsite"


def test_workmode_relocate():
    assert classify_workmode({"location": "Singapore, Singapore"}) == "relocate"


def test_flag_indo():
    assert get_flag("Jakarta, Indonesia") == "🇮🇩"


def test_flag_remote():
    assert get_flag("Remote", is_remote=True) == "🌍"
    assert get_flag("Remote, US") == "🌍"


def test_flag_unknown():
    assert get_flag("Lagos, Nigeria") == "🏳"
    assert get_flag("") == "🏳"
