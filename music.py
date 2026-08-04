import re
import json
import time
import asyncio
import aiohttp
import discord
from discord.ext import commands
import yt_dlp

# ── yt-dlp config ─────────────────────────────────────────────────────────────
# Prefer opus/webm — Discord speaks opus natively so no re-encoding needed,
# which means zero transcoding overhead and the lowest possible latency.
YDL_OPTS = {
    "format": "bestaudio[acodec=opus]/bestaudio[ext=webm]/bestaudio/best",
    "noplaylist": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extractor_retries": 3,
    "socket_timeout": 10,
}

# FFmpeg pipeline tuned for low latency + clear audio:
#   -probesize 200K / -analyzeduration 200K → minimal probe, fast start
#   -reconnect* flags                       → survive stream hiccups
#   -ar 48000 -ac 2                         → Discord's native rate + stereo
#   dynaudnorm                              → single-pass dynamic normalizer
#                                             (loudnorm needs 2 passes → breaks streams)
FFMPEG_BEFORE = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
    "-probesize 200K -analyzeduration 200K"
)
FFMPEG_AFTER = (
    "-vn -sn -dn "
    "-ar 48000 -ac 2 "
    "-af dynaudnorm=f=200:g=5"
)
FFMPEG_OPTS = {
    "before_options": FFMPEG_BEFORE,
    "options": FFMPEG_AFTER,
}

SP_TRACK_RE    = re.compile(r"spotify\.com/track/([A-Za-z0-9]+)")
SP_PLAYLIST_RE = re.compile(r"spotify\.com/playlist/([A-Za-z0-9]+)")
SP_ALBUM_RE    = re.compile(r"spotify\.com/album/([A-Za-z0-9]+)")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


# ── helpers ───────────────────────────────────────────────────────────────────
def fmt_dur(secs: int | None) -> str:
    if secs is None:
        return "?:??"
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


async def fetch_ytdl(query: str, *, loop=None) -> list[dict]:
    """Return a list of track dicts from YouTube search or URL."""
    loop = loop or asyncio.get_event_loop()
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        data = await loop.run_in_executor(
            None, lambda: ydl.extract_info(query, download=False)
        )
    if data is None:
        return []
    entries = [e for e in data["entries"] if e] if "entries" in data else [data]
    return [
        {
            "title": e.get("title", "Unknown"),
            "url": e.get("url") or e.get("webpage_url"),
            "webpage_url": e.get("webpage_url") or e.get("url"),
            "duration": e.get("duration"),
            "thumbnail": e.get("thumbnail"),
            "uploader": e.get("uploader") or e.get("channel", "Unknown"),
        }
        for e in entries
        if e.get("url") or e.get("webpage_url")
    ]


