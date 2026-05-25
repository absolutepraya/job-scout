"""Discord interaction handler for job-manage buttons."""
import hashlib
import logging
import os
import sys
import time
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

import db as db_mod

logger = logging.getLogger(__name__)

_patched = False

# Color palette (matches run.py). Tier names are score-tiers, not paint names.
HIGH = 0x377AAF   # score >=70
MED  = 0x3BBBB3   # score 50-69
LOW  = 0x79DC96   # score <50
RED  = 0xF93827   # dismissed
BLUE = 0x2C5EAD   # applied


def short_id(dedup_key: str) -> str:
    return hashlib.sha1(dedup_key.encode("utf-8")).hexdigest()[:12]


def color_for_score(score: int) -> int:
    if score >= 70:
        return HIGH
    if score >= 50:
        return MED
    return LOW


def _lookup_dedup_key(conn, sid: str):
    for row in conn.execute("SELECT dedup_key FROM seen_v2"):
        if short_id(row[0]) == sid:
            return row[0]
    for row in conn.execute("SELECT dedup_key FROM triage"):
        if short_id(row[0]) == sid:
            return row[0]
    return None


def _handle_button_action(action: str, sid: str) -> tuple[str, bool, dict | None]:
    """Returns (reply_text, success, snapshot_dict_or_None)."""
    try:
        conn = db_mod.open_db()
    except Exception as e:
        return (f"⚠ DB error: {type(e).__name__}", False, None)
    try:
        dedup_key = _lookup_dedup_key(conn, sid)
        if not dedup_key:
            return ("⚠ Posting not in scrape history.", False, None)

        existing = db_mod.get_triage(conn, dedup_key)
        snap_row = conn.execute(
            "SELECT dedup_key, job_url, title, company, location, site FROM seen_v2 WHERE dedup_key = ?",
            (dedup_key,)
        ).fetchone()
        if not snap_row and not existing:
            return ("⚠ Posting not in scrape history.", False, None)
        snap = existing if existing else {
            "dedup_key": snap_row[0], "job_url": snap_row[1],
            "title": snap_row[2], "company": snap_row[3],
            "location": snap_row[4], "site": snap_row[5],
        }

        title = snap.get("title") or "?"
        co = snap.get("company") or "?"

        if action == "save":
            if existing and existing["status"] == "saved":
                return (f"🔖 Already saved", True, snap)
            db_mod.upsert_triage(conn, snap, "saved")
            return (f"🔖 Saved", True, snap)
        if action == "dismiss":
            db_mod.upsert_triage(conn, snap, "dismissed")
            return (f"❌ Dismissed", True, snap)
        if action == "applied":
            db_mod.upsert_triage(conn, snap, "applied")
            return (f"✅ Applied", True, snap)
        return (f"⚠ Unknown action: {action}", False, None)
    except Exception as e:
        logger.exception("[job-manage] button action error")
        return (f"⚠ Error: {type(e).__name__}: {e}", False, None)
    finally:
        conn.close()


def _ts_now() -> str:
    return time.strftime("%H:%M WIB")


async def _handle_dashboard_view(interaction, view: str, arg: str = ""):
    """Edit the embed in place to show the requested dashboard view."""
    import dashboard as dashboard_mod
    if view == "refresh":
        view = "menu"

    conn = db_mod.open_db()
    try:
        payload = dashboard_mod.build(view, conn, arg)
    finally:
        conn.close()

    try:
        import discord as dpy
        new_embed = dpy.Embed.from_dict(payload["embeds"][0])
        view_obj = _components_to_view(payload.get("components", []))
        await interaction.response.edit_message(embed=new_embed, view=view_obj)
    except Exception:
        logger.exception("[job-manage] dashboard edit_message failed")
        try:
            await interaction.response.send_message("⚠ Failed to switch view.", ephemeral=True)
        except Exception:
            pass


