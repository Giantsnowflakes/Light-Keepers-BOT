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

# Globals
fireteams = {}           # { date_str: [user_id, …] }
backups = {}             # { date_str: [user_id, …] }
scores = {}              # { user_id: points }
user_scores = {}         # dice-game scores
previous_week_messages = []  # to delete old raid posts
CHANNEL_ID = 1209484610568720384  # your raid channel ID

# ————————
# Helper: Build the exact raid message text
# ————————
async def build_raid_message(date_str: str) -> str:
    """Returns the full raid signup text for a given date."""
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

    # main slots 1–6
    for i in range(6):
        if i < len(fire_ids):
            user = await bot.fetch_user(fire_ids[i])
            lines.append(f"{i+1}. {user.display_name}")
        else:
            lines.append(f"{i+1}. Empty Slot")

    lines.append("")  # spacer
    lines.append("🛡️ **Backup Players (2):**")

    # backup slots 1–2
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


# ————————
# Weekly Raid Schedule Poster
# ————————
async def schedule_weekly_posts_function():
    london = pytz.timezone("Europe/London")
    now = datetime.now(london)
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        logging.error(f"Could not find channel {CHANNEL_ID}")
        return

    # On Sunday, delete last week's posts
    if now.weekday() == 6 and previous_week_messages:
        for msg_id in previous_week_messages:
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.delete()
            except discord.NotFound:
                pass
        previous_week_messages.clear()

    # Post for the next 7 days
    for i in range(7):
        raid_date = now + timedelta(days=i)
        date_str = raid_date.strftime("%A, %d %B")

        # skip if we've already posted this date
        recent = [m async for m in channel.history(limit=100)]
        if any(date_str in m.content for m in recent):
            continue

        # init our slot lists
        fireteams.setdefault(date_str, [])
        backups.setdefault(date_str, [])

        # build & send the plaintext message
        content = await build_raid_message(date_str)
        msg = await channel.send(content)
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")
        previous_week_messages.append(msg.id)


# Detect missed Sunday runs
async def check_missed_schedule():
    london = pytz.timezone("Europe/London")
    now = datetime.now(london)
    # If it's Sunday after 09:00 and we haven't posted yet
    if now.weekday() == 6 and now.hour >= 9 and not previous_week_messages:
        await schedule_weekly_posts_function()


# ————————
# Bot Events
# ————————
@bot.event
async def on_ready():
        print("✅ on_ready fired – bot is up as", bot.user)
    logging.info(f"Bot started as {bot.user}")
    schedule_weekly_posts.start()
    send_reminders.start()
    await check_missed_schedule()


@bot.event
async def on_command_error(ctx, error):
    logging.warning(f"Command error in {ctx.command}: {error}")


# Reaction Add: handle ✅ & ❌
@bot.event
async def on_raw_reaction_add(payload):
    # ignore bot’s own emoji
    if payload.user_id == bot.user.id:
        return

    # only our channel
    if payload.channel_id != CHANNEL_ID:
        return

    # only handle ✅ or ❌
    if payload.emoji.name not in ("✅", "❌"):
        return

    channel = bot.get_channel(payload.channel_id)
    message = await channel.fetch_message(payload.message_id)

    # ensure this is our raid message
    if message.author != bot.user:
        return
    if "CLAN RAID EVENT: Desert Perpetual" not in message.content:
        return

    # extract date
    match = re.search(r"Day:\s*(.+?)\s*\|", message.content)
    if not match:
        return
    date_str = match.group(1).strip()

    # init lists
    fireteams.setdefault(date_str, [])
    backups.setdefault(date_str, [])

    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)

    # JOIN: ✅
    if payload.emoji.name == "✅":
        # already in?
        if member.id in fireteams[date_str] or member.id in backups[date_str]:
            return

        if len(fireteams[date_str]) < 6:
            fireteams[date_str].append(member.id)
            await member.send(f"You're in! 🎉 Raid on {date_str} at 20:00 BST.")
        elif len(backups[date_str]) < 2:
            backups[date_str].append(member.id)
            await member.send(f"You’re on backup for {date_str} at 20:00 BST.")
        else:
            await member.send(f"Sorry — {date_str} is full (6 main + 2 backups).")

    # LEAVE: ❌
    else:
        removed = False
        if member.id in fireteams[date_str]:
            fireteams[date_str].remove(member.id)
            removed = True
        if member.id in backups[date_str]:
            backups[date_str].remove(member.id)
            removed = True
        if removed:
            await member.send(f"You've been removed from {date_str} raid or backups.")

    # rebuild and edit message
    new_content = await build_raid_message(date_str)
    await message.edit(content=new_content)


# Reaction Remove: also free slots when someone un-reacts
@bot.event
async def on_raw_reaction_remove(payload):
    # same filters as add
    if payload.user_id == bot.user.id:
        return
    if payload.channel_id != CHANNEL_ID:
        return
    if payload.emoji.name not in ("✅", "❌"):
        return

    channel = bot.get_channel(payload.channel_id)
    message = await channel.fetch_message(payload.message_id)
    if message.author != bot.user:
        return
    if "CLAN RAID EVENT: Desert Perpetual" not in message.content:
        return

    match = re.search(r"Day:\s*(.+?)\s*\|", message.content)
    if not match:
        return
    date_str = match.group(1).strip()

    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)

    removed = False
    if member.id in fireteams.get(date_str, []):
        fireteams[date_str].remove(member.id)
        removed = True
    if member.id in backups.get(date_str, []):
        backups[date_str].remove(member.id)
        removed = True

    if removed:
        await member.send(f"You've been removed from {date_str} raid or backups.")
        new_content = await build_raid_message(date_str)
        await message.edit(content=new_content)


# ————————
# Scheduled Tasks
# ————————
@tasks.loop(hours=168)
async def schedule_weekly_posts():
    await schedule_weekly_posts_function()

@tasks.loop(minutes=1)
async def send_reminders():
    london = pytz.timezone("Europe/London")
    now = datetime.now(london)
    for date_str, players in fireteams.items():
        try:
            # one hour before at 19:00
            dt = datetime.strptime(date_str, "%A, %d %B").replace(
                year=now.year, hour=19, minute=0
            )
            if now.strftime("%A, %d %B %H:%M") == dt.strftime("%A, %d %B %H:%M"):
                for uid in players:
                    user = await bot.fetch_user(uid)
                    await user.send(
                        "⏳ One hour to go! See you at 20:00 BST for Desert Perpetual."
                    )
                    scores[uid] = scores.get(uid, 0) + 1
        except Exception as e:
            logging.warning(f"Reminder error for '{date_str}': {e}")


# ————————
# Commands
# ————————
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


# ————————
# Run Bot
# ————————
print("» Using Discord token:", repr(token))
token = os.getenv("DISCORD_TOKEN")
if not token:
    logging.error("DISCORD_TOKEN is missing.")
    exit(1)
