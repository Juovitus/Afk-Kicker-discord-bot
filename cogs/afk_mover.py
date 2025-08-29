import discord
from discord.ext import commands
import asyncio
import aiosqlite
import os
import logging
from dotenv import load_dotenv

load_dotenv()

ENABLE_WHITELIST = True

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

class AFKMoverCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pending_tasks = {}
        self.db_path = 'social_credit.db'

        # Load timers
        self.base_timer = int(os.getenv('AFK_TIMER_SECONDS', 300))
        self.min_timer = int(os.getenv('AFK_MIN_TIMER_SECONDS', 60))
        self.max_timer = int(os.getenv('AFK_MAX_TIMER_SECONDS', 900))  # new

        # Whitelist
        if ENABLE_WHITELIST:
            self.whitelist = self._parse_whitelist(os.getenv('AFK_WHITELIST_USERS', ''))
        else:
            self.whitelist = []

        self.afk_channel_id = int(os.getenv('AFK_CHANNEL_ID', 0)) or None

    def _parse_whitelist(self, raw):
        whitelist = []
        for entry in raw.split(','):
            entry = entry.strip()
            if entry.isdigit():
                whitelist.append(int(entry))
        return whitelist

    async def get_score(self, user_id: int) -> int:
        """Fetch a user's social credit score asynchronously."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT score FROM scores WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def start_move_timer(self, member: discord.Member):
        """Starts the AFK move timer for a member."""
        score = await self.get_score(member.id)
        timer = self.base_timer + (score * 3)
        timer = max(self.min_timer, min(timer, self.max_timer))  # enforce min/max

        logging.info(f"Starting AFK timer for {member.display_name} ({timer}s, score={score})")

        await asyncio.sleep(timer)

        # Re-check before moving
        if member.voice and member.voice.self_deaf:
            afk_channel = self.get_afk_channel(member.guild)
            if afk_channel and member.voice.channel != afk_channel:
                await member.move_to(afk_channel, reason="Moved to AFK due to self-deafen timeout")
                logging.info(f"Moved {member.display_name} to AFK channel.")
        self.pending_tasks.pop(member.id, None)

    def get_afk_channel(self, guild: discord.Guild):
        """Get the AFK channel from ID or fallback."""
        channel = guild.get_channel(self.afk_channel_id) if self.afk_channel_id else None
        return channel or guild.afk_channel

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Handles starting/stopping AFK timers based on voice state changes."""
        # Ignore bots
        if member.bot:
            return

        # Ignore whitelisted
        if ENABLE_WHITELIST and member.id in self.whitelist:
            return

        # User starts self-deafening
        if (after.self_deaf and not before.self_deaf) and member.voice:
            # Cancel any existing timer
            if member.id in self.pending_tasks:
                self.pending_tasks[member.id].cancel()

            # Start new timer
            task = asyncio.create_task(self.start_move_timer(member))
            self.pending_tasks[member.id] = task

        # User undeafens or leaves VC
        elif (before.self_deaf and not after.self_deaf) or not after.channel:
            if member.id in self.pending_tasks:
                self.pending_tasks[member.id].cancel()
                self.pending_tasks.pop(member.id, None)
                logging.info(f"Cancelled AFK timer for {member.display_name}.")

async def setup(bot):
    await bot.add_cog(AFKMoverCog(bot))
