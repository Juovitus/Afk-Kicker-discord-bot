import discord
from discord.ext import commands
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True  # Needed for voice state updates in AFK mover
bot = commands.Bot(command_prefix='!', intents=intents)

# Load cogs asynchronously
async def load_cogs():
    try:
        await bot.load_extension('cogs.social_credit')
        await bot.load_extension('cogs.afk_mover')
    except Exception as e:
        print(f"Failed to load cogs: {e}")
        raise

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')

# Run the bot
def run_bot():
    token = os.getenv('DISCORD_BOT_TOKEN')
    print(f"Loaded token (first 10 chars): {token[:10] if token else None}...")
    if not token:
        raise ValueError("DISCORD_BOT_TOKEN not found in .env file")
    if len(token) < 59:  # Typical Discord bot token length is ~59-72 chars
        raise ValueError("DISCORD_BOT_TOKEN appears invalid (too short)")
    
    bot.setup_hook = load_cogs
    
    try:
        bot.run(token)
    except discord.errors.LoginFailure as e:
        print(f"Login failed: {e}. Please verify the DISCORD_BOT_TOKEN in .env")
        raise
    except Exception as e:
        print(f"Bot failed to start: {e}")
        raise

if __name__ == "__main__":
    run_bot()
