"""Fit-score computation for job-watcher postings."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

# ---- Geo weights (substring-matched against location, case-insensitive) ----
GEO_WEIGHTS: list[tuple[int, list[str]]] = [
    (35, ["indonesia", "jakarta", "tangerang", "bandung", "depok", "bekasi", "bali", "surabaya"]),
    (30, ["singapore", "malaysia", "kuala lumpur", "penang"]),
    (25, ["japan", "tokyo", "osaka", "taiwan", "taipei", "hsinchu",
          "china", "shanghai", "beijing", "shenzhen",
          "hong kong", "south korea", "korea", "seoul"]),
    (20, ["united kingdom", "uk,", " uk", "england", "london", "scotland",
          "germany", "berlin", "munich", "hamburg",
          "netherlands", "amsterdam",
          "france", "paris", "lyon",
          "sweden", "stockholm",
          "norway", "oslo",
          "denmark", "copenhagen",
          "finland", "helsinki",
          "italy", "milan", "rome",
          "spain", "madrid", "barcelona",
          "belgium", "brussels",
          "switzerland", "zurich",
          "ireland", "dublin"]),
    (15, ["canada", "toronto", "vancouver", "montreal",
          "australia", "sydney", "melbourne",
          "new zealand", "auckland"]),
    (8, ["united states", "usa", "us,", " us", "remote, us", "new york", "san francisco", "seattle",
         "los angeles", "boston", "chicago", "austin", "atlanta"]),
]

# ---- Stack keywords (regex word boundaries; matched on title + description) ----
STACK_KEYWORDS: list[tuple[str, str]] = [
    ("ts",        r"\btypescript\b"),
    ("js",        r"\bjavascript\b"),
    ("py",        r"\bpython\b"),
    ("java",      r"\bjava\b"),
    ("sql",       r"\bsql\b|\bpostgres\b|\bpostgresql\b"),
    ("react",     r"\breact(?!\s*native)\b"),
    ("rn",        r"\breact\s*native\b"),
    ("next",      r"\bnext\.?js\b"),
    ("remix",     r"\bremix\b"),
    ("tanstack",  r"\btanstack\b"),
    ("tailwind",  r"\btailwind\b"),
    ("framer",    r"\bframer\b"),
    ("express",   r"\bexpress(?:\.?js)?\b"),
    ("elysia",    r"\belysia(?:\.?js)?\b"),
    ("fastapi",   r"\bfastapi\b"),
    ("django",    r"\bdjango\b"),
    ("supabase",  r"\bsupabase\b"),
    ("redis",     r"\bredis\b"),
    ("docker",    r"\bdocker\b"),
    ("cf",        r"\bcloudflare\b|\bworkers\b|\bdurable objects\b|\bai gateway\b"),
    ("vercel",    r"\bvercel\b"),
    ("agent",     r"\bagent\b|\bagentic\b|\bagents sdk\b"),
    ("rag",       r"\brag\b|\bretrieval[- ]augmented\b"),
    ("mcp",       r"\bmcp\b|\bmodel context protocol\b"),
    ("llm",       r"\bllm\b|\blarge language model\b"),
    ("genai",     r"\bgen[- ]?ai\b|\bgenerative ai\b"),
    ("azure",     r"\bazure\b|\bai foundry\b|\bmicrosoft fabric\b|\bcopilot\b"),
    ("gcp",       r"\bgcp\b|\bgoogle cloud\b"),
    ("aws",       r"\baws\b|\bamazon web services\b"),
    ("gemini",    r"\bgemini\b"),
    ("openai",    r"\bopenai\b|\bgpt-?\d\b"),
    ("bun",       r"\bbun\b"),
    ("gh",        r"\bgithub actions\b|\bgha\b"),
    ("trigger",   r"\btrigger\.dev\b"),
    ("sentry",    r"\bsentry\b"),
    ("posthog",   r"\bposthog\b"),
    ("playwright", r"\bplaywright\b"),
]

# ---- Niche bonus keywords (title only, case-insensitive) ----
NICHE_TITLE_KEYWORDS = [
    "ai engineer", "ml engineer", "applied ai", "ai agent",
    "agentic", "llm engineer", "genai engineer", "gen ai",
    "generative ai", "forward deployed", "applied scientist",
]

# ---- Company tiers (substring on company, case-insensitive; first match wins) ----
COMPANY_TIERS: list[tuple[int, list[str]]] = [
    (10, ["anthropic", "openai", "cohere", "mistral", "hugging face", "deepmind",
          "xai", "inflection", "modal", "together ai", "replicate",
          "langchain", "llamaindex", "pinecone", "weaviate",
          "perplexity", "cursor", "sourcegraph"]),
    (7,  ["cloudflare", "vercel", "supabase", "stripe", "linear", "notion",
          "figma", "microsoft", "google", "meta", "apple", "amazon",
          "netflix", "nvidia", "tesla", "snowflake", "databricks",
          "mongodb", "datadog", "confluent", "coinbase", "brex", "ramp"]),
    (4,  ["bytedance", "tiktok", "tencent", "alibaba", "sea limited",
          "shopee", "grab", "gojek", "tokopedia", "bukalapak", "traveloka",
          "lazada", "razer", "synopsys", "cadence", "intel", "amd",
          "logitech", "marvell", "boeing", "airbus", "blackrock",
          "jpmorgan", "goldman", "sap", "oracle", "adobe", "salesforce",
          "mercari", "tiket.com",
          # Energy/industrial enterprises (added 2026-05-23 — Halliburton-class catches)
          "halliburton", "schlumberger", "slb ", "baker hughes",
          "exxonmobil", "chevron", "totalenergies", "petronas",
          "pertamina", "equinor", "conocophillips",
          "ge aerospace", "ge vernova", "siemens", "abb", "honeywell",
          "caterpillar"]),
]


def score_geo(location: str) -> int:
    if not location:
        return 0
    loc = location.lower()
    for score, keywords in GEO_WEIGHTS:
        if any(kw in loc for kw in keywords):
            return score
    return 0


def score_freshness(date_posted: Optional[str], now: Optional[datetime] = None) -> int:
    """Looser windows (2026-05-26 rebalance): max 25 within 5 days, decays over a month.

    When date_posted is missing/nan/unparseable, returns 12 (middle tier). JobSpy already
    filters to hours_old=168 (1 week), so the row is recent enough — we just don't know
    the exact day. Treating it as middle-aged avoids dropping legit postings like
    Astra's Otopprentice IT/Intern - AOP where LinkedIn omits the date field.
    """
    if not date_posted or date_posted == "nan" or str(date_posted).lower() == "none":
        return 12
    try:
        posted = datetime.fromisoformat(str(date_posted)[:10]).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 12
    now = now or datetime.now(timezone.utc)
    days = (now - posted).total_seconds() / 86400
    if days < 5:
        return 25
    if days < 10:
        return 18
    if days < 20:
        return 12
    if days < 30:
        return 6
    return 0


def score_stack(title: str, description: str) -> tuple[int, list[str]]:
    text = f"{title or ''} {description or ''}".lower()
    matches: list[str] = []
    for short, pattern in STACK_KEYWORDS:
        if re.search(pattern, text, re.IGNORECASE):
            matches.append(short)
    return min(len(matches) * 3, 30), matches


def score_niche(title: str, description: str = "") -> int:
    """Niche-role bonus. Title hit = 15 (strong signal). Description-only hit = 8
    (half weight) — catches multi-track postings like Astra's Otopprentice where
    the title is generic but the body lists sub-roles like "AI Engineer Intern".
    """
    if title:
        t = title.lower()
        for kw in NICHE_TITLE_KEYWORDS:
            if kw in t:
                return 15
    if description:
        d = description.lower()
        for kw in NICHE_TITLE_KEYWORDS:
            if kw in d:
                return 8
    return 0


def score_company(company: str) -> int:
    if not company:
        return 0
    c = company.lower()
    for score, keywords in COMPANY_TIERS:
        if any(kw in c for kw in keywords):
            return score
    return 0


# ---- Description intern-program signal (added 2026-05-23) ----
# Awards +5 when description suggests this is a structured intern/early-career
# program rather than just a one-off internship listing. Catches cases like
# Halliburton ESG-Intern where the title/stack signals are weak but the
# posting is clearly a legitimate intern program in Indonesia.
INTERN_DESC_SIGNALS = [
    "entry level",
    "early career",
    "intern program",
    "internship program",
    "graduate program",
]


def score_intern_desc(description: str) -> int:
    if not description:
        return 0
    d = description.lower()
    return 5 if any(kw in d for kw in INTERN_DESC_SIGNALS) else 0


INDO_LOCATION_KEYWORDS = [
    "indonesia", "jakarta", "tangerang", "bandung", "depok",
    "bekasi", "bali", "surabaya", "yogyakarta", "semarang", "medan",
]


def is_local(posting: dict) -> bool:
    """Returns True if posting is Indonesia-based (regardless of remote/onsite)."""
    loc = (posting.get("location") or "").lower()
    if not loc:
        return False
    return any(kw in loc for kw in INDO_LOCATION_KEYWORDS)


def compute_fit_score(posting: dict, now: Optional[datetime] = None) -> tuple[int, list[str], bool]:
    """Returns (fit_score, top3_stack_matches, is_local_posting). Both buckets cap at 100."""
    title = str(posting.get("title") or "")
    company = str(posting.get("company") or "")
    location = str(posting.get("location") or "")
    description = str(posting.get("description") or "")
    date_posted = posting.get("date_posted")

    stack_score, stack_matches = score_stack(title, description)
    local = is_local(posting)

    if local:
        # Local: baseline 35 (2026-05-26 rebalance — bumped from 25).
        geo_component = 35
    else:
        geo_component = score_geo(location)

    # Company tier dropped 2026-05-26 — geo + freshness carry more weight now.
    # score_niche now also scans description with half-weight (+8).
    total = (
        geo_component
        + score_freshness(date_posted, now)
        + stack_score
        + score_niche(title, description)
        + score_intern_desc(description)
    )
    return total, stack_matches[:3], local


def classify_workmode(posting: dict) -> str:
    if posting.get("is_remote") is True:
        return "remote"
    loc = (posting.get("location") or "").lower()
    if "remote" in loc:
        return "remote"
    indo_keywords = ["jakarta", "tangerang", "bandung", "depok", "bekasi", "bali", "surabaya", "indonesia"]
    if any(kw in loc for kw in indo_keywords):
        return "onsite"
    return "relocate"


FLAG_MAP: list[tuple[str, list[str]]] = [
    ("🇮🇩", ["indonesia", "jakarta", "tangerang", "bandung", "depok", "bekasi", "bali", "surabaya"]),
    ("🇸🇬", ["singapore"]),
    ("🇲🇾", ["malaysia", "kuala lumpur", "penang"]),
    ("🇯🇵", ["japan", "tokyo", "osaka"]),
    ("🇹🇼", ["taiwan", "taipei", "hsinchu"]),
    ("🇨🇳", ["china", "shanghai", "beijing", "shenzhen"]),
    ("🇭🇰", ["hong kong"]),
    ("🇰🇷", ["south korea", "korea", "seoul"]),
    ("🇬🇧", ["united kingdom", " uk", "uk,", "england", "london", "scotland"]),
    ("🇩🇪", ["germany", "berlin", "munich", "hamburg"]),
    ("🇳🇱", ["netherlands", "amsterdam"]),
    ("🇫🇷", ["france", "paris", "lyon"]),
    ("🇸🇪", ["sweden", "stockholm"]),
    ("🇳🇴", ["norway", "oslo"]),
    ("🇩🇰", ["denmark", "copenhagen"]),
    ("🇫🇮", ["finland", "helsinki"]),
    ("🇮🇹", ["italy", "milan", "rome"]),
    ("🇪🇸", ["spain", "madrid", "barcelona"]),
    ("🇧🇪", ["belgium", "brussels"]),
    ("🇨🇭", ["switzerland", "zurich"]),
    ("🇮🇪", ["ireland", "dublin"]),
    ("🇨🇦", ["canada", "toronto", "vancouver", "montreal"]),
    ("🇦🇺", ["australia", "sydney", "melbourne"]),
    ("🇳🇿", ["new zealand", "auckland"]),
    ("🇺🇸", ["united states", "usa", " us,", " us ", "new york", "san francisco",
            "seattle", "los angeles", "boston", "chicago", "austin", "atlanta"]),
]


def get_flag(location: str, is_remote: bool = False) -> str:
    if is_remote or (location and "remote" in location.lower()):
        return "🌍"
    if not location:
        return "🏳"
    loc = location.lower()
    for flag, keywords in FLAG_MAP:
        if any(kw in loc for kw in keywords):
            return flag
    return "🏳"
