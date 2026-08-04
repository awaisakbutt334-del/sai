import io
import os
import re
import asyncio
import tempfile
import aiohttp
import discord
from discord.ext import commands
from collections import defaultdict, deque

CHARACTER_ID_RE = re.compile(r"^[0-9A-Za-z]{6,40}$")


def is_valid_character_id(cid: str) -> bool:
    return bool(CHARACTER_ID_RE.match(cid))

import db
import netmarble
import voice as vc
import keepalive
import games
import moderation

# Load Opus for voice support
try:
    discord.opus.load_opus("libopus.so.0")
except Exception:
    pass

OWNER_ID = 1238527430575128598

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class AlyaBot(commands.Bot):
    async def setup_hook(self):
        await self.load_extension("games")
        await self.load_extension("moderation")
        await self.load_extension("music")
        await self.load_extension("server_tools")


bot = AlyaBot(command_prefix=["?", "a!"], intents=intents, help_command=None)



def _mask(cid: str) -> str:
    if len(cid) <= 4:
        return "•" * len(cid)
    return "•" * (len(cid) - 4) + cid[-4:]


def redeem_embed(user: discord.User, code: str, results: list[dict]) -> discord.Embed:
    ok = sum(1 for r in results if r["success"])
    total = len(results)
    color = discord.Color.green() if ok == total else (
        discord.Color.gold() if ok > 0 else discord.Color.red()
    )
    if ok == total:
        title = "Code Redeemed Successfully"
    elif ok > 0:
        title = "Code Partially Redeemed"
    else:
        title = "Code Redemption Failed"

    e = discord.Embed(
        title=title,
        description=(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"**Hunter** — {user.mention}\n"
            f"**Code** — `{code}`\n"
            f"**Accounts Processed** — `{total}`\n"
            f"**Successful** — `{ok}`  •  **Failed** — `{total - ok}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    e.set_thumbnail(url=user.display_avatar.url)
    for i, r in enumerate(results, start=1):
        status = "✓ Success" if r["success"] else "✗ Failed"
        msg = (r["message"] or "(no response)").strip()
        if len(msg) > 400:
            msg = msg[:400] + "..."
        e.add_field(
            name=f"Account #{i} — {status}",
            value=f"```\n{msg}\n```",
            inline=False,
        )
    e.set_footer(text="Solo Leveling • Code Redemption System")
    return e


async def _try_fetch_msg(channel: discord.TextChannel, msg_id: int) -> tuple[int, str | None]:
    try:
        msg = await channel.fetch_message(msg_id)
        if msg.attachments:
            return msg_id, msg.attachments[0].url
        for emb in msg.embeds:
            if emb.image and emb.image.url:
                return msg_id, emb.image.url
            if emb.url:
                return msg_id, emb.url
        return msg_id, None
    except discord.NotFound:
        return msg_id, None
    except discord.Forbidden:
        return msg_id, None
    except Exception:
        return msg_id, None


async def _load_welcome_gifs():
    guild = bot.guilds[0] if bot.guilds else None
    if not guild:
        return
    text_channels = [c for c in guild.channels if isinstance(c, discord.TextChannel)]
    found: dict[int, str] = {}
    for channel in text_channels:
        if len(found) == len(WELCOME_GIF_MSG_IDS):
            break
        remaining = [mid for mid in WELCOME_GIF_MSG_IDS if mid not in found]
        results = await asyncio.gather(*[_try_fetch_msg(channel, mid) for mid in remaining])
        for msg_id, url in results:
            if url:
                found[msg_id] = url
                print(f"[gifs] ✓ {msg_id} in #{channel.name} -> {url[:80]}", flush=True)
    if found:
        WELCOME_GIFS.clear()
        WELCOME_GIFS.extend(found.values())
        print(f"[gifs] loaded {len(WELCOME_GIFS)} custom GIFs", flush=True)
    else:
        print(f"[gifs] custom GIFs not found — using {len(WELCOME_GIFS)} fallback GIFs", flush=True)


@bot.event
async def on_ready():
    db.init()
    bot.add_view(ClaimView())
    try:
        for g in bot.guilds:
            bot.tree.copy_global_to(guild=g)
            synced = await bot.tree.sync(guild=g)
            print(f"Synced {len(synced)} commands to guild {g.name} ({g.id})")
        synced_global = await bot.tree.sync()
        print(f"Synced {len(synced_global)} global commands")
    except Exception as e:
        print(f"Slash sync failed: {e}")
    print(f"Logged in as {bot.user} ({bot.user.id})")
    await bot.change_presence(
        activity=discord.CustomActivity(name="🍀"), status=discord.Status.online
    )
    await _load_welcome_gifs()


# =============== ?chat enable / disable ===============

@bot.group(name="chat", invoke_without_command=True)
async def chat_group(ctx: commands.Context):
    cid = str(ctx.channel.id)
    state = "enabled" if db.is_chat_enabled(cid) else "disabled"
    await ctx.send(f"Chat is **{state}** in this channel. Use `?chat enable` or `?chat disable`.")


def _is_admin(ctx: commands.Context) -> bool:
    if ctx.guild is None:
        return True
    perms = ctx.author.guild_permissions
    return perms.administrator or perms.manage_guild


@chat_group.command(name="enable")
async def chat_enable(ctx: commands.Context):
    if not _is_admin(ctx):
        await ctx.send("Only server admins can use this command.")
        return
    db.set_chat_enabled(str(ctx.channel.id), True)
    await ctx.send(f"Chat **enabled** in {ctx.channel.mention}. I'm listening here.")


@chat_group.command(name="disable")
async def chat_disable(ctx: commands.Context):
    if not _is_admin(ctx):
        await ctx.send("Only server admins can use this command.")
        return
    db.set_chat_enabled(str(ctx.channel.id), False)
    await ctx.send(f"Chat **disabled** in {ctx.channel.mention}. I'll stay quiet here.")


# =============== /register ===============

register = discord.app_commands.Group(name="register", description="Link Solo Leveling accounts")


@register.command(name="set", description="Link a single Solo Leveling character ID")
async def register_set(interaction: discord.Interaction, character_id: str):
    cid = character_id.strip()
    if not is_valid_character_id(cid):
        await interaction.response.send_message(
            f"Invalid character ID: `{cid}`\n"
            "A character ID should only contain letters and numbers (no spaces or symbols).",
            ephemeral=True,
        )
        return
    db.add_accounts(str(interaction.user.id), [cid])
    total_linked = len(db.get_accounts(str(interaction.user.id)))
    e = discord.Embed(
        title="Account Successfully Linked",
        description=(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"**Hunter** — {interaction.user.mention}\n"
            f"**Status** — Linked\n"
            f"**Total Accounts on File** — `{total_linked}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"This account is now ready to receive code rewards. "
            f"Use `/redeem code <code>` to redeem on all linked accounts at once."
        ),
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    e.set_thumbnail(url=interaction.user.display_avatar.url)
    e.set_footer(text="Solo Leveling • Account Registration")
    await interaction.response.send_message(embed=e)


@register.command(
    name="multiple",
    description="Link multiple character IDs at once (comma or space separated)",
)
async def register_multiple(interaction: discord.Interaction, character_ids: str):
    raw_ids = [x.strip() for x in character_ids.replace(",", " ").split() if x.strip()]
    if not raw_ids:
        await interaction.response.send_message("No valid IDs provided.", ephemeral=True)
        return
    ids = [c for c in raw_ids if is_valid_character_id(c)]
    invalid = [c for c in raw_ids if not is_valid_character_id(c)]
    if invalid and not ids:
        await interaction.response.send_message(
            "All IDs were invalid. Character IDs should only contain letters and numbers.\n"
            f"Invalid: {', '.join(f'`{c}`' for c in invalid)}",
            ephemeral=True,
        )
        return
    db.add_accounts(str(interaction.user.id), ids)
    total_linked = len(db.get_accounts(str(interaction.user.id)))
    e = discord.Embed(
        title="Accounts Successfully Linked",
        description=(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"**Hunter** — {interaction.user.mention}\n"
            f"**New Accounts Added** — `{len(ids)}`\n"
            f"**Total Accounts on File** — `{total_linked}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"All linked accounts are ready to redeem codes. "
            f"Use `/redeem code <code>` to redeem on all of them at once, "
            f"or `/redeem solo` to redeem on a single one."
        ),
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    e.set_thumbnail(url=interaction.user.display_avatar.url)
    e.set_footer(text="Solo Leveling • Account Registration")
    if invalid:
        e.add_field(
            name="Skipped (invalid format)",
            value=", ".join(f"`{c}`" for c in invalid),
            inline=False,
        )
    await interaction.response.send_message(embed=e)


@register.command(name="list", description="List your linked character IDs")
async def register_list(interaction: discord.Interaction):
    ids = db.get_accounts(str(interaction.user.id))
    if not ids:
        await interaction.response.send_message(
            "No accounts linked yet. Use `/register set` to link one.", ephemeral=True
        )
        return
    e = discord.Embed(
        title="Your Linked Accounts",
        description=(
            f"You have **{len(ids)}** account(s) linked.\n\n"
            + "\n".join(f"`#{i+1}` — `{_mask(c)}`" for i, c in enumerate(ids))
        ),
        color=discord.Color.blurple(),
    )
    e.set_footer(text="Only you can see this list")
    await interaction.response.send_message(embed=e, ephemeral=True)


@register.command(name="remove", description="Unlink one of your character IDs")
async def register_remove(interaction: discord.Interaction, character_id: str):
    db.remove_account(str(interaction.user.id), character_id.strip())
    await interaction.response.send_message(
        f"Removed `{character_id}`.", ephemeral=True
    )


@register.command(name="clear", description="Reset and remove ALL your linked accounts")
async def register_clear(interaction: discord.Interaction):
    removed = db.clear_accounts(str(interaction.user.id))
    if removed == 0:
        await interaction.response.send_message(
            "You had no linked accounts to clear.", ephemeral=True
        )
        return
    e = discord.Embed(
        title="All Accounts Cleared",
        description=(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"**Hunter** — {interaction.user.mention}\n"
            f"**Accounts Removed** — `{removed}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"All linked accounts have been unlinked. "
            f"You can register again any time with `/register set` or `/register multiple`."
        ),
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow(),
    )
    e.set_thumbnail(url=interaction.user.display_avatar.url)
    e.set_footer(text="Solo Leveling • Account Reset")
    await interaction.response.send_message(embed=e)


bot.tree.add_command(register)


# =============== /about ===============
about = discord.app_commands.Group(name="about", description="Learn about Alya")


@about.command(name="me", description="Who I am and what I can do")
async def about_me(interaction: discord.Interaction):
    e = discord.Embed(
        title="ALYA — S-Rank Hunter",
        description=(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "I am **Alya**, an S-Rank hunter and the personal companion of "
            f"<@{ai.OWNER_ID}>.\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow(),
    )
    e.set_thumbnail(url=interaction.client.user.display_avatar.url)
    e.add_field(
        name="Account Linking",
        value=(
            "`/register set` — link a single character ID\n"
            "`/register multiple` — link several at once\n"
            "`/register list` — see your linked accounts\n"
            "`/register remove` — unlink one\n"
            "`/register clear` — wipe them all"
        ),
        inline=False,
    )
    e.add_field(
        name="Code Redemption",
        value=(
            "`/redeem code <code>` — redeem on every linked account\n"
            "`/redeem solo <code> <id>` — redeem on one account\n"
            "`/claim post <code>` — post a code with a public Redeem button"
        ),
        inline=False,
    )
    e.add_field(
        name="Chat",
        value=(
            "Mention me, reply to me, or DM me to talk.\n"
            "Admins can toggle me with `?chat enable` / `?chat disable`."
        ),
        inline=False,
    )
    e.set_footer(text="Storm Tigers • Alya at your service")
    await interaction.response.send_message(embed=e)


bot.tree.add_command(about)


# =============== /redeem ===============

redeem = discord.app_commands.Group(name="redeem", description="Redeem Solo Leveling codes")


@redeem.command(name="code", description="Redeem a code on ALL your linked accounts")
async def redeem_code(interaction: discord.Interaction, code: str):
    ids = db.get_accounts(str(interaction.user.id))
    if not ids:
        await interaction.response.send_message(
            "You have no linked accounts. Use `/register set` first.", ephemeral=True
        )
        return
    await interaction.response.defer()
    results = await netmarble.redeem_many(ids, code.strip())
    await interaction.followup.send(embed=redeem_embed(interaction.user, code, results))


@redeem.command(name="solo", description="Redeem a code on ONE specific linked account")
async def redeem_solo(interaction: discord.Interaction, code: str, character_id: str):
    ids = db.get_accounts(str(interaction.user.id))
    if character_id not in ids:
        await interaction.response.send_message(
            f"`{character_id}` isn't linked to you. Link it with `/register set` first.",
            ephemeral=True,
        )
        return
    await interaction.response.defer()
    results = await netmarble.redeem_many([character_id], code.strip())
    await interaction.followup.send(embed=redeem_embed(interaction.user, code, results))


bot.tree.add_command(redeem)


# =============== /claim post (button) ===============

CODE_RE = re.compile(r"\*\*Code\*\*\s*[—-]\s*`([^`]+)`")


def _extract_code_from_message(message: discord.Message) -> str | None:
    for emb in message.embeds:
        text = (emb.description or "")
        m = CODE_RE.search(text)
        if m:
            return m.group(1).strip()
    return None


class ClaimView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Redeem this code",
        style=discord.ButtonStyle.success,
        custom_id="alya:claim_redeem",
    )
    async def redeem_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        code = _extract_code_from_message(interaction.message) if interaction.message else None
        if not code:
            await interaction.response.send_message(
                "Sorry, I couldn't read the code from this post.", ephemeral=True
            )
            return
        ids = db.get_accounts(str(interaction.user.id))
        if not ids:
            await interaction.response.send_message(
                "You have no linked accounts. Use `/register set` first.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        results = await netmarble.redeem_many(ids, code)
        await interaction.followup.send(
            embed=redeem_embed(interaction.user, code, results),
            ephemeral=True,
        )


claim = discord.app_commands.Group(name="claim", description="Post claimable codes")


@claim.command(name="post", description="Post a code with a Redeem button anyone can use")
async def claim_post(interaction: discord.Interaction, code: str):
    e = discord.Embed(
        title="A New Code Has Dropped",
        description=(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"**Code** — `{code}`\n"
            f"**Posted By** — {interaction.user.mention}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Click the **Redeem this code** button below and it will be applied "
            f"automatically to all of your linked accounts.\n\n"
            f"Haven't registered yet? Use `/register set` to link your account first."
        ),
        color=discord.Color.gold(),
        timestamp=discord.utils.utcnow(),
    )
    e.set_thumbnail(url=interaction.user.display_avatar.url)
    e.set_footer(text="Solo Leveling • Public Code Drop")
    await interaction.response.send_message(embed=e, view=ClaimView())


bot.tree.add_command(claim)


# =============== /role ===============

import random

WELCOME_CHANNEL_ID = 1493504455264698399
GB_GUIDE_CHANNEL_ID = 1493672315450560614
ST_MEMBER_ROLE_ID = 1493520900027318323

WELCOME_GIF_MSG_IDS = [
    1507372346778980563,
    1507372247940071527,
    1507372212041285654,
    1507372180139413535,
    1507372134366707733,
    1507372102716489911,
]
# Fallback anime welcome GIFs — replaced by user's own GIFs once /loadgifs is run
WELCOME_GIFS: list[str] = [
    "https://media.tenor.com/x8v1oNUOmg4AAAAC/anime-wave.gif",
    "https://media.tenor.com/jDjCBjBPuusAAAAC/welcome-anime.gif",
    "https://media.tenor.com/F1iCDFKBpUoAAAAC/anime-girl-wave.gif",
    "https://media.tenor.com/sV_4s5Yv6TIAAAAC/anime-celebrate.gif",
    "https://media.tenor.com/wMblk6XVnpgAAAAC/welcome-hi.gif",
    "https://media.tenor.com/7F1YlfJiajsAAAAC/anime-happy.gif",
]

role_group = discord.app_commands.Group(name="role", description="Add or remove roles from members")


def _has_role_perms(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    perms = interaction.user.guild_permissions
    return perms.administrator or perms.manage_roles


@role_group.command(name="add", description="Give a role to a member")
@discord.app_commands.describe(member="The member to give the role to", role="The role to assign")
async def role_add(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if not _has_role_perms(interaction):
        await interaction.response.send_message("You need Manage Roles permission.", ephemeral=True)
        return
    if role >= interaction.guild.me.top_role:
        await interaction.response.send_message(
            f"I can't assign **{role.name}** — it's equal to or higher than my own role.", ephemeral=True
        )
        return
    if role in member.roles:
        await interaction.response.send_message(
            f"{member.mention} already has **{role.name}**.", ephemeral=True
        )
        return
    await member.add_roles(role, reason=f"Assigned by {interaction.user}")
    e = discord.Embed(
        description=f"✓ Gave **{role.name}** to {member.mention}.",
        color=role.color if role.color.value else discord.Color.green(),
    )
    await interaction.response.send_message(embed=e)
    # Welcome is handled automatically by on_member_update


@role_group.command(name="remove", description="Remove a role from a member")
@discord.app_commands.describe(member="The member to remove the role from", role="The role to remove")
async def role_remove(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if not _has_role_perms(interaction):
        await interaction.response.send_message("You need Manage Roles permission.", ephemeral=True)
        return
    if role >= interaction.guild.me.top_role:
        await interaction.response.send_message(
            f"I can't remove **{role.name}** — it's equal to or higher than my own role.", ephemeral=True
        )
        return
    if role not in member.roles:
        await interaction.response.send_message(
            f"{member.mention} doesn't have **{role.name}**.", ephemeral=True
        )
        return
    await member.remove_roles(role, reason=f"Removed by {interaction.user}")
    e = discord.Embed(
        description=f"✓ Removed **{role.name}** from {member.mention}.",
        color=discord.Color.red(),
    )
    await interaction.response.send_message(embed=e)


bot.tree.add_command(role_group)


# =============== /join /leave + ! TTS ===============

voice_cmds = discord.app_commands.Group(name="voice", description="Voice channel controls")


@voice_cmds.command(name="join", description="Alya joins your voice channel")
async def voice_join(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    member = interaction.guild.get_member(interaction.user.id)
    if not member or not member.voice or not member.voice.channel:
        await interaction.response.send_message("You're not in a voice channel.", ephemeral=True)
        return
    vc_channel = member.voice.channel
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.move_to(vc_channel)
    else:
        await vc_channel.connect()
    await interaction.response.send_message(
        f"Joined **{vc_channel.name}**. Type `!<text>` in this VC's chat and I'll speak it.", ephemeral=True
    )


@voice_cmds.command(name="leave", description="Alya leaves the voice channel")
async def voice_leave(interaction: discord.Interaction):
    if not interaction.guild or not interaction.guild.voice_client:
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        return
    await interaction.guild.voice_client.disconnect()
    await interaction.response.send_message("Left the voice channel.", ephemeral=True)


bot.tree.add_command(voice_cmds)


# =============== /say + /reply (owner proxy) ===============

@bot.tree.command(name="say", description="Send a message as Alya in any channel")
@discord.app_commands.describe(
    message="What Alya should say",
    channel="Channel to send it in (defaults to current channel)",
)
async def say_cmd(interaction: discord.Interaction, message: str, channel: discord.TextChannel | None = None):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Only Awais can use this.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    target = channel or interaction.channel
    await target.send(message)
    await interaction.followup.send(f"✓ Sent in {target.mention}.", ephemeral=True)


@bot.tree.command(name="reply", description="Reply to a message as Alya")
@discord.app_commands.describe(
    message_id="ID of the message to reply to",
    text="What Alya should say",
)
async def reply_cmd(interaction: discord.Interaction, message_id: str, text: str):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Only Awais can use this.", ephemeral=True)
        return
    try:
        msg_id = int(message_id)
    except ValueError:
        await interaction.response.send_message("Invalid message ID.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    target_msg = None
    channels_to_try = [interaction.channel] + [
        c for c in interaction.guild.channels
        if isinstance(c, discord.TextChannel) and c.id != interaction.channel_id
    ]
    for ch in channels_to_try:
        try:
            target_msg = await ch.fetch_message(msg_id)
            break
        except Exception:
            pass
    if not target_msg:
        await interaction.followup.send("Couldn't find that message. Make sure the ID is correct.", ephemeral=True)
        return
    await target_msg.reply(text)
    await interaction.followup.send(f"✓ Replied to {target_msg.author.mention}'s message.", ephemeral=True)


# =============== /announce ===============

@bot.tree.command(name="announce", description="Send a formatted announcement embed as Alya")
@discord.app_commands.describe(
    title="Title of the announcement",
    message="Main content of the announcement",
    channel="Channel to post it in (defaults to current channel)",
    color="Embed color: gold, red, blue, green, purple (default: gold)",
    image_url="Optional image URL to attach at the bottom",
)
async def announce_cmd(
    interaction: discord.Interaction,
    title: str,
    message: str,
    channel: discord.TextChannel | None = None,
    color: str = "gold",
    image_url: str | None = None,
):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Only Awais can use this.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    color_map = {
        "gold": discord.Color.gold(),
        "red": discord.Color.red(),
        "blue": discord.Color.blue(),
        "green": discord.Color.green(),
        "purple": discord.Color.purple(),
    }
    embed_color = color_map.get(color.lower(), discord.Color.gold())
    embed = discord.Embed(title=title, description=message, color=embed_color)
    embed.set_footer(text="— Alya | Storm Tigers")
    if image_url:
        embed.set_image(url=image_url)
    target = channel or interaction.channel
    await target.send(embed=embed)
    await interaction.followup.send(f"✓ Announcement sent in {target.mention}.", ephemeral=True)


# =============== /react ===============

@bot.tree.command(name="react", description="React to a message as Alya")
@discord.app_commands.describe(
    message_id="ID of the message to react to",
    emoji="The emoji to react with",
)
async def react_cmd(interaction: discord.Interaction, message_id: str, emoji: str):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Only Awais can use this.", ephemeral=True)
        return
    try:
        msg_id = int(message_id)
    except ValueError:
        await interaction.response.send_message("Invalid message ID.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    target_msg = None
    channels_to_try = [interaction.channel] + [
        c for c in interaction.guild.channels
        if isinstance(c, discord.TextChannel) and c.id != interaction.channel_id
    ]
    for ch in channels_to_try:
        try:
            target_msg = await ch.fetch_message(msg_id)
            break
        except Exception:
            pass
    if not target_msg:
        await interaction.followup.send("Couldn't find that message.", ephemeral=True)
        return
    try:
        await target_msg.add_reaction(emoji)
        await interaction.followup.send(f"✓ Reacted with {emoji}.", ephemeral=True)
    except discord.HTTPException as e:
        await interaction.followup.send(f"Couldn't react — invalid emoji? (`{e.text}`)", ephemeral=True)


# =============== /loadgifs ===============

@bot.tree.command(name="loadgifs", description="Load welcome GIFs from a specific channel (owner only)")
@discord.app_commands.describe(channel="The channel that contains the welcome GIFs")
async def loadgifs_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Only Awais can do this.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    WELCOME_GIFS.clear()
    loaded = await _fetch_gifs_from_channel(channel)
    await interaction.followup.send(
        f"✓ Loaded **{loaded}/{len(WELCOME_GIF_MSG_IDS)}** GIFs from {channel.mention}.",
        ephemeral=True,
    )


# =============== AI chat (mention or reply) ===============

_handled_messages: deque = deque(maxlen=500)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.id in _handled_messages:
        return
    _handled_messages.append(message.id)
    await bot.process_commands(message)

    # ---- ! TTS ----
    if (
        message.guild
        and message.content.startswith("!")
        and not message.content.startswith("!?")
    ):
        text = message.content[1:].strip()
        voice_client = message.guild.voice_client
        print(f"[tts] msg='{message.content}' vc={voice_client} ch={message.channel.id} vc_ch={voice_client.channel.id if voice_client else None}", flush=True)
        in_vc_chat = (
            voice_client
            and voice_client.is_connected()
            and message.channel.id == voice_client.channel.id
        )
        print(f"[tts] in_vc_chat={in_vc_chat} text='{text}'", flush=True)
        if text and in_vc_chat:
            if voice_client.is_playing():
                voice_client.stop()
            try:
                # Get Alya's AI reply, then speak it
                hist = list(_history[message.author.id])
                reply_text = await ai.reply(
                    user_id=str(message.author.id),
                    user_name=message.author.display_name,
                    message=text,
                    history=hist,
                )
                _history[message.author.id].append({"role": "user", "content": text})
                _history[message.author.id].append({"role": "assistant", "content": reply_text})
                if not reply_text.strip():
                    return
                audio_bytes = await vc.synthesize(reply_text)
                source = discord.FFmpegPCMAudio(
                    io.BytesIO(audio_bytes),
                    pipe=True,
                    before_options="-f mp3",
                )
                voice_client.play(source)
                await message.add_reaction("🔊")
            except Exception as e:
                print(f"[tts] error: {e!r}", flush=True)
                await message.add_reaction("❌")
            return

    if message.content.startswith("?") or message.content.startswith("!"):
        return

    mentioned = bot.user in message.mentions
    is_reply_to_bot = (
        message.reference
        and isinstance(message.reference.resolved, discord.Message)
        and message.reference.resolved.author.id == bot.user.id
    )
    is_dm = message.guild is None

    if not (mentioned or is_reply_to_bot or is_dm):
        return

    if not db.is_chat_enabled(str(message.channel.id)):
        return

    content = message.content
    if bot.user:
        content = content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
    if not content:
        return

    uid = str(message.author.id)

    # ── Natural language role give/remove (Awais + Hamza only) ──
    TRUSTED_IDS = {1238527430575128598, 1296114418677059667}
    if message.author.id in TRUSTED_IDS and message.guild and mentioned:
        _is_give   = bool(re.search(r"\bgive\b", content, re.IGNORECASE))
        _is_remove = bool(re.search(r"\bremove\b", content, re.IGNORECASE))
        if _is_give or _is_remove:
            target_members = [m for m in message.mentions if not m.bot and m.id != bot.user.id]
            if target_members:
                target_member = target_members[0]
                role_to_assign = None
                if message.role_mentions:
                    role_to_assign = message.role_mentions[0]
                else:
                    content_lower = content.lower()
                    for role in message.guild.roles:
                        if role.name.lower() in content_lower:
                            role_to_assign = role
                            break
                if role_to_assign:
                    try:
                        if _is_give:
                            await target_member.add_roles(role_to_assign, reason=f"Assigned via Alya by {message.author}")
                            await message.reply(
                                f"Done~ I've given {target_member.mention} the **{role_to_assign.name}** role.",
                                mention_author=False,
                            )
                            # Welcome is handled automatically by on_member_update
                        else:
                            await target_member.remove_roles(role_to_assign, reason=f"Removed via Alya by {message.author}")
                            await message.reply(
                                f"Done~ I've removed the **{role_to_assign.name}** role from {target_member.mention}.",
                                mention_author=False,
                            )
                    except discord.Forbidden:
                        await message.reply("I don't have permission to change that role.", mention_author=False)
                    except Exception as e:
                        await message.reply(f"Something went wrong: {e}", mention_author=False)
                    return
                # No role found — ignore

    # ── Natural language ID registration ──
    ID_HINT_RE = re.compile(
        r"(?:my\s+)?(?:character\s+|player\s+|game\s+)?id(?:\s+is|:)?\s+([0-9A-Za-z]{6,40})",
        re.IGNORECASE,
    )
    id_match = ID_HINT_RE.search(content)
    if id_match:
        cid = id_match.group(1).strip()
        if is_valid_character_id(cid):
            db.add_accounts(uid, [cid])
            total = len(db.get_accounts(uid))
            await message.reply(
                f"Got it — I've saved `{_mask(cid)}` to your profile. "
                f"You now have **{total}** account(s) linked. "
                f"Share a code with me and I'll redeem it for you.",
                mention_author=False,
            )
            return

    # ── Natural language code redemption ──
    CODE_HINT_RE = re.compile(
        r"(?:here(?:'s|\s+is)?\s+(?:a\s+)?code|redeem\s+(?:this\s+)?(?:code)?|code\s*(?:is|:)?)\s*[:\-]?\s*([0-9A-Za-z_\-]{4,32})",
        re.IGNORECASE,
    )
    code_match = CODE_HINT_RE.search(content)
    if code_match:
        code = code_match.group(1).strip()
        ids = db.get_accounts(uid)
        if not ids:
            await message.reply(
                "Tell me your character ID first and I'll save it — then I can redeem codes for you.",
                mention_author=False,
            )
            return
        await message.reply(f"On it — redeeming `{code}` on your {len(ids)} account(s)...", mention_author=False)
        results = await netmarble.redeem_many(ids, code)
        await message.channel.send(embed=redeem_embed(message.author, code, results))
        return

    # AI chat disabled


# =============== run ===============

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """Send welcome message whenever the ST Member role is newly assigned."""
    had_role = any(r.id == ST_MEMBER_ROLE_ID for r in before.roles)
    has_role = any(r.id == ST_MEMBER_ROLE_ID for r in after.roles)
    if not had_role and has_role:
        guild = after.guild
        welcome_ch = guild.get_channel(WELCOME_CHANNEL_ID)
        target_ch = welcome_ch or guild.system_channel
        if target_ch is None:
            return
        gb_mention = f"<#{GB_GUIDE_CHANNEL_ID}>"
        help_mention = "<#1493503671911317514>"
        msg = (
            f"Everyone welcome {after.mention} to the guild! 🎉\n"
            f"Check {gb_mention} for GB guides and ask for help here or in {help_mention}.\n"
            f"Also check <#1494988490218406008> for hunter guides."
        )
        # Try welcome channel first, then fall back to any writable text channel
        channels_to_try = [target_ch] if target_ch else []
        for tc in guild.text_channels:
            if tc not in channels_to_try:
                channels_to_try.append(tc)
        sent = False
        for tc in channels_to_try:
            perms = tc.permissions_for(guild.me)
            if not (perms.send_messages and perms.embed_links):
                continue
            try:
                embed = discord.Embed(description=msg, color=discord.Color.gold())
                if WELCOME_GIFS:
                    embed.set_image(url=random.choice(WELCOME_GIFS))
                await tc.send(embed=embed)
                print(f"[member_update] welcome sent for {after} in #{tc}", flush=True)
                sent = True
                break
            except Exception as e:
                print(f"[member_update] failed to send in #{tc}: {e}", flush=True)
        if not sent:
            print(f"[member_update] could not find any writable channel for welcome", flush=True)


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set.")
    keepalive.start()
    bot.run(token)


if __name__ == "__main__":
    main()
