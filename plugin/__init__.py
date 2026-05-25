"""job-manage-commands plugin — `/job` opens dashboard in #manage.

The dashboard is one persistent-style embed in #manage with 8 buttons.
Clicking a button edits the embed in place to show a different view.

Daily digest button cards in #notification still use the same interaction
listener (interactions.py) — that flow is unchanged.
"""
import logging
import os
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

import dashboard as dashboard_mod
import db as db_mod
import discord_send as discord_send_mod
import interactions as interactions_mod

logger = logging.getLogger(__name__)


def register(ctx):
    """Register the /job slash command + patch DiscordAdapter for button interactions."""
    ctx.register_command(
        "job",
        _handle_job,
        description="Open the job-manage dashboard in #manage",
        args_hint="",
    )
    interactions_mod.patch_discord_adapter()
    logger.info("[job-manage] /job slash command registered + interaction patch applied")


def _handle_job(raw_args: str) -> str:
    """`/job` posts a fresh dashboard embed to #manage. Args are ignored.

    Returns an empty string so Hermes doesn't post a redundant reply. The dashboard
    embed itself is posted via Discord REST in this function.
    """
    try:
        conn = db_mod.open_db()
        try:
            payload = dashboard_mod.build_menu(conn)
        finally:
            conn.close()
        channel = os.environ.get("DISCORD_JOB_MANAGE_CHANNEL", "").strip() or None
        ok = discord_send_mod.post_payload(payload, channel_id=channel)
        if not ok:
            return "⚠ Failed to post dashboard — check logs."
        return ""  # silent — dashboard already posted
    except Exception as e:
        logger.exception("[job-manage] _handle_job failed")
        return f"⚠ Error opening dashboard: {type(e).__name__}: {e}"