def _components_to_view(components: list):
    """Convert raw component dicts to a discord.ui.View with persistent buttons.
    Phase-1: only Button(style=secondary/primary, custom_id, emoji, label, disabled).
    """
    import discord as dpy
    from discord import ui as dui
    view = dui.View(timeout=None)
    for row in components or []:
        for c in row.get("components", []):
            if c.get("type") != 2:
                continue
            style_num = c.get("style", 2)
            style_map = {
                1: dpy.ButtonStyle.primary,
                2: dpy.ButtonStyle.secondary,
                3: dpy.ButtonStyle.success,
                4: dpy.ButtonStyle.danger,
                5: dpy.ButtonStyle.link,
            }
            style = style_map.get(style_num, dpy.ButtonStyle.secondary)
            emoji_name = (c.get("emoji") or {}).get("name")
            kwargs = {
                "style": style,
                "label": c.get("label"),
                "disabled": bool(c.get("disabled")),
            }
            if emoji_name:
                kwargs["emoji"] = emoji_name
            if style_num == 5:
                kwargs["url"] = c.get("url")
            else:
                kwargs["custom_id"] = c.get("custom_id")
            button = dui.Button(**kwargs)
            view.add_item(button)
    return view



# ----- Manual "Add Job" modal -----

def _classify_workmode_from_location(location: str) -> str:
    loc = (location or "").lower()
    if "remote" in loc:
        return "remote"
    indo = ["jakarta", "tangerang", "bandung", "depok", "bekasi", "bali",
            "surabaya", "yogyakarta", "semarang", "medan", "indonesia"]
    if any(kw in loc for kw in indo):
        return "onsite"
    return "relocate"


def _build_add_modal(status: str):
    """Returns a discord.ui.Modal for adding a job manually under <status>."""
    import discord as dpy
    from discord import ui

    class _AddJobModal(ui.Modal):
        def __init__(self):
            super().__init__(
                title=f"Log {status.title()} Job",
                custom_id=f"jm:addmodal:{status}",
                timeout=600,
            )
            self.add_item(ui.TextInput(
                label="Title", custom_id="jm:add:title",
                placeholder="Software Engineer Intern",
                required=True, max_length=200,
            ))
            self.add_item(ui.TextInput(
                label="Company", custom_id="jm:add:company",
                placeholder="Acme Corp",
                required=True, max_length=100,
            ))
            self.add_item(ui.TextInput(
                label="URL", custom_id="jm:add:url",
                placeholder="https://...",
                required=True, max_length=400,
            ))
            self.add_item(ui.TextInput(
                label="Location",
                custom_id="jm:add:location",
                placeholder="Jakarta, Indonesia",
                required=False, max_length=200,
            ))
            self.add_item(ui.TextInput(
                label="Notes (optional)",
                custom_id="jm:add:notes",
                placeholder="Recruiter, salary range, anything else",
                style=dpy.TextStyle.paragraph,
                required=False, max_length=1000,
            ))

        async def on_submit(self, interaction):
            # No-op — the actual handler runs from the raw on_interaction
            # listener so we keep one code path. Suppress the default
            # "interaction failed" message.
            pass

    return _AddJobModal()


async def _handle_add_modal_submit(interaction, status: str):
    """Read fields from the submitted modal, insert into triage, ack ephemerally."""
    data = getattr(interaction, "data", None) or {}
    fields = {}
    for row in data.get("components", []):
        for c in row.get("components", []):
            cid = c.get("custom_id", "")
            val = (c.get("value") or "").strip()
            if cid.startswith("jm:add:"):
                fields[cid.split(":", 2)[2]] = val

    title = fields.get("title", "")
    company = fields.get("company", "")
    url = fields.get("url", "")
    location = fields.get("location", "")
    notes = fields.get("notes", "") or None

    if not (title and company and url):
        try:
            await interaction.response.send_message(
                "⚠ Title, Company, and URL are required.", ephemeral=True
            )
        except Exception:
            pass
        return

    # dedup_key matches run.py: "{title_lower_strip}|{company_lower_strip}".
    # If the user later scrapes the same posting, dedup will join the rows.
    dedup = f"{title.lower().strip()}|{company.lower().strip()}"

    snap = {
        "dedup_key": dedup,
        "job_url": url,
        "title": title,
        "company": company,
        "location": location,
        "score": None,
        "workmode": _classify_workmode_from_location(location),
    }

    conn = db_mod.open_db()
    try:
        db_mod.upsert_triage(conn, snap, status, notes=notes)
    except Exception as e:
        logger.exception("[job-manage] manual add failed")
        try:
            await interaction.response.send_message(
                f"⚠ Failed to add: {type(e).__name__}: {e}", ephemeral=True
            )
        except Exception:
            pass
        return
    finally:
        conn.close()

    try:
        await interaction.response.send_message(
            f"✓ Logged as **{status}**: **{title}** @ **{company}**\n"
            f"<{url}>",
            ephemeral=True,
        )
    except Exception:
        logger.exception("[job-manage] modal ack send failed")


