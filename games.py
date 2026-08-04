import re
import random
import asyncio
import html
import aiohttp
import discord
import discord.app_commands as app_commands
from discord.ext import commands

_active_guess: dict[int, int] = {}


def parse_duration(raw: str) -> int | None:
    """Parse a time string like '30s', '5m', '1h', '2h30m' into total seconds.
    Returns None if the format is unrecognisable.
    Max: 48h (172800s). Min: 10s."""
    raw = raw.strip().lower()
    # plain number → treat as minutes
    if raw.isdigit():
        return int(raw) * 60
    pattern = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", raw)
    if not pattern or not pattern.group(0):
        return None
    h = int(pattern.group(1) or 0)
    m = int(pattern.group(2) or 0)
    s = int(pattern.group(3) or 0)
    total = h * 3600 + m * 60 + s
    if total < 10 or total > 172800:
        return None
    return total


def fmt_duration(secs: int) -> str:
    """Return a human-friendly duration string."""
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s:
        parts.append(f"{s}s")
    return " ".join(parts) or "0s"

TRUTHS = [
    "What's the most embarrassing thing you've done in a game?",
    "Have you ever rage quit a game? Which one?",
    "Who in this server do you think would win in a 1v1?",
    "What's your biggest gaming weakness?",
    "Have you ever cheated in an online game?",
    "What game do you pretend to be good at but secretly struggle with?",
    "Who's the most annoying person you've played with?",
    "What's the longest you've stayed up gaming?",
    "Have you ever cried over a game?",
    "What's a game everyone likes that you secretly hate?",
]

DARES = [
    "Send your current screen time for today.",
    "Type with your eyes closed for the next 3 messages.",
    "Speak in rhymes for the next 5 minutes in chat.",
    "Change your nickname to 'Alya's Servant' for 10 minutes.",
    "Send the last meme you saved.",
    "Tell us your most embarrassing username you've ever had.",
    "DM a random server member 'you're awesome' right now.",
    "React to the last 5 messages with a random emoji.",
    "Send a voice message saying 'Alya is the best bot ever'.",
    "Post your top 3 most played games.",
]

EIGHTBALL = [
    "Obviously yes~ Did you even need to ask? 😏",
    "Ara ara~ The stars say yes.",
    "Mm... yes, I believe so~ ✨",
    "Without a doubt. Trust me on this one.",
    "My instincts as an S-Rank hunter say yes.",
    "Mn. Likely.",
    "The odds are in your favor~ 💫",
    "It seems so. For now.",
    "...I'm not sure. Ask me again~",
    "Even I cannot see this one clearly.",
    "Hmph. Don't rush me.",
    "The answer is clouded. Be patient.",
    "I wouldn't count on it~ 😒",
    "No. Absolutely not.",
    "That's... not going to happen.",
    "Unlikely. Don't get your hopes up.",
    "My read on this? Not good.",
    "Hmm... very doubtful. Even for you.",
]


LABELS = ["🅐", "🅑", "🅒", "🅓"]
LABEL_STYLES = [
    discord.ButtonStyle.primary,
    discord.ButtonStyle.primary,
    discord.ButtonStyle.primary,
    discord.ButtonStyle.primary,
]

POLL_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
POLL_STYLES = [
    discord.ButtonStyle.primary,
    discord.ButtonStyle.success,
    discord.ButtonStyle.danger,
    discord.ButtonStyle.secondary,
]


