import os
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import pytz
import random
import logging
import re
import asyncio

# Setup logging
logging.basicConfig(level=logging.INFO)

# Intents
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

# Bot setup
bot = commands.Bot(command_prefix="!", intents=intents)
lock = asyncio.Lock()

# Globals
fireteams = {}                # { date_str: [user_id, …] }
backups = {}                  # { date_str: [user_id, …] }
scores = {}                   # { user_id: points }
user_scores = {}              # dice‐game scores
previous_week_messages = []   # IDs of last Sunday’s 7 posts
last_schedule_date = None     # to ensure one run/Sunday
CHANNEL_ID = 1209484610568720384  # your raid channel ID

# —————————————————————————————————————————
# Helper: Build the exact raid message text
# —————————————————————————————————————————
async def build_raid_message(date_str: str) -> str:
    fire_ids = fireteams.get(date_str, [])
    backup_ids = backups.get(date_str, [])

    lines = [
        "@everyone",
        "🔥 **CLAN RAID EVENT: Desert Perpetual** 🔥",
        "",
        f"📅 **Day:** {date_str} | 🕗 **Time:** 20:00 BST",
        "",
        "🎯 **Fireteam Lineup (6 Players):**"
    ]

    # main slots
    for i in range(6):
        if i < len(fire_ids):
            user = await bot.fetch_user(fire_ids[i])
            lines.append(f"{i+1}. {user.display_name}")
        else:
            lines.append(f"{i+1}. Empty Slot")

    lines.append("")  # spacer
    lines.append("🛡️ **Backup Players (2):**")

    # backup slots
    for i in range(2):
        if i < len(backup_ids):
            user = await bot.fetch_user(backup_ids[i])
            lines.append(f"{i+1}. {user.display_name}")
        else:
            lines.append(f"{i+1}. Empty Slot")

    lines.extend([
        "",
        "✅ React with a ✅ if you're joining the raid.",
        "❌ React with a ❌ if you can't make it.",
        "",
        "Let’s assemble a legendary team and conquer the Desert Perpetual!"
    ])

    return "\n".join(lines)


# —————————————————————————————————————————
# Core Scheduler: Run once each Sunday at 09:00 BST
# —————————————————————————————————————————
@tasks.loop(minutes=1)
async def sunday_scheduler():
    global last_schedule_date

    tz = pytz.timezone("Europe/London")
    now = datetime.now(tz)

    # Fire exactly once at 09:00 on Sundays
    if now.weekday() == 6 and now.hour == 9 and last_schedule_date != now.date():
        await schedule_weekly_posts_function()
        last_schedule_date = now.date()

    # Reset flag any other day
    if now.weekday() != 6:
        last_schedule_date = None


async def schedule_weekly_posts_function():
    tz = pytz.timezone("Europe/London")
    now = datetime.now(tz)
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        logging.error(f"Could not find channel {CHANNEL_ID}")
        return

    # Delete the previous 7 posts
    for msg_id in previous_week_messages:
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.delete()
        except discord.NotFound:
            pass
    previous_week_messages.clear()

    # Scan for already‐posted dates (avoid duplicate Fri/Sun)
    posted_dates = set()
    async for msg in channel.history(limit=200):
        if msg.author == bot.user and "CLAN RAID EVENT" in msg.content:
            m = re.search(r"\*\*Day:\*\*\s*(.+?)\s*\|", msg.content)
            if m:
                posted_dates.add(m.group(1).strip())

    # Post a block for the next 7 days
    for delta in range(7):
        raid_dt = now + timedelta(days=delta)
        date_str = raid_dt.strftime("%A, %d %B")

        if date_str in posted_dates:
            continue

        fireteams.setdefault(date_str, [])
        backups.setdefault(date_str, [])

        content = await build_raid_message(date_str)
        msg = await channel.send(content)
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")
        previous_week_messages.append(msg.id)


# —————————————————————————————————————————
# Bot Events
# —————————————————————————————————————————
@bot.event
async def on_ready():
    logging.info(f"Bot started as {bot.user}")
    sunday_scheduler.start()
    send_reminders.start()

    # If we restarted and have no previous raid-post IDs, post the week's raids immediately.
    if not previous_week_messages:
        logging.info("No existing raid posts found on startup – posting initial week block.")
        await schedule_weekly_posts_function()

# —————————————————————————————————————————
# Reactions: ✅ join / ❌ leave
# —————————————————————————————————————————
def extract_date(content: str) -> str | None:
    m = re.search(r"\*\*Day:\*\*\s*(.+?)\s*\|", content)
    return m.group(1).strip() if m else None


