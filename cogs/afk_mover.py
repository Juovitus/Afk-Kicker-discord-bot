import discord
from discord.ext import commands
import asyncio
import sqlite3
import os
from dotenv import load_dotenv

load_dotenv()
ENABLE_WHITELIST = True
class AFKMoverCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pending_tasks = {}  # Dict to track pending move tasks: user_id -> task
        self.db_path = 'social_credit.db'
        self.base_timer = int(os.getenv('AFK_TIMER_SECONDS', 5))
        self.min_timer = 2
        if ENABLE_WHITELIST :
            self.whitelist = [int(id.strip()) for id in os.getenv('AFK_WHITELIST_USERS', '').split(',') if id.strip()]
        else: self.whitelist = []
        self.afk_channel_id = os.getenv('AFK_CHANNEL_ID')
        self.afk_channel_id = int(self.afk_channel_id) if self.afk_channel_id else None
        print(f"AFK Mover initialized: base_timer={self.base_timer}, min_timer={self.min_timer}, whitelist={self.whitelist}, afk_channel_id={self.afk_channel_id}")

    def get_score(self, user_id):
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('SELECT score FROM scores WHERE user_id = ?', (user_id,))
            result = c.fetchone()
            conn.close()
            score = result[0] if result else 0
            #print(f"Fetched score for user {user_id}: {score}")
            return score
        except Exception as e:
            print(f"Error fetching score for user {user_id}: {e}")
            return 0

    def calculate_timer(self, user_id):
        if user_id in self.whitelist:
            #print(f"User {user_id} is whitelisted, skipping move")
            return float('inf')  # Infinite timer for whitelisted users
        score = self.get_score(user_id)
        if score < 0:
            reduction = abs(score) // 10
            timer = self.base_timer - reduction
            timer = max(timer, self.min_timer)
            #print(f"User {user_id} has negative score {score}, timer reduced to {timer} seconds")
            return timer
        elif score > 0:
            increase = (score // 10) * 30
            timer = self.base_timer + increase
            #print(f"User {user_id} has positive score {score}, timer increased to {timer} seconds")
            return timer
        #rint(f"User {user_id} has score 0, using base timer {self.base_timer} seconds")
        return self.base_timer

    async def get_afk_channel(self, guild):
        if self.afk_channel_id:
            channel = guild.get_channel(self.afk_channel_id)
            if channel and isinstance(channel, discord.VoiceChannel):
                return channel
            else:
                print(f"Custom AFK channel ID {self.afk_channel_id} not found or not a voice channel in guild {guild.id}")
                return None
        else:
            if guild.afk_channel:
                #print(f"Using guild's AFK channel: {guild.afk_channel.name} (ID: {guild.afk_channel.id})")
                return guild.afk_channel
            else:
                print(f"No AFK channel set for guild {guild.id}")
                return None

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState):
        afk_channel = await self.get_afk_channel(member.guild)
        if not afk_channel:
            return

        if member.id in self.whitelist:
            return  # Ignore whitelisted users

        async def _cancel_pending(uid):
            task = self.pending_tasks.pop(uid, None)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # 1) Self-deaf state changed?
        if before.self_deaf != after.self_deaf:
            if after.self_deaf:
                # user started self-deafening
                #print(f"User {member.id} ({member.display_name}) self-deafened, checking timer")
                await _cancel_pending(member.id)
                timer = self.calculate_timer(member.id)
                if timer == float('inf'):
                    print(f"Infinite timer for user {member.id}, skipping move")
                elif after.channel and after.channel != afk_channel:
                    self.pending_tasks[member.id] = asyncio.create_task(self.deafen_timer(member, timer, afk_channel))
            else:
                # user stopped self-deafening -> cancel pending move
                await _cancel_pending(member.id)
                print(f"User {member.display_name} self-undeafened, cancelled task")

        # 2) Joined a channel while already self-deafened
        if before.channel is None and after.channel is not None:
            if after.self_deaf and after.channel != afk_channel:
                await _cancel_pending(member.id)
                timer = self.calculate_timer(member.id)
                if timer != float('inf'):
                    self.pending_tasks[member.id] = asyncio.create_task(self.deafen_timer(member, timer, afk_channel))

        # 3) Moved channels while deafened: cancel and restart if still deaf and not AFK
        if before.channel != after.channel and member.id in self.pending_tasks:
            await _cancel_pending(member.id)
            #print(f"User {member.id} ({member.display_name}) moved channels, cancelled task")
            if after.self_deaf and after.channel and after.channel != afk_channel:
                timer = self.calculate_timer(member.id)
                if timer != float('inf'):
                    self.pending_tasks[member.id] = asyncio.create_task(self.deafen_timer(member, timer, afk_channel))

        # 4) Left voice: cancel any pending task
        if after.channel is None and member.id in self.pending_tasks:
            await _cancel_pending(member.id)
            print(f"User {member.id} ({member.display_name}) left voice, cancelled task")

    async def deafen_timer(self, member: discord.Member, timer: float, afk_channel: discord.VoiceChannel):
        try:
            start_score = self.get_score(member.id)
            print(f"Waiting {timer} seconds to move user to afk: {member.display_name} (Social Credit = {start_score})")
            await asyncio.sleep(timer)

            # Re-check score/state at move time if you want the most up-to-date value
            current_score = self.get_score(member.id)

            # After timer, check if still self-deafened and in a voice channel
            if member.voice and member.voice.self_deaf:
                #print(f"Attempting move: {member.display_name} (social_credit = {current_score})")
                await member.move_to(afk_channel)
                print(f"Moved {member.display_name} to AFK. (Social Credit = {current_score})")
            else:
                print(
                    f"User {member.display_name} no longer self-deaf or in voice, not moving (score={current_score})")
        except discord.errors.Forbidden:
            print(
                f"Missing permissions to move user {member.id} ({member.display_name}) to AFK channel {afk_channel.id}")
        except discord.errors.HTTPException as e:
            print(f"HTTP error moving user {member.id} ({member.display_name}): {e}")
        except Exception as e:
            print(f"Unexpected error moving user {member.id} ({member.display_name}): {e}")
        finally:
            if member.id in self.pending_tasks:
                del self.pending_tasks[member.id]


async def setup(bot):
    await bot.add_cog(AFKMoverCog(bot))