async def _on_interaction(interaction):
    try:
        data = getattr(interaction, "data", None) or {}
        custom_id = data.get("custom_id", "")
        if not custom_id.startswith("jm:"):
            return

        # jm:noop:* — disabled boundary buttons. Discord requires unique
        # custom_ids even on disabled components; we use noop ids to satisfy
        # that and silently ack here so nothing flashes for the clicker.
        if custom_id.startswith("jm:noop:"):
            try:
                await interaction.response.defer()
            except Exception:
                pass
            return

        user_id = str(interaction.user.id) if interaction.user else ""
        allowlist = set(s.strip() for s in os.environ.get("DISCORD_ALLOWED_USERS", "").split(",") if s.strip())
        if user_id not in allowlist:
            await interaction.response.send_message("⚠ Not allowed.", ephemeral=True)
            return

        # Modal submit fires as a separate interaction; handle by custom_id prefix.
        if custom_id.startswith("jm:addmodal:"):
            status = custom_id.split(":", 2)[2]
            await _handle_add_modal_submit(interaction, status)
            return

        # Split with no max so we can capture optional 4th arg for jm:dash:<view>:<arg>
        parts = custom_id.split(":")
        if len(parts) < 3:
            await interaction.response.send_message("⚠ Malformed button id.", ephemeral=True)
            return
        _, action = parts[0], parts[1]

        # ── Dashboard view switch (jm:dash:<view>[:<arg>[:<arg2>...]]) ──
        if action == "dash":
            view = parts[2] if len(parts) >= 3 else "menu"
            # Join everything after the view name with ':' so history's
            # "yyyymmdd:page" format passes through intact
            arg = ":".join(parts[3:]) if len(parts) > 3 else ""
            await _handle_dashboard_view(interaction, view, arg)
            return

        # ── "Add Manual" button (jm:add:<status>) → open the modal ──
        if action == "add":
            status = parts[2] if len(parts) >= 3 else "applied"
            try:
                await interaction.response.send_modal(_build_add_modal(status))
            except Exception:
                logger.exception("[job-manage] send_modal failed")
                try:
                    await interaction.response.send_message(
                        "⚠ Could not open the form.", ephemeral=True
                    )
                except Exception:
                    pass
            return

        # ── Daily-digest card triage (jm:<save|dismiss|applied>:<sid>) ──
        target = parts[2]

        # ── Daily-digest card triage (jm:save|dismiss|applied:<sid>) ──
        sid = target
        reply, ok, snap = _handle_button_action(action, sid)

        try:
            import discord as dpy
            msg = interaction.message
            if msg and msg.embeds:
                old = msg.embeds[0]
                new_color = old.color
                if action == "dismiss":
                    new_color = dpy.Color(RED)
                elif action == "applied":
                    new_color = dpy.Color(BLUE)
                new_embed = dpy.Embed(
                    title=old.title,
                    url=old.url,
                    description=old.description,
                    color=new_color,
                )
                footer_text = f"{reply} · {_ts_now()}"
                new_embed.set_footer(text=footer_text)
                await interaction.response.edit_message(embeds=[new_embed], view=None)
            else:
                await interaction.response.edit_message(content=reply, view=None)
        except Exception:
            logger.exception("[job-manage] edit_message failed")
            try:
                await interaction.response.send_message(reply, ephemeral=True)
            except Exception:
                pass
    except Exception as e:
        logger.exception("[job-manage] on_interaction error")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"⚠ {type(e).__name__}: {e}", ephemeral=True)
        except Exception:
            pass


def patch_discord_adapter():
    global _patched
    if _patched:
        return
    try:
        from gateway.platforms.discord import DiscordAdapter
    except Exception as e:
        logger.warning("[job-manage] cannot import DiscordAdapter: %s", e)
        return

    original_connect = DiscordAdapter.connect

    async def patched_connect(self):
        result = await original_connect(self)
        if result and self._client and not getattr(self, "_job_manage_hooked", False):
            try:
                self._client.add_listener(_on_interaction, "on_interaction")
                self._job_manage_hooked = True
                logger.info("[job-manage] attached on_interaction listener to discord client")
            except Exception as e:
                logger.warning("[job-manage] failed to attach listener: %s", e)
        return result

    DiscordAdapter.connect = patched_connect
    _patched = True
    logger.info("[job-manage] patched DiscordAdapter.connect")
