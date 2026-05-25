"""LLM summarizer for job descriptions via the local hermes oneshot CLI.

Called from run.py at scrape time. Output is cached in seen_v2 so we never
re-summarize the same posting. Failure modes (timeout, parse error, empty
description) return ([], []) which the renderer treats as "no bullets yet".
"""
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

HERMES_PY = Path("/home/praya/.hermes/hermes-agent/venv/bin/python")
HERMES_BIN = Path("/home/praya/.hermes/hermes-agent/hermes")

PROMPT = """You summarize internship job postings into 2 brief sections for a Discord card. Be terse. Max 6 words per bullet. Max 3 bullets per section. No fluff.

Output EXACTLY this format. No preamble, no headers beyond the two below, no extra text:

EXPERIENCE:
- <bullet>
- <bullet>

JOB DESC:
- <bullet>
- <bullet>

---
EXAMPLE INPUT:
Backend engineering intern. We need 1-2 years experience in Python and Django. You will build REST APIs, work with PostgreSQL, and collaborate with our platform team on payments infrastructure. Familiarity with Docker is a plus.

EXAMPLE OUTPUT:
EXPERIENCE:
- 1-2 yrs Python/Django
- Docker familiarity (plus)

JOB DESC:
- Build REST APIs
- Work with PostgreSQL
- Payments platform collaboration

---
NOW SUMMARIZE THIS POSTING:
{description}
"""


def summarize(description: str, timeout: int = 45) -> tuple[list[str], list[str]]:
    """Return (experience_bullets, jobdesc_bullets). Empty lists on any failure."""
    if not description or len(description.strip()) < 80:
        return ([], [])
    desc = description.strip()
    if len(desc) > 4000:
        desc = desc[:4000]
    prompt = PROMPT.format(description=desc)
    try:
        result = subprocess.run(
            [str(HERMES_PY), str(HERMES_BIN),
             "--provider", "copilot", "-m", "gpt-5.4",
             "-z", prompt, "--ignore-rules", "--ignore-user-config"],
            capture_output=True, text=True, timeout=timeout,
            cwd="/tmp",
        )
        if result.returncode != 0:
            logger.warning("hermes oneshot rc=%s stderr=%s", result.returncode, (result.stderr or "")[:200])
            return ([], [])
        return _parse(result.stdout or "")
    except subprocess.TimeoutExpired:
        logger.warning("hermes oneshot timeout after %ds", timeout)
        return ([], [])
    except Exception as e:
        logger.warning("hermes oneshot error: %s", e)
        return ([], [])


def _parse(text: str) -> tuple[list[str], list[str]]:
    exp, desc = [], []
    mode = None
    for raw in text.splitlines():
        s = raw.strip()
        upper = s.upper()
        if upper.startswith("EXPERIENCE"):
            mode = "exp"
            continue
        if upper.startswith("JOB DESC") or upper.startswith("JOBDESC"):
            mode = "desc"
            continue
        if s.startswith(("- ", "• ", "* ")):
            bullet = s.lstrip("-•* ").strip()
            if not bullet:
                continue
            if mode == "exp" and len(exp) < 3:
                exp.append(bullet)
            elif mode == "desc" and len(desc) < 3:
                desc.append(bullet)
    return (exp, desc)


if __name__ == "__main__":
    import sys, json
    sample = sys.stdin.read()
    e, d = summarize(sample)
    print(json.dumps({"experience": e, "jobdesc": d}, indent=2))