async def spotify_to_searches(url: str) -> list[str]:
    """Convert a Spotify URL → YouTube search strings with NO API key.

    • Track  → uses Spotify's public oembed endpoint (title + artist in og tags)
    • Album/Playlist → scrapes the __NEXT_DATA__ JSON embedded in the public page
    """
    async with aiohttp.ClientSession(headers=_HEADERS) as session:

        # ── single track via oembed ──────────────────────────────────────────
        if SP_TRACK_RE.search(url):
            try:
                oembed = f"https://open.spotify.com/oembed?url={url}"
                async with session.get(oembed, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        title = data.get("title", "")
                        # oembed title is usually "Song - Artist"
                        return [f"{title} audio"] if title else []
            except Exception:
                pass
            return []

        # ── playlist or album → scrape __NEXT_DATA__ ────────────────────────
        if SP_PLAYLIST_RE.search(url) or SP_ALBUM_RE.search(url):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
                    html = await r.text()
                m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', html, re.S)
                if not m:
                    return []
                payload = json.loads(m.group(1))
                # navigate into the nested structure for tracks
                items = []
                # try playlist path
                try:
                    items = payload["props"]["pageProps"]["state"]["data"]["entity"]["trackList"]
                    return [f"{t['title']} {t.get('subtitle', '')} audio".strip() for t in items if t.get("title")]
                except (KeyError, TypeError):
                    pass
                # try album path
                try:
                    tracks = payload["props"]["pageProps"]["state"]["data"]["entity"]["tracks"]["items"]
                    return [
                        f"{t['track']['name']} {t['track']['artists'][0]['name']} audio"
                        for t in tracks if t.get("track")
                    ]
                except (KeyError, TypeError):
                    pass
            except Exception:
                pass
            return []

    return []


# ── per-guild state ───────────────────────────────────────────────────────────
class GuildPlayer:
    def __init__(self):
        self.queue:        list[dict] = []
        self.current:      dict | None = None
        self.vc:           discord.VoiceClient | None = None
        self.loop_track:   bool = False
        self.loop_queue:   bool = False
        self.text_channel: discord.TextChannel | None = None
        self._task:        asyncio.Task | None = None
        self.started_at:   float | None = None  # monotonic time when current track started

    def remaining_secs(self) -> float | None:
        """Seconds left in the current track, or None if unknown."""
        if not self.current or self.started_at is None:
            return None
        dur = self.current.get("duration")
        if not dur:
            return None
        elapsed = time.monotonic() - self.started_at
        return max(0.0, dur - elapsed)

    def eta_for_index(self, idx: int) -> float | None:
        """Seconds until queue[idx] will start playing (0-based)."""
        rem = self.remaining_secs()
        if rem is None:
            return None
        total = rem
        for i in range(idx):
            d = self.queue[i].get("duration")
            if d is None:
                return None
            total += d
        return total

    def reset(self):
        self.queue.clear()
        self.current = None
        self.loop_track = False
        self.loop_queue = False
        self.started_at = None


# ── cog ───────────────────────────────────────────────────────────────────────
class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._players: dict[int, GuildPlayer] = {}

    def get_player(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self._players:
            self._players[guild_id] = GuildPlayer()
        return self._players[guild_id]

    # ── internal playback loop ────────────────────────────────────────────────
    async def _play_next(self, guild_id: int):
        player = self.get_player(guild_id)
        vc = player.vc

        if not vc or not vc.is_connected():
            return

        # if loop_track, replay current
        if player.loop_track and player.current:
            track = player.current
        elif player.queue:
            track = player.queue.pop(0)
            # if loop_queue put it at back
            if player.loop_queue and player.current:
                player.queue.append(player.current)
            player.current = track
        else:
            if player.loop_queue and player.current:
                player.queue.append(player.current)
                player.current = None
                if player.queue:
                    await self._play_next(guild_id)
                    return
            player.current = None
            if player.text_channel:
                await player.text_channel.send("Queue finished~ 🎵 See you next time!")
            return

        try:
            source = discord.FFmpegPCMAudio(track["url"], **FFMPEG_OPTS)
            source = discord.PCMVolumeTransformer(source, volume=0.7)

            def after(err):
                asyncio.run_coroutine_threadsafe(self._play_next(guild_id), self.bot.loop)

            player.started_at = time.monotonic()
            vc.play(source, after=after)

            if player.text_channel:
                embed = discord.Embed(
                    title="🎵 Now Playing~",
                    description=f"**[{track['title']}]({track['webpage_url']})**",
                    color=discord.Color.blurple(),
                )
                embed.add_field(name="Duration", value=fmt_dur(track["duration"]), inline=True)
                embed.add_field(name="Uploader", value=track.get("uploader", "Unknown"), inline=True)
                loops = []
                if player.loop_track:
                    loops.append("🔂 Track")
                if player.loop_queue:
                    loops.append("🔁 Queue")
                if loops:
                    embed.add_field(name="Loop", value=" | ".join(loops), inline=True)
                if thumb := track.get("thumbnail"):
                    embed.set_thumbnail(url=thumb)
                await player.text_channel.send(embed=embed)
        except Exception as e:
            if player.text_channel:
                await player.text_channel.send(f"Couldn't play that track~ ({e})")
            await self._play_next(guild_id)

    # ── join helper ───────────────────────────────────────────────────────────
    async def _ensure_vc(self, ctx: commands.Context) -> bool:
        player = self.get_player(ctx.guild.id)
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.reply("Join a voice channel first~ 🎙️")
            return False
        vc_channel = ctx.author.voice.channel
        if player.vc and player.vc.is_connected():
            if player.vc.channel != vc_channel:
                await player.vc.move_to(vc_channel)
        else:
            player.vc = await vc_channel.connect()
        player.text_channel = ctx.channel
        return True

    # ── a!play ────────────────────────────────────────────────────────────────
    @commands.command(name="play", aliases=["p"])
    async def play(self, ctx: commands.Context, *, query: str):
        query = query.lstrip("@ ")
        if not await self._ensure_vc(ctx):
            return

        player = self.get_player(ctx.guild.id)
        async with ctx.typing():
            searches: list[str] = []

            # Spotify URL — no API key needed, uses public oembed + page scraping
            if "spotify.com" in query:
                await ctx.reply("🔍 Looking up that Spotify link~", delete_after=5)
                searches = await spotify_to_searches(query)
                if not searches:
                    await ctx.reply(
                        "Couldn't grab that from Spotify~ Try pasting the song name directly! 🎵"
                    )
                    return
            else:
                # YouTube URL or plain search query
                searches = [query]

            added = 0
            for search in searches:
                try:
                    tracks = await fetch_ytdl(search, loop=self.bot.loop)
                    player.queue.extend(tracks)
                    added += len(tracks)
                except Exception as e:
                    await ctx.reply(f"Couldn't load `{search[:60]}` — {e}")

            if added == 0:
                await ctx.reply("Nothing found~ Try a different search term! 🔍")
                return

            if added == 1:
                if player.vc.is_playing() or player.vc.is_paused():
                    t = player.queue[-1]
                    pos = len(player.queue)
                    eta = player.eta_for_index(pos - 1)
                    eta_str = f" • plays in ~{fmt_dur(int(eta))}" if eta is not None else ""
                    await ctx.reply(
                        f"Added to queue~ 🎵 **{t['title']}** "
                        f"(#{pos}{eta_str})"
                    )
                # else the now-playing embed will fire
            else:
                # show cumulative wait for the first newly added track
                first_new_idx = len(player.queue) - added
                eta = player.eta_for_index(first_new_idx)
                eta_str = f" • first track plays in ~{fmt_dur(int(eta))}" if eta is not None else ""
                await ctx.reply(f"Added **{added}** tracks to the queue~{eta_str} 🎵")

            if not player.vc.is_playing() and not player.vc.is_paused():
                await self._play_next(ctx.guild.id)

    # ── a!skip ────────────────────────────────────────────────────────────────
    @commands.command(name="skip", aliases=["s", "next"])
    async def skip(self, ctx: commands.Context):
        player = self.get_player(ctx.guild.id)
        if not player.vc or not player.vc.is_playing():
            await ctx.reply("Nothing is playing~ 🎵")
            return
        was = player.current
        player.loop_track = False
        player.vc.stop()
        await ctx.reply(f"Skipped **{was['title'] if was else 'track'}**~ ⏭️")

    # ── a!stop ────────────────────────────────────────────────────────────────
    @commands.command(name="stop", aliases=["leave", "dc"])
    async def stop(self, ctx: commands.Context):
        player = self.get_player(ctx.guild.id)
        if player.vc and player.vc.is_connected():
            player.reset()
            await player.vc.disconnect()
            player.vc = None
            await ctx.reply("Disconnected and cleared the queue~ 👋")
        else:
            await ctx.reply("I'm not in a voice channel~ 🎵")

    # ── a!pause / a!resume ────────────────────────────────────────────────────
    @commands.command(name="pause")
    async def pause(self, ctx: commands.Context):
        player = self.get_player(ctx.guild.id)
        if player.vc and player.vc.is_playing():
            player.vc.pause()
            await ctx.reply("Paused~ ⏸️")
        else:
            await ctx.reply("Nothing is playing~ 🎵")

    @commands.command(name="resume", aliases=["unpause"])
    async def resume(self, ctx: commands.Context):
        player = self.get_player(ctx.guild.id)
        if player.vc and player.vc.is_paused():
            player.vc.resume()
            await ctx.reply("Resumed~ ▶️")
        else:
            await ctx.reply("I'm not paused~ 🎵")

    # ── a!loop / a!repeat ─────────────────────────────────────────────────────
    @commands.command(name="loop", aliases=["l"])
    async def loop(self, ctx: commands.Context, mode: str = "track"):
        player = self.get_player(ctx.guild.id)
        mode = mode.lower()
        if mode in ("queue", "q", "all"):
            player.loop_queue = not player.loop_queue
            state = "enabled 🔁" if player.loop_queue else "disabled"
            await ctx.reply(f"Queue loop {state}~")
        else:
            player.loop_track = not player.loop_track
            state = "enabled 🔂" if player.loop_track else "disabled"
            await ctx.reply(f"Track loop {state}~")

    @commands.command(name="repeat", aliases=["r"])
    async def repeat(self, ctx: commands.Context):
        player = self.get_player(ctx.guild.id)
        player.loop_track = not player.loop_track
        state = "enabled 🔂" if player.loop_track else "disabled"
        await ctx.reply(f"Track repeat {state}~")

    # ── a!queue ───────────────────────────────────────────────────────────────
    @commands.command(name="queue", aliases=["q", "list"])
    async def queue(self, ctx: commands.Context):
        player = self.get_player(ctx.guild.id)
        embed = discord.Embed(title="🎵 Music Queue~", color=discord.Color.blurple())

        if player.current:
            loop_flag = " 🔂" if player.loop_track else ""
            rem = player.remaining_secs()
            rem_str = f" — {fmt_dur(int(rem))} left" if rem is not None else f" — {fmt_dur(player.current.get('duration'))}"
            embed.add_field(
                name=f"▶️ Now Playing{loop_flag}",
                value=f"**[{player.current['title']}]({player.current['webpage_url']})**{rem_str}",
                inline=False,
            )

        if player.queue:
            lines = []
            for i, t in enumerate(player.queue[:15]):
                eta = player.eta_for_index(i)
                eta_str = f" • plays in ~{fmt_dur(int(eta))}" if eta is not None else ""
                lines.append(
                    f"`{i+1}.` [{t['title']}]({t['webpage_url']}) "
                    f"— {fmt_dur(t.get('duration'))}{eta_str}"
                )
            if len(player.queue) > 15:
                lines.append(f"*…and {len(player.queue) - 15} more*")
            embed.add_field(name="📋 Up Next", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="📋 Up Next", value="Queue is empty~", inline=False)

        total = sum(t.get("duration") or 0 for t in player.queue)
        loops = []
        if player.loop_track:
            loops.append("🔂 Track")
        if player.loop_queue:
            loops.append("🔁 Queue")
        embed.set_footer(
            text=f"Total: {len(player.queue)} tracks • {fmt_dur(total)}"
                 + (f" • Loop: {' | '.join(loops)}" if loops else "")
        )
        await ctx.reply(embed=embed)

    # ── a!np ──────────────────────────────────────────────────────────────────
    @commands.command(name="nowplaying", aliases=["np", "now"])
    async def nowplaying(self, ctx: commands.Context):
        player = self.get_player(ctx.guild.id)
        if not player.current:
            await ctx.reply("Nothing is playing right now~ 🎵")
            return
        t = player.current
        embed = discord.Embed(
            title="🎵 Now Playing~",
            description=f"**[{t['title']}]({t['webpage_url']})**",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Duration", value=fmt_dur(t["duration"]), inline=True)
        embed.add_field(name="Uploader", value=t.get("uploader", "Unknown"), inline=True)
        loops = []
        if player.loop_track:
            loops.append("🔂 Track")
        if player.loop_queue:
            loops.append("🔁 Queue")
        if loops:
            embed.add_field(name="Loop", value=" | ".join(loops), inline=True)
        if thumb := t.get("thumbnail"):
            embed.set_thumbnail(url=thumb)
        await ctx.reply(embed=embed)

    # ── a!remove ──────────────────────────────────────────────────────────────
    @commands.command(name="remove", aliases=["rm"])
    async def remove(self, ctx: commands.Context, index: int):
        player = self.get_player(ctx.guild.id)
        if not player.queue:
            await ctx.reply("Queue is empty~ 🎵")
            return
        if index < 1 or index > len(player.queue):
            await ctx.reply(f"Invalid index — queue has {len(player.queue)} tracks~")
            return
        removed = player.queue.pop(index - 1)
        await ctx.reply(f"Removed **{removed['title']}** from the queue~ 🗑️")

    # ── a!clear ───────────────────────────────────────────────────────────────
    @commands.command(name="clear", aliases=["clearqueue", "cq"])
    async def clear(self, ctx: commands.Context):
        player = self.get_player(ctx.guild.id)
        count = len(player.queue)
        player.queue.clear()
        await ctx.reply(f"Cleared {count} track{'s' if count != 1 else ''} from the queue~ 🗑️")

    # ── a!volume ──────────────────────────────────────────────────────────────
    @commands.command(name="volume", aliases=["vol", "v"])
    async def volume(self, ctx: commands.Context, vol: int):
        player = self.get_player(ctx.guild.id)
        if not player.vc or not player.vc.source:
            await ctx.reply("Nothing is playing~ 🎵")
            return
        if not 0 < vol <= 150:
            await ctx.reply("Volume must be between 1 and 150~")
            return
        player.vc.source.volume = vol / 100
        await ctx.reply(f"Volume set to **{vol}%**~ 🔊")

    # ── a!shuffle ─────────────────────────────────────────────────────────────
    @commands.command(name="shuffle")
    async def shuffle(self, ctx: commands.Context):
        import random
        player = self.get_player(ctx.guild.id)
        if len(player.queue) < 2:
            await ctx.reply("Not enough tracks to shuffle~ 🎵")
            return
        random.shuffle(player.queue)
        await ctx.reply(f"Shuffled {len(player.queue)} tracks~ 🔀")

    # ── a!music (help) ────────────────────────────────────────────────────────
    @commands.command(name="music", aliases=["mhelp", "musichelp"])
    async def music_help(self, ctx: commands.Context):
        embed = discord.Embed(
            title="🎵 Music Commands~",
            description="Prefix: `a!`",
            color=discord.Color.blurple(),
        )
        cmds = [
            ("`a!play <song/URL>`",      "Play from YouTube, Spotify track/playlist/album, or search"),
            ("`a!skip` / `a!next`",      "Skip the current track"),
            ("`a!stop` / `a!leave`",     "Stop music and disconnect"),
            ("`a!pause` / `a!resume`",   "Pause or resume playback"),
            ("`a!loop [track|queue]`",   "Loop current track or entire queue"),
            ("`a!repeat`",               "Toggle repeat for current track"),
            ("`a!queue`",                "Show the current queue"),
            ("`a!nowplaying`",           "Show what's playing now"),
            ("`a!remove <#>`",           "Remove a track by queue position"),
            ("`a!clear`",                "Clear the entire queue"),
            ("`a!shuffle`",              "Shuffle the queue"),
            ("`a!volume <1-150>`",       "Adjust playback volume"),
        ]
        for name, desc in cmds:
            embed.add_field(name=name, value=desc, inline=False)
        embed.set_footer(text="Spotify links work automatically — no setup needed~ 🎵")
        await ctx.reply(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
