import datetime
import discord
import discord.app_commands as app_commands
from discord.ext import commands
from db import add_warning, get_warnings, clear_warnings


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    mod = app_commands.Group(name="mod", description="Server moderation commands")

    @mod.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(member="Member to kick", reason="Reason for kick")
    @app_commands.default_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        try:
            await member.kick(reason=reason)
            embed = discord.Embed(
                description=f"👢 **{member.mention}** has been kicked.\n**Reason:** {reason}",
                color=discord.Color.orange(),
            )
            await interaction.response.send_message(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to kick that member~", ephemeral=True)

    @mod.command(name="ban", description="Ban a member from the server")
    @app_commands.describe(member="Member to ban", reason="Reason for ban")
    @app_commands.default_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        try:
            await member.ban(reason=reason)
            embed = discord.Embed(
                description=f"🔨 **{member.mention}** has been banned.\n**Reason:** {reason}",
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to ban that member~", ephemeral=True)

    @mod.command(name="mute", description="Timeout a member")
    @app_commands.describe(member="Member to mute", minutes="Duration in minutes", reason="Reason")
    @app_commands.default_permissions(moderate_members=True)
    async def mute(self, interaction: discord.Interaction, member: discord.Member, minutes: int = 10, reason: str = "No reason provided"):
        try:
            until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
            await member.timeout(until, reason=reason)
            embed = discord.Embed(
                description=f"🔇 **{member.mention}** muted for **{minutes}m**.\n**Reason:** {reason}",
                color=discord.Color.orange(),
            )
            await interaction.response.send_message(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to timeout that member~", ephemeral=True)

    @mod.command(name="unmute", description="Remove timeout from a member")
    @app_commands.describe(member="Member to unmute")
    @app_commands.default_permissions(moderate_members=True)
    async def unmute(self, interaction: discord.Interaction, member: discord.Member):
        try:
            await member.timeout(None)
            embed = discord.Embed(
                description=f"🔊 **{member.mention}**'s timeout has been removed.",
                color=discord.Color.green(),
            )
            await interaction.response.send_message(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to do that~", ephemeral=True)

    @mod.command(name="purge", description="Delete messages from this channel")
    @app_commands.describe(amount="Number of messages to delete (max 100)")
    @app_commands.default_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, amount: int = 10):
        amount = min(amount, 100)
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"🗑️ Deleted **{len(deleted)}** messages~", ephemeral=True)

    @mod.command(name="warn", description="Warn a member")
    @app_commands.describe(member="Member to warn", reason="Reason for warning")
    @app_commands.default_permissions(manage_messages=True)
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        add_warning(str(interaction.guild_id), str(member.id), reason)
        warns = get_warnings(str(interaction.guild_id), str(member.id))
        embed = discord.Embed(
            title="⚠️ Warning Issued",
            description=f"{member.mention} has been warned.\n**Reason:** {reason}\n**Total warnings:** {len(warns)}",
            color=discord.Color.yellow(),
        )
        await interaction.response.send_message(embed=embed)

    @mod.command(name="warnings", description="View warnings for a member")
    @app_commands.describe(member="Member to check")
    @app_commands.default_permissions(manage_messages=True)
    async def warnings_cmd(self, interaction: discord.Interaction, member: discord.Member):
        warns = get_warnings(str(interaction.guild_id), str(member.id))
        if not warns:
            await interaction.response.send_message(f"{member.mention} has no warnings~ 😇", ephemeral=True)
            return
        embed = discord.Embed(title=f"⚠️ Warnings for {member.display_name}", color=discord.Color.yellow())
        for i, (reason, ts) in enumerate(warns, 1):
            embed.add_field(name=f"#{i} — {ts[:10]}", value=reason, inline=False)
        await interaction.response.send_message(embed=embed)

    @mod.command(name="clearwarns", description="Clear all warnings for a member")
    @app_commands.describe(member="Member to clear warnings for")
    @app_commands.default_permissions(manage_messages=True)
    async def clearwarns(self, interaction: discord.Interaction, member: discord.Member):
        clear_warnings(str(interaction.guild_id), str(member.id))
        await interaction.response.send_message(f"✅ Cleared all warnings for {member.mention}~", ephemeral=True)

    @mod.command(name="lock", description="Lock this channel so members can't send messages")
    @app_commands.default_permissions(manage_channels=True)
    async def lock(self, interaction: discord.Interaction):
        overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = False
        await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
        embed = discord.Embed(description="🔒 This channel has been locked~", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)

    @mod.command(name="unlock", description="Unlock this channel")
    @app_commands.default_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction):
        overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = None
        await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
        embed = discord.Embed(description="🔓 This channel has been unlocked~", color=discord.Color.green())
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