class PollView(discord.ui.View):
    def __init__(self, question: str, options: list[str], author_name: str,
                 duration_secs: int | None = None):
        super().__init__(timeout=float(duration_secs) if duration_secs else None)
        self.question = question
        self.options = options
        self.author_name = author_name
        self.duration_secs = duration_secs
        self.votes: dict[int, int] = {}
        self.voter_names: dict[int, str] = {}
        self.message: discord.Message | None = None
        self._uid = id(self)

        for i, opt in enumerate(options):
            btn = discord.ui.Button(
                label=opt[:80],
                style=POLL_STYLES[i % len(POLL_STYLES)],
                custom_id=f"poll_{i}_{self._uid}",
                emoji=POLL_EMOJIS[i],
            )
            btn.callback = self._make_callback(i)
            self.add_item(btn)

        see_votes_btn = discord.ui.Button(
            label="See Votes",
            style=discord.ButtonStyle.secondary,
            custom_id=f"poll_see_{self._uid}",
            emoji="👥",
            row=1,
        )
        see_votes_btn.callback = self._see_votes
        self.add_item(see_votes_btn)

    def _make_callback(self, idx: int):
        async def callback(interaction: discord.Interaction):
            uid = interaction.user.id
            old = self.votes.get(uid)
            if old == idx:
                del self.votes[uid]
                self.voter_names.pop(uid, None)
                note = "Removed your vote~ ✅"
            else:
                self.votes[uid] = idx
                self.voter_names[uid] = interaction.user.display_name
                if old is None:
                    note = f"Voted for **{self.options[idx]}**~ 🗳️"
                else:
                    note = f"Changed vote to **{self.options[idx]}**~ 🗳️"
            await interaction.response.edit_message(embed=self._build_embed())
            await interaction.followup.send(note, ephemeral=True)
        return callback

    async def _see_votes(self, interaction: discord.Interaction):
        member = interaction.user
        is_mod = (
            isinstance(member, discord.Member)
            and (
                member.guild_permissions.manage_messages
                or member.guild_permissions.administrator
            )
        )
        if not is_mod:
            await interaction.response.send_message(
                "Only mods can see who voted~ 🔒", ephemeral=True
            )
            return
        if not self.votes:
            await interaction.response.send_message("No votes yet~ 🗳️", ephemeral=True)
            return
        lines = []
        for i, opt in enumerate(self.options):
            voters = [self.voter_names[uid] for uid, v in self.votes.items() if v == i]
            if voters:
                lines.append(f"**{POLL_EMOJIS[i]} {opt}**\n" + "\n".join(f"• {n}" for n in voters))
        await interaction.response.send_message("\n\n".join(lines) or "No votes yet~", ephemeral=True)

    def _build_embed(self, ended: bool = False) -> discord.Embed:
        total = len(self.votes)
        color = discord.Color.blurple() if not ended else discord.Color.dark_grey()
        title = f"{'🔒' if ended else '📊'} {self.question}"
        embed = discord.Embed(title=title, color=color)
        for i, opt in enumerate(self.options):
            count = sum(1 for v in self.votes.values() if v == i)
            pct = (count / total * 100) if total > 0 else 0
            filled = round(pct / 10)
            bar = "█" * filled + "░" * (10 - filled)
            embed.add_field(
                name=f"{POLL_EMOJIS[i]} {opt}",
                value=f"`{bar}` **{count}** vote{'s' if count != 1 else ''} ({pct:.0f}%)",
                inline=False,
            )
        timer_note = ""
        if self.duration_secs and not ended:
            timer_note = f" • Ends in {self.duration_secs // 60}m"
        suffix = "Poll ended~" if ended else f"Poll by {self.author_name}{timer_note} • Click again to unvote"
        embed.set_footer(text=f"Total votes: {total} • {suffix}")
        return embed

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            if self.message:
                await self.message.edit(embed=self._build_embed(ended=True), view=self)
        except Exception:
            pass