@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id or payload.channel_id != CHANNEL_ID:
        return
    if payload.emoji.name not in ("✅", "❌"):
        return

    channel = bot.get_channel(payload.channel_id)
    message = await channel.fetch_message(payload.message_id)
    if message.author != bot.user:
        return

    date_str = extract_date(message.content)
    if not date_str:
        return

    async with lock:
        ft = fireteams.setdefault(date_str, [])
        bu = backups.setdefault(date_str, [])
        guild = bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id)

        if payload.emoji.name == "✅":
            if member.id in ft or member.id in bu:
                return
            if len(ft) < 6:
                ft.append(member.id)
                await member.send(f"🎉 You’re in for {date_str} @ 20:00 BST!")
            elif len(bu) < 2:
                bu.append(member.id)
                await member.send(f"🛡️ You’re on backup for {date_str}.")
            else:
                await member.send(f"⚠️ {date_str} is full (6 + 2).")
        else:  # ❌
            removed = False
            if member.id in ft:
                ft.remove(member.id)
                removed = True
            if member.id in bu:
                bu.remove(member.id)
                removed = True
            if removed:
                await member.send(f"❌ You’ve been removed from {date_str} raid/backups.")

    new_content = await build_raid_message(date_str)
    await message.edit(content=new_content)


@bot.event
async def on_raw_reaction_remove(payload):
    if payload.user_id == bot.user.id or payload.channel_id != CHANNEL_ID:
        return
    if payload.emoji.name not in ("✅", "❌"):
        return

    channel = bot.get_channel(payload.channel_id)
    message = await channel.fetch_message(payload.message_id)
    if message.author != bot.user:
        return

    date_str = extract_date(message.content)
    if not date_str:
        return

    async with lock:
        ft = fireteams.get(date_str, [])
        bu = backups.get(date_str, [])
        guild = bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id)

        removed = False
        if member.id in ft:
            ft.remove(member.id)
            removed = True
        if member.id in bu:
            bu.remove(member.id)
            removed = True
        if removed:
            await member.send(f"❌ You’ve been removed from {date_str} raid/backups.")
            new_content = await build_raid_message(date_str)
            await message.edit(content=new_content)


# —————————————————————————————————————————
# Reminders: unchanged except for date regex
# —————————————————————————————————————————
@tasks.loop(minutes=1)
async def send_reminders():
    tz = pytz.timezone("Europe/London")
    now = datetime.now(tz)
    for date_str, players in fireteams.items():
        try:
            dt = datetime.strptime(date_str, "%A, %d %B")\
                     .replace(year=now.year, hour=19, minute=0)
            if now.strftime("%A, %d %B %H:%M") == dt.strftime("%A, %d %B %H:%M"):
                for uid in players:
                    user = await bot.fetch_user(uid)
                    await user.send(
                        "⏳ One hour to go! See you at 20:00 BST for Desert Perpetual."
                    )
                    scores[uid] = scores.get(uid, 0) + 1
        except Exception as e:
            logging.warning(f"Reminder error for '{date_str}': {e}")


# —————————————————————————————————————————
# Commands (unchanged)
# —————————————————————————————————————————
@bot.command(name="Raidleaderboard")
async def Raidleaderboard(ctx):
    if not scores:
        return await ctx.send("No scores yet. Start raiding to earn points!")
    sorted_scores = sorted(
        [(uid, pts) for uid, pts in scores.items() if uid != bot.user.id],
        key=lambda x: x[1], reverse=True
    )
    lines = []
    for uid, pts in sorted_scores:
        user = await bot.fetch_user(uid)
        lines.append(f"**{user.name}**: {pts} point{'s' if pts != 1 else ''}")
    await ctx.send("🏆 **Raid Leaderboard** 🏆\n" + "\n".join(lines))


@bot.command(name="roll")
async def roll_dice(ctx, sides: int = 6):
    if sides < 2:
        return await ctx.send("Dice must have at least 2 sides!")
    result = random.randint(1, sides)
    uid = str(ctx.author.id)
    user_scores.setdefault(uid, {"name": ctx.author.display_name, "score": 0})
    user_scores[uid]["score"] += result
    await ctx.send(
        f"🎲 {ctx.author.display_name} rolled a {result}! "
        f"Total score: {user_scores[uid]['score']}"
    )


@bot.command(name="leaderboard")
async def show_leaderboard(ctx):
    if not user_scores:
        return await ctx.send("No scores yet! Roll the dice with `!roll`.")
    sorted_us = sorted(user_scores.values(), key=lambda x: x["score"], reverse=True)
    msg = "**🏆 Dice Leaderboard 🏆**\n"
    for i, player in enumerate(sorted_us[:5], start=1):
        msg += f"{i}. {player['name']} – {player['score']} pts\n"
    await ctx.send(msg)


# —————————————————————————————————————————
# Run Bot
# —————————————————————————————————————————
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logging.error("DISCORD_TOKEN environment variable is missing.")
        exit(1)

    masked = token[:4] + "…" + token[-4:]
    print("» Using Discord token:", masked)
    bot.run(token)
