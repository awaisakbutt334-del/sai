import os
import io
import re
import base64
import aiohttp
import discord
import discord.app_commands as app_commands
from discord.ext import commands

DISCORD_API = "https://discord.com/api/v10"


def _is_mod(member: discord.Member) -> bool:
    return member.guild_permissions.manage_emojis or member.guild_permissions.administrator


async def _fetch_bytes(url: str, max_bytes: int = 256_000) -> bytes | None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return None
                data = await r.read()
                return data if len(data) <= max_bytes else None
    except Exception:
        return None


def _parse_pairs(raw: str) -> list[tuple[str, str]]:
    """Parse 'name1:url1 , name2:url2' into [(name, url), ...]."""
    pairs = []
    for chunk in re.split(r",\s*", raw.strip()):
        if ":" not in chunk:
            continue
        name, _, rest = chunk.partition(":")
        name = name.strip()
        url = (":" + rest).lstrip(":").strip()
        if name and url.startswith("http"):
            pairs.append((name, url))
    return pairs


def _safe_name(filename: str) -> str:
    """Turn a filename into a valid emoji/sound name."""
    stem = filename.rsplit(".", 1)[0]
    name = re.sub(r"[^a-zA-Z0-9_]", "_", stem)
    return name[:32] or "sound"


# ── Cog ───────────────────────────────────────────────────────────────────────
class ServerTools(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ════════════════════════ EMOJI GROUP ════════════════════════════════════
    emoji_group = app_commands.Group(name="emoji", description="Manage server emojis~")

    # ── /emoji add ───────────────────────────────────────────────────────────
    @emoji_group.command(name="add", description="Add one emoji from a URL or uploaded image")
    @app_commands.describe(
        name="Emoji name (letters, numbers, underscores)",
        url="Public image URL (PNG/JPG/GIF, max 256 KB)",
        image="Or upload an image file directly",
    )
    async def emoji_add(
        self,
        interaction: discord.Interaction,
        name: str,
        url: str | None = None,
        image: discord.Attachment | None = None,
    ):
        await interaction.response.defer(ephemeral=False)
        if not _is_mod(interaction.user):
            await interaction.followup.send("Only mods can manage emojis~ 🔒")
            return
        if not url and not image:
            await interaction.followup.send("Give me a URL or attach an image~")
            return

        img_bytes = (await image.read()) if image else await _fetch_bytes(url, 256_000)
        if not img_bytes:
            await interaction.followup.send("Couldn't fetch that image~ (check URL / file size ≤256 KB)")
            return
        try:
            emoji = await interaction.guild.create_custom_emoji(name=name, image=img_bytes)
            await interaction.followup.send(f"Added {emoji} **:{emoji.name}:** ~")
        except discord.HTTPException as e:
            await interaction.followup.send(f"Discord rejected it~ `{e.text}`")

    # ── /emoji addmultiple ───────────────────────────────────────────────────
    @emoji_group.command(name="addmultiple", description="Add many emojis at once from URLs")
    @app_commands.describe(
        data='Comma-separated name:url pairs — e.g.  pepega:https://… , kekw:https://…',
    )
    async def emoji_addmultiple(self, interaction: discord.Interaction, data: str):
        await interaction.response.defer(ephemeral=False)
        if not _is_mod(interaction.user):
            await interaction.followup.send("Only mods can manage emojis~ 🔒")
            return
        pairs = _parse_pairs(data)
        if not pairs:
            await interaction.followup.send(
                "Couldn't parse any pairs~ Format: `name1:https://url1, name2:https://url2`"
            )
            return
        pairs = pairs[:20]
        added, failed = [], []
        for name, url in pairs:
            img_bytes = await _fetch_bytes(url, 256_000)
            if not img_bytes:
                failed.append(f"`{name}` — fetch failed")
                continue
            try:
                emoji = await interaction.guild.create_custom_emoji(name=name, image=img_bytes)
                added.append(str(emoji))
            except discord.HTTPException as e:
                failed.append(f"`{name}` — {e.text}")

        lines = []
        if added:
            lines.append(f"✅ Added **{len(added)}**: " + " ".join(added))
        if failed:
            lines.append("❌ Failed:\n" + "\n".join(failed))
        await interaction.followup.send("\n".join(lines) or "Nothing happened~")

    # ── /emoji copy ──────────────────────────────────────────────────────────
    @emoji_group.command(
        name="copy",
        description="Copy emojis from any server — paste one or many emoji strings in one go",
    )
    @app_commands.describe(
        emojis="Paste emoji(s) here — e.g.  :PogChamp: :KEKW: :peepoHappy:  (up to 30 at once)",
        rename="Optionally rename a single emoji being copied",
    )
    async def emoji_copy(
        self,
        interaction: discord.Interaction,
        emojis: str,
        rename: str | None = None,
    ):
        await interaction.response.defer(ephemeral=False)
        if not _is_mod(interaction.user):
            await interaction.followup.send("Only mods can manage emojis~ 🔒")
            return

        # Parse every <:name:id> and <a:name:id> in the input
        EMOJI_RE = re.compile(r"<(a?):([a-zA-Z0-9_]+):(\d+)>")
        matches = EMOJI_RE.findall(emojis)

        if not matches:
            await interaction.followup.send(
                "No valid emoji strings found~\n"
                "Paste actual emojis (e.g. `<:KEKW:123456789>`) — they appear when you type them in chat."
            )
            return

        matches = matches[:30]
        added, failed = [], []

        for animated, name, emoji_id in matches:
            use_name = rename if (rename and len(matches) == 1) else name
            ext = "gif" if animated else "png"
            url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}?size=128&quality=lossless"
            img_bytes = await _fetch_bytes(url, 256_000)
            if not img_bytes:
                failed.append(f"`:{name}:` — couldn't fetch from Discord CDN")
                continue
            try:
                new_emoji = await interaction.guild.create_custom_emoji(
                    name=use_name, image=img_bytes
                )
                added.append(str(new_emoji))
            except discord.HTTPException as e:
                failed.append(f"`:{name}:` — {e.text}")

        lines = []
        if added:
            lines.append(f"✅ Copied **{len(added)}**: " + " ".join(added))
        if failed:
            lines.append("❌ Failed:\n" + "\n".join(failed))
        await interaction.followup.send("\n".join(lines) or "Nothing happened~")

    # ── /emoji delete ────────────────────────────────────────────────────────
    @emoji_group.command(name="delete", description="Delete a server emoji by name")
    @app_commands.describe(name="Exact emoji name to delete")
    async def emoji_delete(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=False)
        if not _is_mod(interaction.user):
            await interaction.followup.send("Only mods can delete emojis~ 🔒")
            return
        emoji = discord.utils.get(interaction.guild.emojis, name=name)
        if not emoji:
            await interaction.followup.send(f"No emoji named `{name}` found~")
            return
        await emoji.delete()
        await interaction.followup.send(f"Deleted **:{name}:**~")

    # ── /emoji rename ────────────────────────────────────────────────────────
    @emoji_group.command(name="rename", description="Rename a server emoji")
    @app_commands.describe(old_name="Current emoji name", new_name="New emoji name")
    async def emoji_rename(self, interaction: discord.Interaction, old_name: str, new_name: str):
        await interaction.response.defer(ephemeral=False)
        if not _is_mod(interaction.user):
            await interaction.followup.send("Only mods can rename emojis~ 🔒")
            return
        emoji = discord.utils.get(interaction.guild.emojis, name=old_name)
        if not emoji:
            await interaction.followup.send(f"No emoji named `{old_name}` found~")
            return
        await emoji.edit(name=new_name)
        await interaction.followup.send(f"Renamed **:{old_name}:** → **:{new_name}:** ~")

    # ── /emoji list ──────────────────────────────────────────────────────────
    @emoji_group.command(name="list", description="List all server emojis")
    async def emoji_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        emojis = interaction.guild.emojis
        if not emojis:
            await interaction.followup.send("No custom emojis yet~")
            return
        static = [e for e in emojis if not e.animated]
        animated = [e for e in emojis if e.animated]
        embed = discord.Embed(
            title=f"🖼️ Server Emojis — {len(emojis)}/{interaction.guild.emoji_limit}",
            color=discord.Color.blurple(),
        )
        if static:
            val = " ".join(str(e) for e in static[:40])
            if len(static) > 40:
                val += f"\n*…and {len(static)-40} more*"
            embed.add_field(name=f"Static ({len(static)})", value=val, inline=False)
        if animated:
            val = " ".join(str(e) for e in animated[:40])
            if len(animated) > 40:
                val += f"\n*…and {len(animated)-40} more*"
            embed.add_field(name=f"Animated ({len(animated)})", value=val, inline=False)
        await interaction.followup.send(embed=embed)

    # ════════════════════════ SOUNDBOARD GROUP ════════════════════════════════
    sound_group = app_commands.Group(name="sound", description="Manage server soundboard~")

    async def _create_sound(
        self, guild: discord.Guild, name: str, audio_bytes: bytes, content_type: str
    ) -> dict | str:
        """POST a sound to Discord's soundboard API. Returns the sound dict or an error string."""
        ext = "ogg" if "ogg" in content_type else ("wav" if "wav" in content_type else "mp3")
        b64 = base64.b64encode(audio_bytes).decode()
        payload = {
            "name": name,
            "sound": f"data:audio/{ext};base64,{b64}",
            "volume": 1.0,
        }
        token = os.getenv("DISCORD_BOT_TOKEN", "")
        url = f"{DISCORD_API}/guilds/{guild.id}/soundboard-sounds"
        headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status in (200, 201):
                    return await r.json()
                text = await r.text()
                return f"Discord error {r.status}: {text[:200]}"

    async def _list_sounds(self, guild: discord.Guild) -> list[dict]:
        token = os.getenv("DISCORD_BOT_TOKEN", "")
        url = f"{DISCORD_API}/guilds/{guild.id}/soundboard-sounds"
        headers = {"Authorization": f"Bot {token}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    return data.get("items", data) if isinstance(data, dict) else data
        return []

    async def _delete_sound(self, guild: discord.Guild, sound_id: int) -> bool:
        token = os.getenv("DISCORD_BOT_TOKEN", "")
        url = f"{DISCORD_API}/guilds/{guild.id}/soundboard-sounds/{sound_id}"
        headers = {"Authorization": f"Bot {token}"}
        async with aiohttp.ClientSession() as session:
            async with session.delete(url, headers=headers,
                                      timeout=aiohttp.ClientTimeout(total=10)) as r:
                return r.status == 204

    # ── /sound add ───────────────────────────────────────────────────────────
    @sound_group.command(name="add", description="Add one sound to the soundboard")
    @app_commands.describe(
        file="Audio file (MP3/OGG/WAV, ≤512 KB)",
        name="Sound name (leave blank to use filename)",
        emoji="Optional emoji to show with the sound",
    )
    async def sound_add(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
        name: str | None = None,
        emoji: str | None = None,
    ):
        await interaction.response.defer(ephemeral=False)
        if not _is_mod(interaction.user):
            await interaction.followup.send("Only mods can manage the soundboard~ 🔒")
            return
        sound_name = (name or _safe_name(file.filename))[:32]
        audio = await file.read()
        if len(audio) > 512_000:
            await interaction.followup.send("File must be ≤ 512 KB~")
            return
        result = await self._create_sound(interaction.guild, sound_name, audio, file.content_type or "audio/mpeg")
        if isinstance(result, str):
            await interaction.followup.send(f"Couldn't add that sound~ {result}")
        else:
            await interaction.followup.send(f"Added sound **{sound_name}** to the soundboard~ 🔊")

    # ── /sound addmultiple ───────────────────────────────────────────────────
    @sound_group.command(name="addmultiple", description="Upload up to 5 sounds at once")
    @app_commands.describe(
        file1="Sound file 1", name1="Name for sound 1 (optional)",
        file2="Sound file 2", name2="Name for sound 2 (optional)",
        file3="Sound file 3", name3="Name for sound 3 (optional)",
        file4="Sound file 4", name4="Name for sound 4 (optional)",
        file5="Sound file 5", name5="Name for sound 5 (optional)",
    )
    async def sound_addmultiple(
        self,
        interaction: discord.Interaction,
        file1: discord.Attachment,
        name1: str | None = None,
        file2: discord.Attachment | None = None,
        name2: str | None = None,
        file3: discord.Attachment | None = None,
        name3: str | None = None,
        file4: discord.Attachment | None = None,
        name4: str | None = None,
        file5: discord.Attachment | None = None,
        name5: str | None = None,
    ):
        await interaction.response.defer(ephemeral=False)
        if not _is_mod(interaction.user):
            await interaction.followup.send("Only mods can manage the soundboard~ 🔒")
            return
        pairs = [
            (file1, name1), (file2, name2), (file3, name3),
            (file4, name4), (file5, name5),
        ]
        pairs = [(f, n) for f, n in pairs if f is not None]
        added, failed = [], []
        for att, custom_name in pairs:
            sound_name = (custom_name or _safe_name(att.filename))[:32]
            try:
                audio = await att.read()
                if len(audio) > 512_000:
                    failed.append(f"`{sound_name}` — exceeds 512 KB")
                    continue
                result = await self._create_sound(
                    interaction.guild, sound_name, audio, att.content_type or "audio/mpeg"
                )
                if isinstance(result, str):
                    failed.append(f"`{sound_name}` — {result[:80]}")
                else:
                    added.append(f"`{sound_name}`")
            except Exception as e:
                failed.append(f"`{sound_name}` — {e}")
        lines = []
        if added:
            lines.append(f"✅ Added **{len(added)}** sound{'s' if len(added) != 1 else ''}: " + ", ".join(added))
        if failed:
            lines.append("❌ Failed:\n" + "\n".join(failed))
        await interaction.followup.send("\n".join(lines) or "Nothing happened~")

    # ── /sound list ──────────────────────────────────────────────────────────
    @sound_group.command(name="list", description="List all soundboard sounds in this server")
    async def sound_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        sounds = await self._list_sounds(interaction.guild)
        if not sounds:
            await interaction.followup.send("No custom sounds yet~ Upload some with `/sound add`!")
            return
        custom = [s for s in sounds if s.get("guild_id")]
        default = [s for s in sounds if not s.get("guild_id")]
        embed = discord.Embed(title="🔊 Soundboard", color=discord.Color.green())
        if custom:
            embed.add_field(
                name=f"Server sounds ({len(custom)})",
                value="\n".join(f"• `{s['name']}` (ID: {s['sound_id']})" for s in custom[:20]),
                inline=False,
            )
        if default:
            embed.add_field(
                name=f"Default sounds ({len(default)})",
                value=", ".join(f"`{s['name']}`" for s in default[:20]),
                inline=False,
            )
        await interaction.followup.send(embed=embed)

    # ── /sound delete ────────────────────────────────────────────────────────
    @sound_group.command(name="delete", description="Delete a server soundboard sound by name")
    @app_commands.describe(name="Exact name of the sound to delete")
    async def sound_delete(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=False)
        if not _is_mod(interaction.user):
            await interaction.followup.send("Only mods can delete sounds~ 🔒")
            return
        sounds = await self._list_sounds(interaction.guild)
        match = next(
            (s for s in sounds if s.get("name", "").lower() == name.lower() and s.get("guild_id")),
            None,
        )
        if not match:
            await interaction.followup.send(f"No server sound named `{name}` found~")
            return
        ok = await self._delete_sound(interaction.guild, match["sound_id"])
        if ok:
            await interaction.followup.send(f"Deleted sound **{name}**~ 🗑️")
        else:
            await interaction.followup.send("Couldn't delete that sound~ (Discord API error)")


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerTools(bot))
