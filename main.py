import os
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import pytz
import random
import logging
import re
import asyncio

fireteams = {}  # { date_str: {slot_index: user_id} }
backups = {}    # { date_str: {slot_index: user_id} }
lock = asyncio.Lock()

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

@bot.event
async def on_resumed():
    logging.info("Session RESUMED → checking for missing raid posts")

    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        # Rebuild the in-memory list from history
        previous_week_messages.clear()
        async for msg in channel.history(limit=200):
            if msg.author == bot.user and "CLAN RAID EVENT" in msg.content:
                previous_week_messages.append(msg.id)

    # If we still have no posts tracked, rebuild the week block
    if not previous_week_messages:
        logging.info("No raid posts found on resume → posting week block now")
        await schedule_weekly_posts_function()
# —————————————————————————————————————————
# Reactions: ✅ join / ❌ leave
# —————————————————————————————————————————
def extract_date(content: str) -> str | None:
    m = re.search(r"\*\*Day:\*\*\s*(.+?)\s*\|", content)
    return m.group(1).strip() if m else None

@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return

    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)
    emoji = str(payload.emoji)

    async with lock:
        channel = bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        date_str = extract_date(message.content)

        if not date_str:
            print("Could not extract date from message.")
            return

        if date_str not in fireteams:
            fireteams[date_str] = {}
        if date_str not in backups:
            backups[date_str] = {}

        print(f"Reaction added: {emoji} by {member.display_name}")

        if emoji == "❌":
            print(f"{member.display_name} opted out. Removing from slots...")
            for slot, uid in list(fireteams[date_str].items()):
                if uid == member.id:
                    del fireteams[date_str][slot]
            for slot, uid in list(backups[date_str].items()):
                if uid == member.id:
                    del backups[date_str][slot]

        elif emoji == "✅":
            if member.id in fireteams[date_str].values() or member.id in backups[date_str].values():
                print(f"{member.display_name} is already assigned.")
                return

            for i in range(6):
                if i not in fireteams[date_str]:
                    fireteams[date_str][i] = member.id
                    print(f"{member.display_name} assigned to fireteam slot {i+1}")
                    break
            else:
                for i in range(2):
                    if i not in backups[date_str]:
                        backups[date_str][i] = member.id
                        print(f"{member.display_name} assigned to backup slot {i+1}")
                        break

        await update_raid_message(payload.message_id, date_str)


    new_content = await build_raid_message(date_str)
    await message.edit(content=new_content)

@bot.event
async def on_raw_reaction_remove(payload):
    if payload.user_id == bot.user.id:
        return

    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)

    async with lock:
        channel = bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        date_str = extract_date(message.content)

        if not date_str:
            print("Could not extract date from message.")
            return

        print(f"Reaction removed by {member.display_name}")
        print(f"Removing {member.display_name} from all slots...")

        if date_str in fireteams:
            for slot, uid in list(fireteams[date_str].items()):
                if uid == member.id:
                    del fireteams[date_str][slot]
        if date_str in backups:
            for slot, uid in list(backups[date_str].items()):
                if uid == member.id:
                    del backups[date_str][slot]

        await update_raid_message(payload.message_id, date_str)

async def update_raid_message(message_id, date_str):
    channel = bot.get_channel(1209484610568720384) 
    message = await channel.fetch_message(message_id)

    lines = []

    for i in range(6):
        uid = fireteams[date_str].get(i)
        if uid:
            user = await bot.fetch_user(uid)
            lines.append(f"{i+1}. {user.display_name}")
        else:
            lines.append(f"{i+1}. Empty Slot")

    lines.append("\nBackups:")
    for i in range(2):
        uid = backups[date_str].get(i)
        if uid:
            user = await bot.fetch_user(uid)
            lines.append(f"Backup {i+1}: {user.display_name}")
        else:
            lines.append(f"Backup {i+1}: Empty")

    await message.edit(content="\n".join(lines))

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

@bot.command(name="showlineup")
async def show_lineup(ctx, *, date_str: str):
    if date_str not in fireteams and date_str not in backups:
        await ctx.send(f"No lineup found for **{date_str}**.")
        return

    lines = [f"**Lineup for {date_str}:**"]

    # Fireteam slots
    lines.append("\nFireteam:")
    for i in range(6):
        uid = fireteams.get(date_str, {}).get(i)
        if uid:
            user = await bot.fetch_user(uid)
            lines.append(f"{i+1}. {user.display_name}")
        else:
            lines.append(f"{i+1}. Empty Slot")

    # Backup slots
    lines.append("\nBackups:")
    for i in range(2):
        uid = backups.get(date_str, {}).get(i)
        if uid:
            user = await bot.fetch_user(uid)
            lines.append(f"Backup {i+1}: {user.display_name}")
        else:
            lines.append(f"Backup {i+1}: Empty")

    await ctx.send("\n".join(lines))


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
