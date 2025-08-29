import discord
from discord.ext import commands, tasks
import sqlite3
from datetime import datetime
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

APPROVE_EMOJI = '✅'
DENY_EMOJI = '❌'

class SocialCreditCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_path = 'social_credit.db'

        # Config from env
        self.MIN_SCORE = int(os.getenv('MIN_SCORE', -100))
        self.MAX_SCORE = int(os.getenv('MAX_SCORE', 100))
        self.MAX_AMOUNT_PER_ACTION = int(os.getenv('MAX_AMOUNT_PER_ACTION', 10))
        self.REQUIRED_APPROVALS = int(os.getenv('REQUIRED_APPROVALS', 2))
        self.PROPOSAL_TIMEOUT_MINUTES = int(os.getenv('PROPOSAL_TIMEOUT_MINUTES', 5))
        self.ACTION_RESET_HOURS = int(os.getenv('ACTION_RESET_HOURS', 6))

        # Runtime state
        self.pending_approvals = {}  # message_id -> details
        self.cleanup_tasks = {}

        self.init_db()
        self.reset_actions.start()

    # ---------------- DB Helpers ----------------
    def init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS scores 
                         (user_id INTEGER PRIMARY KEY, score INTEGER DEFAULT 0)''')
            c.execute('''CREATE TABLE IF NOT EXISTS actions 
                         (user_id INTEGER, target_id INTEGER, last_action TIMESTAMP,
                          UNIQUE(user_id, target_id))''')

    def db_query(self, query, params=(), fetchone=False, fetchall=False):
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute(query, params)
            if fetchone:
                return c.fetchone()
            if fetchall:
                return c.fetchall()
            conn.commit()

    # ---------------- Task: Reset Actions ----------------
    @tasks.loop(hours=1)
    async def reset_actions(self):
        self.db_query('DELETE FROM actions')
        print("Action cooldown reset completed.")

    @reset_actions.before_loop
    async def before_reset_actions(self):
        await self.bot.wait_until_ready()

    # ---------------- Core DB Logic ----------------
    def can_perform_action(self, user_id, target_id):
        return self.db_query(
            'SELECT last_action FROM actions WHERE user_id = ? AND target_id = ?',
            (user_id, target_id), fetchone=True
        ) is None

    def record_action(self, user_id, target_id):
        self.db_query(
            'INSERT OR REPLACE INTO actions (user_id, target_id, last_action) VALUES (?, ?, ?)',
            (user_id, target_id, datetime.now())
        )

    def get_score(self, user_id):
        row = self.db_query('SELECT score FROM scores WHERE user_id = ?', (user_id,), fetchone=True)
        return row[0] if row else 0

    def update_score(self, user_id, amount):
        current_score = self.get_score(user_id)
        new_score = max(self.MIN_SCORE, min(self.MAX_SCORE, current_score + amount))
        self.db_query(
            'INSERT OR REPLACE INTO scores (user_id, score) VALUES (?, ?)',
            (user_id, new_score)
        )

    # ---------------- Helpers ----------------
    async def cleanup_proposal(self, message_id):
        details = self.pending_approvals.pop(message_id, None)
        if not details:
            return
        try:
            proposal_msg = await self.bot.get_channel(details['channel_id']).fetch_message(message_id)
            await proposal_msg.delete()
        except discord.errors.NotFound:
            pass
        except Exception as e:
            print(f"Error cleaning up proposal {message_id}: {e}")
        self.cleanup_tasks.pop(message_id, None)

    async def schedule_cleanup(self, message_id):
        self.cleanup_tasks[message_id] = asyncio.create_task(self._cleanup_after_timeout(message_id))

    async def _cleanup_after_timeout(self, message_id):
        await asyncio.sleep(self.PROPOSAL_TIMEOUT_MINUTES * 60)
        if message_id in self.pending_approvals:
            await self.cleanup_proposal(message_id)

    def format_proposal(self, details, approvers=None, remaining=None):
        verb = "Add" if details['is_add'] else "Deduct"
        direction = "to" if details['is_add'] else "from"
        base = (
            f"Proposal: {verb} {details['amount']} social credit {direction} {details['target_name']} "
            f"by {details['author_name']}.\n**Reason:** {details['reason']}"
        )
        if remaining is not None:
            base += f"\n\nApproved by ({len(details['approvers'])}): {approvers}\nNeeds {remaining} more {APPROVE_EMOJI} " \
                    f"or 1 {DENY_EMOJI} to deny within {self.PROPOSAL_TIMEOUT_MINUTES} minutes."
        return base

    async def get_display_name(self, uid):
        u = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
        return getattr(u, "display_name", None) or getattr(u, "name", None) or f"<@{uid}>"

    # ---------------- Commands ----------------
    @commands.command(name="credit")
    async def credit(self, ctx, member: discord.Member, amount: int, *, reason: str):
        try:
            await ctx.message.delete()
        except discord.errors.Forbidden:
            pass

        if amount == 0:
            return await ctx.send("Amount must be non-zero!", delete_after=10)
        if ctx.author.id == member.id:
            return await ctx.send("You can't modify your own score!", delete_after=10)

        signed_amount = max(-self.MAX_AMOUNT_PER_ACTION, min(self.MAX_AMOUNT_PER_ACTION, amount))
        is_add = signed_amount > 0

        if not self.can_perform_action(ctx.author.id, member.id):
            return await ctx.send(
                f"You've performed this action recently, actions reset every {self.ACTION_RESET_HOURS} hours.",
                delete_after=10
            )

        proposal_msg = await ctx.send(
            f"Proposal: {'Add' if is_add else 'Deduct'} {abs(signed_amount)} social credit "
            f"{'to' if is_add else 'from'} {member.display_name} by {ctx.author.display_name}.\n"
            f"**Reason:** {reason}\nNeeds {self.REQUIRED_APPROVALS} {APPROVE_EMOJI} or 1 {DENY_EMOJI} within "
            f"{self.PROPOSAL_TIMEOUT_MINUTES} minutes."
        )
        await proposal_msg.add_reaction(APPROVE_EMOJI)
        await proposal_msg.add_reaction(DENY_EMOJI)

        self.pending_approvals[proposal_msg.id] = {
            'author_id': ctx.author.id,
            'target_id': member.id,
            'author_name': ctx.author.display_name,
            'target_name': member.display_name,
            'amount': abs(signed_amount),
            'signed_amount': signed_amount,
            'reason': reason,
            'is_add': is_add,
            'approvers': set(),
            'channel_id': ctx.channel.id,
            'result_message_id': None
        }

        await self.schedule_cleanup(proposal_msg.id)

    @commands.command()
    async def score(self, ctx, member: discord.Member = None):
        try:
            await ctx.message.delete()
        except discord.errors.Forbidden:
            pass
        member = member or ctx.author
        score = self.get_score(member.id)
        await ctx.send(f"{member.display_name}'s social credit score: {score}", delete_after=30)

    # ---------------- Reaction Listener ----------------
    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        if user.bot or str(reaction.emoji) not in [APPROVE_EMOJI, DENY_EMOJI]:
            return
        msg_id = reaction.message.id
        if msg_id not in self.pending_approvals:
            return
        details = self.pending_approvals[msg_id]
        if user.id in [details['author_id'], details['target_id']]:
            return

        if str(reaction.emoji) == APPROVE_EMOJI:
            details['approvers'].add(user.id)
            approvers_names = [await self.get_display_name(uid) for uid in details['approvers']]
            if len(details['approvers']) >= self.REQUIRED_APPROVALS:
                self.update_score(details['target_id'], details['signed_amount'])
                self.record_action(details['author_id'], details['target_id'])
                await reaction.message.channel.send(
                    f"✅ Approved by {len(details['approvers'])}: {', '.join(approvers_names)}\n"
                    f"{'Added' if details['is_add'] else 'Deducted'} {details['amount']} social credit "
                    f"{'to' if details['is_add'] else 'from'} {details['target_name']}.\n"
                    f"**Reason:** {details['reason']}\nNew score: {self.get_score(details['target_id'])}",
                    delete_after=30
                )
                await self.cleanup_proposal(msg_id)
            else:
                await reaction.message.edit(
                    content=self.format_proposal(
                        details, approvers=", ".join(approvers_names),
                        remaining=self.REQUIRED_APPROVALS - len(details['approvers'])
                    )
                )
        else:
            await reaction.message.channel.send(
                f"❌ Denied by {await self.get_display_name(user.id)}! No change made.\n"
                f"**Reason:** {details['reason']}",
                delete_after=30
            )
            await self.cleanup_proposal(msg_id)

async def setup(bot):
    await bot.add_cog(SocialCreditCog(bot))
