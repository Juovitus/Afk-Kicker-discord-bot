import discord
from discord.ext import commands, tasks
import sqlite3
from datetime import datetime
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

class SocialCreditCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_path = 'social_credit.db'
        self.MIN_SCORE = int(os.getenv('MIN_SCORE', -100))
        self.MAX_SCORE = int(os.getenv('MAX_SCORE', 100))
        self.MAX_AMOUNT_PER_ACTION = int(os.getenv('MAX_AMOUNT_PER_ACTION', 10))
        self.REQUIRED_APPROVALS = int(os.getenv('REQUIRED_APPROVALS', 2))
        self.PROPOSAL_TIMEOUT_MINUTES = int(os.getenv('PROPOSAL_TIMEOUT_MINUTES', 5))  # Default 5 minutes
        self.pending_approvals = {}  # message_id -> {'author_id':, 'target_id':, 'amount':, 'is_add':, 'approvers': set(), 'channel_id':, 'result_message_id':}
        self.init_db()
        self.reset_actions.start()

    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS scores 
                     (user_id INTEGER PRIMARY KEY, score INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS actions 
                     (user_id INTEGER, target_id INTEGER, last_action TIMESTAMP)''')
        conn.commit()
        conn.close()

    @tasks.loop(hours=int(os.getenv('ACTION_RESET_HOURS', 6)))  # 6 hours
    async def reset_actions(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('DELETE FROM actions')  # Clear all action records
        conn.commit()
        conn.close()
        print("Action cooldown reset completed.")

    def can_perform_action(self, user_id, target_id):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('SELECT last_action FROM actions WHERE user_id = ? AND target_id = ?',
                  (user_id, target_id))
        result = c.fetchone()
        conn.close()

        if result is None:
            return True
        return False

    def record_action(self, user_id, target_id):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO actions (user_id, target_id, last_action) VALUES (?, ?, ?)',
                  (user_id, target_id, datetime.now()))
        conn.commit()
        conn.close()

    def get_score(self, user_id):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('SELECT score FROM scores WHERE user_id = ?', (user_id,))
        result = c.fetchone()
        conn.close()
        return result[0] if result else 0

    def update_score(self, user_id, amount):
        current_score = self.get_score(user_id)
        new_score = current_score + amount
        if new_score < self.MIN_SCORE:
            new_score = self.MIN_SCORE
        elif new_score > self.MAX_SCORE:
            new_score = self.MAX_SCORE

        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO scores (user_id, score) VALUES (?, ?)',
                  (user_id, new_score))
        conn.commit()
        conn.close()

    async def cleanup_proposal(self, message_id):
        details = self.pending_approvals.get(message_id)
        if details:
            try:
                proposal_msg = await self.bot.get_channel(details['channel_id']).fetch_message(message_id)
                await proposal_msg.delete()
            except discord.errors.NotFound:
                pass  # Proposal message already deleted
            except Exception as e:
                print(f"Error cleaning up proposal {message_id}: {e}")
            finally:
                self.pending_approvals.pop(message_id, None)

    @commands.command(name="credit")
    async def credit(self, ctx, member: discord.Member, amount: int, *, reason: str):
        # Delete the command message
        try:
            await ctx.message.delete()
        except discord.errors.Forbidden:
            print(f"Missing permissions to delete command message in channel {ctx.channel.id}")
        except Exception as e:
            print(f"Error deleting command message: {e}")

        # Amount must not be zero (use positive to add, negative to deduct)
        if amount == 0:
            await ctx.send("Amount must be non-zero! Use a positive number to add or a negative number to deduct.",
                           delete_after=self.PROPOSAL_TIMEOUT_MINUTES * 30)
            return

        # Cap absolute amount to MAX_AMOUNT_PER_ACTION
        sign = 1 if amount > 0 else -1
        abs_amount = abs(amount)
        abs_amount = min(abs_amount, self.MAX_AMOUNT_PER_ACTION)
        signed_amount = abs_amount * sign

        if ctx.author.id == member.id:
            await ctx.send("You can't modify your own score!", delete_after=self.PROPOSAL_TIMEOUT_MINUTES * 30)
            return

        if not self.can_perform_action(ctx.author.id, member.id):
            reset_hours = os.getenv("ACTION_RESET_HOURS")
            await ctx.send(f"You've performed this action recently, actions reset every {reset_hours} hours.",
                           delete_after=self.PROPOSAL_TIMEOUT_MINUTES * 30)
            return

        # Determine add vs deduct for display and for downstream handling
        is_add = signed_amount > 0
        verb = "Add" if is_add else "Deduct"
        display_amount = abs(signed_amount)

        # Compose proposal message
        proposal_msg = await ctx.send(
            f"Proposal: {verb} {display_amount} social credit "
            f"{'to' if is_add else 'from'} {member.display_name} by {ctx.author.display_name}.\n"
            f"**Reason:** {reason}\n"
            f"Needs {self.REQUIRED_APPROVALS} ✅ reactions to approve, or 1 ❌ to deny "
            f"within {self.PROPOSAL_TIMEOUT_MINUTES} minutes."
        )
        await proposal_msg.add_reaction('✅')
        await proposal_msg.add_reaction('❌')

        # Store pending approval info (keep both signed and absolute amounts)
        self.pending_approvals[proposal_msg.id] = {
            'author_id': ctx.author.id,
            'target_id': member.id,
            'amount': display_amount,  # absolute amount for most logic
            'signed_amount': signed_amount,  # signed amount if you prefer signed arithmetic
            'reason': reason,
            'is_add': is_add,
            'approvers': set(),
            'channel_id': ctx.channel.id,
            'result_message_id': None
        }

        # Schedule cleanup after timeout
        self.bot.loop.create_task(self.schedule_cleanup(proposal_msg.id))

    async def schedule_cleanup(self, message_id):
        await asyncio.sleep(self.PROPOSAL_TIMEOUT_MINUTES * 60)
        if message_id in self.pending_approvals:
            await self.cleanup_proposal(message_id)

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        if user.bot:
            return
        message_id = reaction.message.id
        if message_id not in self.pending_approvals:
            return
        details = self.pending_approvals[message_id]
        if user.id == details['author_id'] or user.id == details['target_id']:
            return  # Ignore reactions from author or target
        if str(reaction.emoji) not in ['✅', '❌']:
            return

        def _display_name_from_userobj(u):
            return getattr(u, "display_name", None) or getattr(u, "name", None) or f"<@{getattr(u, 'id', 'unknown')}>"

        async def _resolve_user(user_id):
            u = self.bot.get_user(user_id)
            if u is None:
                try:
                    u = await self.bot.fetch_user(user_id)
                except Exception:
                    u = None
            return u

        if str(reaction.emoji) == '✅':
            details['approvers'].add(user.id)

            approver_names = []
            for uid in details['approvers']:
                uobj = await _resolve_user(uid)
                approver_names.append(_display_name_from_userobj(uobj) if uobj else f"<@{uid}>")
            approvers_text = ", ".join(approver_names) if approver_names else "No one"

            if len(details['approvers']) >= self.REQUIRED_APPROVALS:
                # Final approval
                target_member = await _resolve_user(details['target_id'])
                verb = "Added" if details.get('is_add', True) else "Deducted"
                amount_sign = details['amount'] if details.get('is_add', True) else -details['amount']

                # Update score
                self.update_score(details['target_id'], amount_sign)

                result_msg = await reaction.message.channel.send(
                    f"✅ Approved by {len(details['approvers'])} user{'s' if len(details['approvers']) != 1 else ''}: {approvers_text}\n"
                    f"{verb} {details['amount']} social credit "
                    f"{'to' if details.get('is_add', True) else 'from'} {_display_name_from_userobj(target_member)}.\n"
                    f"**Reason:** {details.get('reason', 'No reason provided')}\n"
                    f"New score: {self.get_score(details['target_id'])}",
                    delete_after=self.PROPOSAL_TIMEOUT_MINUTES * 60
                )

                self.record_action(details['author_id'], details['target_id'])
                details['result_message_id'] = result_msg.id
                await self.cleanup_proposal(message_id)
            else:
                # Intermediate status update
                target_member = await _resolve_user(details['target_id'])
                author_member = await _resolve_user(details['author_id'])
                remaining = self.REQUIRED_APPROVALS - len(details['approvers'])
                verb = "Add" if details.get('is_add', True) else "Deduct"
                direction = "to" if details.get('is_add', True) else "from"

                await reaction.message.edit(
                    content=(
                        f"Proposal: {verb} {details['amount']} social credit {direction} "
                        f"{_display_name_from_userobj(target_member)} by {_display_name_from_userobj(author_member)}.\n"
                        f"**Reason:** {details.get('reason', 'No reason provided')}\n\n"
                        f"Approved by ({len(details['approvers'])}): {approvers_text}\n"
                        f"Needs {remaining} more ✅ reaction{'s' if remaining != 1 else ''} to approve, "
                        f"or 1 ❌ to deny within {self.PROPOSAL_TIMEOUT_MINUTES} minutes."
                    )
                )
        else:
            # Denied
            target_member = await _resolve_user(details['target_id'])
            denier = user
            result_msg = await reaction.message.channel.send(
                f"❌ Denied by {_display_name_from_userobj(denier)}! No change made.\n"
                f"**Reason:** {details.get('reason', 'No reason provided')}",
                delete_after=self.PROPOSAL_TIMEOUT_MINUTES * 60
            )
            details['result_message_id'] = result_msg.id
            await self.cleanup_proposal(message_id)

    @commands.command()
    async def score(self, ctx, member: discord.Member = None):
        # Delete the command message
        try:
            await ctx.message.delete()
        except discord.errors.Forbidden:
            print(f"Missing permissions to delete command message in channel {ctx.channel.id}")
        except Exception as e:
            print(f"Error deleting command message: {e}")

        if member is None:
            member = ctx.author
        score = self.get_score(member.id)
        await ctx.send(f"{member.display_name}'s social credit score: {score}", delete_after=self.PROPOSAL_TIMEOUT_MINUTES * 60)

async def setup(bot):
    await bot.add_cog(SocialCreditCog(bot))