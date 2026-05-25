"""Discord delivery for job-watcher.

Accepts list of messages where each item is either:
  - str: plain text (legacy compat)
  - dict with "content" key: text message
  - dict with "embeds"/"components": rich card

Sends one Discord message per item, with 429-retry + per-message delay.
"""
import os
import sys
import time
from pathlib import Path

import httpx

MAX_DISCORD_MSG = 1900


def _load_env_from_file(env_path: Path):
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env_from_file(Path.home() / ".hermes" / ".env")


def _split_at_lines(text: str, max_len: int = MAX_DISCORD_MSG):
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


def _post_payload(payload: dict, webhook: str = None, bot_token: str = None, channel_id: str = None) -> bool:
    """Post an arbitrary Discord message payload (content/embeds/components)."""
    for attempt in range(3):
        if webhook:
            r = httpx.post(webhook, json=payload, timeout=10)
            ok = r.status_code in (200, 204)
        else:
            url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
            headers = {"Authorization": f"Bot {bot_token}"}
            r = httpx.post(url, headers=headers, json=payload, timeout=10)
            ok = r.status_code in (200, 201)
        if ok:
            return True
        if r.status_code == 429:
            try:
                retry_after = r.json().get("retry_after", 1.0)
            except Exception:
                retry_after = 1.0
            time.sleep(retry_after + 0.1)
            continue
        print(f"discord post error {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return False
    return False


def _normalize(section):
    """Coerce a section to a Discord payload dict."""
    if isinstance(section, str):
        return {"content": section}
    if isinstance(section, dict):
        return section
    return {"content": str(section)}


def send_sections(messages, channel_id_override: str | None = None) -> bool:
    webhook = None if channel_id_override else os.environ.get("DISCORD_JOB_WATCHER_WEBHOOK")
    bot_token = os.environ.get("DISCORD_BOT_TOKEN")
    channel_id = channel_id_override or os.environ.get("DISCORD_JOB_WATCHER_CHANNEL")

    if not webhook and not (bot_token and channel_id):
        print("[notify] no DISCORD_JOB_WATCHER_WEBHOOK or BOT_TOKEN+CHANNEL — stdout fallback", file=sys.stderr)
        for m in messages:
            print(m)
        return False

    ok_all = True
    for section in messages:
        payload = _normalize(section)
        # If it's text content and oversized, split by lines into multiple posts
        if "content" in payload and "embeds" not in payload and "components" not in payload:
            content = payload["content"]
            if len(content) > MAX_DISCORD_MSG:
                for chunk in _split_at_lines(content):
                    ok_all &= _post_payload({"content": chunk}, webhook=webhook, bot_token=bot_token, channel_id=channel_id)
                    time.sleep(0.4)
                continue
        ok = _post_payload(payload, webhook=webhook, bot_token=bot_token, channel_id=channel_id)
        ok_all = ok_all and ok
        time.sleep(0.4)
    return ok_all


def send(text: str) -> bool:
    return send_sections([text])


if __name__ == "__main__":
    text = sys.stdin.read()
    ok = send(text)
    sys.exit(0 if ok else 1)