class TriviaView(discord.ui.View):
    def __init__(self, options: list[str], correct_idx: int, question: str, category: str, difficulty: str):
        super().__init__(timeout=30)
        self.options = options
        self.correct_idx = correct_idx
        self.question = question
        self.category = category
        self.difficulty = difficulty
        self.answered: set[int] = set()

        for i, opt in enumerate(options):
            btn = discord.ui.Button(
                label=f"{LABELS[i]}  {opt[:60]}",
                style=LABEL_STYLES[i],
                custom_id=f"trivia_{i}",
            )
            btn.callback = self._make_callback(i)
            self.add_item(btn)

    def _make_callback(self, idx: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id in self.answered:
                await interaction.response.send_message("You already answered~", ephemeral=True)
                return
            self.answered.add(interaction.user.id)

            is_correct = (idx == self.correct_idx)
            correct_answer = self.options[self.correct_idx]

            if is_correct:
                result_text = f"✅ **{LABELS[idx]} {correct_answer}** — Correct! Well done~"
                color = discord.Color.green()
            else:
                result_text = (
                    f"❌ **{LABELS[idx]} {self.options[idx]}** — Wrong!\n"
                    f"The answer was **{LABELS[self.correct_idx]} {correct_answer}**~"
                )
                color = discord.Color.red()

            embed = discord.Embed(
                title=f"Trivia — {self.category}",
                description=f"**{self.question}**\n\n{result_text}",
                color=color,
            )
            embed.set_footer(text=f"Difficulty: {self.difficulty} • Answered by {interaction.user.display_name}")

            for item in self.children:
                item.disabled = True
                if hasattr(item, "custom_id"):
                    try:
                        btn_idx = int(item.custom_id.split("_")[1])
                        if btn_idx == self.correct_idx:
                            item.style = discord.ButtonStyle.success
                        elif btn_idx == idx and not is_correct:
                            item.style = discord.ButtonStyle.danger
                        else:
                            item.style = discord.ButtonStyle.secondary
                    except Exception:
                        pass

            await interaction.response.edit_message(embed=embed, view=self)

        return callback

    async def on_timeout(self):
        correct_answer = self.options[self.correct_idx]
        for item in self.children:
            item.disabled = True
            if hasattr(item, "custom_id"):
                try:
                    btn_idx = int(item.custom_id.split("_")[1])
                    if btn_idx == self.correct_idx:
                        item.style = discord.ButtonStyle.success
                    else:
                        item.style = discord.ButtonStyle.secondary
                except Exception:
                    pass
        try:
            await self.message.edit(
                embed=discord.Embed(
                    title=f"Trivia — {self.category}",
                    description=f"**{self.question}**\n\n⏰ Time's up! The answer was **{LABELS[self.correct_idx]} {correct_answer}**~",
                    color=discord.Color.orange(),
                ),
                view=self,
            )
        except Exception:
            pass


class Games(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="rps", description="Play Rock Paper Scissors against Alya")
    @app_commands.describe(choice="Your move")
    @app_commands.choices(choice=[
        app_commands.Choice(name="🪨 Rock", value="rock"),
        app_commands.Choice(name="📄 Paper", value="paper"),
        app_commands.Choice(name="✂️ Scissors", value="scissors"),
    ])
    async def rps(self, interaction: discord.Interaction, choice: str):
        moves = ["rock", "paper", "scissors"]
        emojis = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
        alya_pick = random.choice(moves)
        wins = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
        if choice == alya_pick:
            result = "It's a tie~ 😶"
            color = discord.Color.yellow()
        elif wins[choice] == alya_pick:
            result = "You win... don't get cocky. 😒"
            color = discord.Color.green()
        else:
            result = "Ahahaha~ I win! As expected. 😏"
            color = discord.Color.red()
        embed = discord.Embed(title="Rock Paper Scissors", color=color)
        embed.add_field(name="You", value=emojis[choice], inline=True)
        embed.add_field(name="Alya", value=emojis[alya_pick], inline=True)
        embed.add_field(name="Result", value=result, inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="guess", description="Start a guess-the-number game (1–100)")
    async def guess_start(self, interaction: discord.Interaction):
        if interaction.channel_id in _active_guess:
            await interaction.response.send_message("A game is already running in this channel~ Type a number!", ephemeral=True)
            return
        number = random.randint(1, 100)
        _active_guess[interaction.channel_id] = number
        embed = discord.Embed(
            title="Guess the Number~",
            description="I'm thinking of a number between **1 and 100**.\nType your guess in chat!",
            color=discord.Color.purple(),
        )
        embed.set_footer(text="Type a number to guess • Type 'stop' to end the game")
        await interaction.response.send_message(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.channel.id not in _active_guess:
            return
        content = message.content.strip().lower()
        if content.startswith(("?", "!", "/")):
            return
        if content == "stop":
            answer = _active_guess.pop(message.channel.id)
            await message.reply(f"Game ended~ The number was **{answer}**.", mention_author=False)
            return
        try:
            guess = int(content)
        except ValueError:
            return
        answer = _active_guess[message.channel.id]
        if guess == answer:
            _active_guess.pop(message.channel.id)
            await message.reply(
                f"🎉 **{message.author.display_name}** got it! The number was **{answer}**~ Well done!",
                mention_author=False,
            )
        elif guess < answer:
            await message.reply("Too low~ Try higher! 📈", mention_author=False)
        else:
            await message.reply("Too high~ Try lower! 📉", mention_author=False)

    @app_commands.command(name="8ball", description="Ask Alya the magic 8-ball")
    @app_commands.describe(question="Your question")
    async def eightball(self, interaction: discord.Interaction, question: str):
        answer = random.choice(EIGHTBALL)
        embed = discord.Embed(color=discord.Color.dark_purple())
        embed.add_field(name="🎱 Question", value=question, inline=False)
        embed.add_field(name="Alya says~", value=f"*{answer}*", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="poll", description="Create a poll with live vote counts")
    @app_commands.describe(
        question="The poll question",
        options="Options separated by commas — 2 to 4 options (e.g. Yes, No, Maybe)",
        duration="How long the poll runs in minutes (optional — leave empty for no time limit)",
    )
    async def poll(self, interaction: discord.Interaction, question: str, options: str,
                   duration: int | None = None):
        opts = [o.strip() for o in options.split(",") if o.strip()]
        if len(opts) < 2:
            await interaction.response.send_message(
                "Give me at least 2 options separated by commas~", ephemeral=True
            )
            return
        opts = opts[:4]
        if duration is not None and duration < 1:
            await interaction.response.send_message("Duration must be at least 1 minute~", ephemeral=True)
            return
        author_name = interaction.user.display_name
        duration_secs = duration * 60 if duration else None
        view = PollView(question, opts, author_name, duration_secs=duration_secs)
        await interaction.response.send_message(embed=view._build_embed(), view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="truthordare", description="Get a truth or dare from Alya")
    @app_commands.describe(choice="Pick truth or dare")
    @app_commands.choices(choice=[
        app_commands.Choice(name="Truth", value="truth"),
        app_commands.Choice(name="Dare", value="dare"),
    ])
    async def tod(self, interaction: discord.Interaction, choice: str):
        if choice == "truth":
            text = random.choice(TRUTHS)
            title = "🔍 Truth~"
            color = discord.Color.blue()
        else:
            text = random.choice(DARES)
            title = "🔥 Dare~"
            color = discord.Color.red()
        embed = discord.Embed(title=title, description=text, color=color)
        embed.set_footer(text=f"Asked by {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="reminder", description="Alya will remind you (or a whole role) after a set time")
    @app_commands.describe(
        time='When to remind — e.g. 30m, 1h, 2h30m, 45s',
        message="What to remind about",
        where="Send the reminder as a DM or ping in this channel",
        role="Ping a specific role instead (mods only)",
    )
    @app_commands.choices(where=[
        app_commands.Choice(name="DM me", value="dm"),
        app_commands.Choice(name="Ping here", value="here"),
    ])
    async def reminder(self, interaction: discord.Interaction, time: str, message: str,
                       where: str = "dm", role: discord.Role | None = None):
        secs = parse_duration(time)
        if secs is None:
            await interaction.response.send_message(
                "I couldn't understand that time~ Try something like `30m`, `1h`, `2h30m`, or `45s`. "
                "Minimum is 10 seconds, maximum is 48 hours~",
                ephemeral=True,
            )
            return

        # role ping requires mod permissions
        if role is not None:
            member = interaction.user
            is_mod = (
                isinstance(member, discord.Member)
                and (member.guild_permissions.manage_messages or member.guild_permissions.administrator)
            )
            if not is_mod:
                await interaction.response.send_message(
                    "Only mods can set reminders for a role~ 🔒", ephemeral=True
                )
                return

        friendly = fmt_duration(secs)
        if role:
            via_label = f"Ping @{role.name}"
        elif where == "dm":
            via_label = "DM"
        else:
            via_label = "Ping here"

        confirm_embed = discord.Embed(
            title="⏰ Reminder set~",
            description=f"I'll remind about: **{message}**",
            color=discord.Color.teal(),
        )
        confirm_embed.add_field(name="In", value=friendly, inline=True)
        confirm_embed.add_field(name="Via", value=via_label, inline=True)
        if role:
            confirm_embed.add_field(name="Role", value=role.mention, inline=True)
        await interaction.response.send_message(embed=confirm_embed, ephemeral=True)

        channel = interaction.channel
        user = interaction.user

        async def fire():
            await asyncio.sleep(secs)
            remind_embed = discord.Embed(
                title="⏰ Reminder~",
                description=message,
                color=discord.Color.teal(),
            )
            if role:
                remind_embed.set_footer(text=f"Reminder set by {user.display_name} • {friendly} ago~")
            else:
                remind_embed.set_footer(text=f"You asked me to remind you {friendly} ago~")

            try:
                if role:
                    if channel:
                        await channel.send(content=role.mention, embed=remind_embed)
                elif where == "dm":
                    await user.send(embed=remind_embed)
                else:
                    if channel:
                        await channel.send(content=user.mention, embed=remind_embed)
            except discord.Forbidden:
                try:
                    if channel:
                        await channel.send(
                            content=f"{user.mention} (couldn't DM you~)",
                            embed=remind_embed,
                        )
                except Exception:
                    pass

        asyncio.create_task(fire())

    @app_commands.command(name="trivia", description="Answer a trivia question")
    @app_commands.describe(category="Question category")
    @app_commands.choices(category=[
        app_commands.Choice(name="General", value="9"),
        app_commands.Choice(name="Anime & Manga", value="31"),
        app_commands.Choice(name="Video Games", value="15"),
        app_commands.Choice(name="Sports", value="21"),
    ])
    async def trivia(self, interaction: discord.Interaction, category: str = "9"):
        await interaction.response.defer()
        try:
            url = f"https://opentdb.com/api.php?amount=1&category={category}&type=multiple"
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    data = await r.json()
            q = data["results"][0]
            question = html.unescape(q["question"])
            correct = html.unescape(q["correct_answer"])
            wrong = [html.unescape(a) for a in q["incorrect_answers"]]
            options = wrong + [correct]
            random.shuffle(options)
            correct_idx = options.index(correct)
            difficulty = q["difficulty"].capitalize()
            category_name = q["category"]

            view = TriviaView(options, correct_idx, question, category_name, difficulty)
            desc = f"**{question}**"
            embed = discord.Embed(title=f"Trivia — {category_name}", description=desc, color=discord.Color.gold())
            embed.set_footer(text=f"Difficulty: {difficulty} • Click a button to answer!")
            await interaction.followup.send(embed=embed, view=view)
        except Exception:
            await interaction.followup.send("Couldn't fetch a question right now, try again~", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Games(bot))


def get_active_guess() -> dict:
    return _active_guess
