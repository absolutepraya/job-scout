"""Discord REST helpers for posting replies."""
import os
import sys
import time
from pathlib import Path

import httpx

MAX_MSG = 1900


def _load_env():
    env = Path.home() / ".hermes" / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env()

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
MANAGE_CHANNEL = os.environ.get("DISCORD_JOB_MANAGE_CHANNEL")


def _split_at_lines(text: str, max_len: int = MAX_MSG) -> list[str]:
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(line) > max_len:
            line = line[:max_len - 1] + "…\n"
        if len(cur) + len(line) > max_len:
            chunks.append(cur)
            cur = line
        else:
            cur += line
    if cur:
        chunks.append(cur)
    return chunks


def post(content: str, channel_id: str | None = None) -> bool:
    """Post a message to a Discord channel. Chunks long content automatically."""
    if not BOT_TOKEN:
        print("[discord] no DISCORD_BOT_TOKEN — stdout fallback", file=sys.stderr)
        print(content)
        return False
    cid = channel_id or MANAGE_CHANNEL
    if not cid:
        print("[discord] no channel_id provided", file=sys.stderr)
        return False
    url = f"https://discord.com/api/v10/channels/{cid}/messages"
    headers = {"Authorization": f"Bot {BOT_TOKEN}"}
    chunks = _split_at_lines(content) if len(content) > MAX_MSG else [content]
    ok_all = True
    for chunk in chunks:
        for attempt in range(3):
            r = httpx.post(url, headers=headers, json={"content": chunk}, timeout=10)
            if r.status_code in (200, 201):
                break
            if r.status_code == 429:
                try:
                    retry = r.json().get("retry_after", 1.0)
                except Exception:
                    retry = 1.0
                time.sleep(retry + 0.1)
                continue
            print(f"[discord] post error {r.status_code}: {r.text[:300]}", file=sys.stderr)
            ok_all = False
            break
        time.sleep(0.4)
    return ok_all


def post_payload(payload: dict, channel_id: str | None = None) -> bool:
    """Post an arbitrary Discord message payload (content / embeds / components).
    No chunking — caller is responsible for staying under Discord's per-message limits.
    """
    if not BOT_TOKEN:
        print("[discord] no DISCORD_BOT_TOKEN — stdout fallback", file=sys.stderr)
        print(payload)
        return False
    cid = channel_id or MANAGE_CHANNEL
    if not cid:
        print("[discord] no channel_id provided", file=sys.stderr)
        return False
    url = f"https://discord.com/api/v10/channels/{cid}/messages"
    headers = {"Authorization": f"Bot {BOT_TOKEN}"}
    for attempt in range(3):
        r = httpx.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code in (200, 201):
            return True
        if r.status_code == 429:
            try:
                retry = r.json().get("retry_after", 1.0)
            except Exception:
                retry = 1.0
            time.sleep(retry + 0.1)
            continue
        print(f"[discord] post_payload error {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return False
    return False
