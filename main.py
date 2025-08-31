import os
import json
import discord
import logging
import pytz
import random

from discord.ext import commands, tasks
from datetime import datetime, timedelta
import asyncio
import re

# --- Setup Logging ---
logging.basicConfig(level=logging.INFO)

# --- Intents & Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
lock = asyncio.Lock()

# --- Persistence Config ---
DATA_FILE = "raid_data.json"

def load_data():
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

# --- Globals ---
CHANNEL_ID = 1209484610568720384  # your raid channel
EVENT_TITLE = "CLAN RAID EVENT: Desert Perpetual"

# --- Helper: Build or rebuild a raid embed ---
async def build_raid_embed(date_str, fire_ids, backup_ids):
    """Return an Embed with six main slots + two backups."""
    color = 0xE74C3C
    emb = discord.Embed(
        title=EVENT_TITLE,
        description=f"📅 Day: {date_str}  |  🕗 Time: 20:00 BST",
        color=color
    )

    # main slots 1–6
    for i in range(6):
        if i < len(fire_ids):
            member = await bot.fetch_user(fire_ids[i])
            label = f"{i+1}. {member.display_name}"
        else:
            label = f"{i+1}. Empty Slot"
        emb.add_field(name=label, value="\u200b", inline=False)

    # backups 1–2
    for i in range(2):
        if i < len(backup_ids):
            member = await bot.fetch_user(backup_ids[i])
            label = f"{i+1}. {member.display_name}"
        else:
            label = f"{i+1}. Empty Slot"
        emb.add_field(name=label, value="\u200b", inline=False)

    emb.set_footer(text="✅ react to join   ❌ react to leave")
    return emb

# --- Weekly Schedule Poster ---
async def schedule_weekly_posts_function():
    tz = pytz.timezone("Europe/London")
    now = datetime.now(tz)
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        logging.error(f"Channel {CHANNEL_ID} not found")
        return

    data = load_data()

    # every Sunday delete last week's posts
    if now.weekday() == 6:
        async for msg in channel.history(limit=100):
            if msg.author == bot.user and msg.embeds:
                if msg.embeds[0].title == EVENT_TITLE:
                    await msg.delete()
        # optionally reset data for older dates
        # data = {k: v for k, v in data.items() if ...}

    # post for next 7 days
    for delta in range(7):
        day = now + timedelta(days=delta)
        date_str = day.strftime("%A, %d %B")

        # skip if already in data and message exists
        exists = False
        async for msg in channel.history(limit=100):
            if msg.embeds and msg.embeds[0].title == EVENT_TITLE:
                if date_str in msg.embeds[0].description:
                    exists = True
                    break
        if exists:
            continue

        # init persistent slots
        data.setdefault(date_str, {"fireteams": [], "backups": []})
        await asyncio.sleep(0.5)  # rate-limit buffer

        # send embed and add reactions
        emb = await build_raid_embed(date_str,
                                     data[date_str]["fireteams"],
                                     data[date_str]["backups"])
        msg = await channel.send("@everyone", embed=emb)
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")

    save_data(data)

# --- Check & Recover Missed Schedule ---
async def check_missed_schedule():
    tz = pytz.timezone("Europe/London")
    now = datetime.now(tz)
    # if Sunday after 09:00 BST
    if now.weekday() == 6 and now.hour >= 9:
        await schedule_weekly_posts_function()

# --- Reaction Add Handler ---
@bot.event
async def on_raw_reaction_add(payload):
    # ignore bot
    if payload.user_id == bot.user.id:
        return

    # only our channel
    if payload.channel_id != CHANNEL_ID:
        return

    # only ✅ / ❌
    if payload.emoji.name not in ("✅", "❌"):
        return

    channel = bot.get_channel(payload.channel_id)
    message = await channel.fetch_message(payload.message_id)

    # must be our raid embed
    if not message.embeds or message.embeds[0].title != EVENT_TITLE:
        return

    # parse date from embed description
    desc = message.embeds[0].description
    m = re.search(r"Day:\s*(.+?)\s*\|", desc)
    if not m:
        return
    date_str = m.group(1).strip()

    # load & init data
    async with lock:
        data = load_data()
        raid = data.setdefault(date_str, {"fireteams": [], "backups": []})
        ft = raid["fireteams"]
        bu = raid["backups"]

        # fetch member
        guild = bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id)

        # join
        if payload.emoji.name == "✅":
            if member.id in ft or member.id in bu:
                return
            if len(ft) < 6:
                ft.append(member.id)
                await member.send(f"🎉 You’re locked in for {date_str} @ 20:00 BST!")
            elif len(bu) < 2:
                bu.append(member.id)
                await member.send(f"🛡️ You’re on backup for {date_str} @ 20:00 BST.")
            else:
                await member.send(f"⚠️ Raid on {date_str} is full (6 + 2).")

        # leave
        else:
            removed = False
            if member.id in ft:
                ft.remove(member.id)
                removed = True
            if member.id in bu:
                bu.remove(member.id)
                removed = True
            if removed:
                await member.send(f"❌ You’ve left the raid for {date_str}.")

        save_data(data)

    # rebuild embed
    new_emb = await build_raid_embed(date_str, ft, bu)
    await message.edit(embed=new_emb)

# --- Reaction Remove Handler ---
@bot.event
async def on_raw_reaction_remove(payload):
    # mirror add logic for freeing slots
    if payload.user_id == bot.user.id:
        return
    if payload.channel_id != CHANNEL_ID:
        return
    if payload.emoji.name not in ("✅", "❌"):
        return

    channel = bot.get_channel(payload.channel_id)
    message = await channel.fetch_message(payload.message_id)
    if not message.embeds or message.embeds[0].title != EVENT_TITLE:
        return

    m = re.search(r"Day:\s*(.+?)\s*\|", message.embeds[0].description)
    if not m:
        return
    date_str = m.group(1).strip()

    async with lock:
        data = load_data()
        raid = data.get(date_str)
        if not raid:
            return
        ft = raid["fireteams"]
        bu = raid["backups"]

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
            await member.send(f"❌ You’ve left the raid for {date_str}.")
            save_data(data)

            new_emb = await build_raid_embed(date_str, ft, bu)
            await message.edit(embed=new_emb)

# --- On Bot Ready ---
@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user} — scheduling tasks.")
    schedule_weekly_posts.start()
    send_reminders.start()
    await check_missed_schedule()

# --- Weekly Posting Task ---
@tasks.loop(hours=168)
async def schedule_weekly_posts():
    await schedule_weekly_posts_function()

# --- Reminder Task ---
@tasks.loop(minutes=1)
async def send_reminders():
    tz = pytz.timezone("Europe/London")
    now = datetime.now(tz)
    data = load_data()

    for date_str, raid in data.items():
        try:
            # parse date and set to 19:00
            dt = datetime.strptime(date_str, "%A, %d %B").replace(
                year=now.year, hour=19, minute=0)
            if now.strftime("%A, %d %B %H:%M") == dt.strftime("%A, %d %B %H:%M"):
                # one hour before at 19:00
                for uid in raid["fireteams"]:
                    user = await bot.fetch_user(uid)
                    await user.send(
                        "⏳ One hour to go! See you at 20:00 BST for Desert Perpetual."
                    )
        except Exception as e:
            logging.warning(f"Reminder parse error for '{date_str}': {e}")

# --- Raid Leaderboard Command ---
@bot.command(name="Raidleaderboard")
async def Raidleaderboard(ctx):
    data = load_data()
    scores = {}
    # award 1pt per confirmed raid in past (optional: track separately)
    for raid in data.values():
        for uid in raid["fireteams"]:
            scores[uid] = scores.get(uid, 0) + 1

    if not scores:
        return await ctx.send("No raid participation recorded yet.")

    sorted_lead = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    lines = [f"**{await bot.fetch_user(uid)}**: {pts}" for uid, pts in sorted_lead]
    await ctx.send("🏆 **Raid Leaderboard** 🏆\n" + "\n".join(lines))

# --- Dice Game Commands (unchanged) ---
user_scores = {}

@bot.command(name="roll")
async def roll_dice(ctx, sides: int = 6):
    if sides < 2:
        return await ctx.send("Dice must have at least 2 sides!")
    result = random.randint(1, sides)
    uid = str(ctx.author.id)
    user_scores.setdefault(uid, {"name": ctx.author.display_name, "score": 0})
    user_scores[uid]["score"] += result
    await ctx.send(
        f"🎲 {ctx.author.display_name} rolled a {result} on a {sides}-sided die! "
        f"Total score: {user_scores[uid]['score']}"
    )

@bot.command(name="leaderboard")
async def show_leaderboard(ctx):
    if not user_scores:
        return await ctx.send("No scores yet! Roll the dice with `!roll`.")
    sorted_us = sorted(user_scores.values(), key=lambda x: x["score"], reverse=True)
    msg = "**🏆 Dice Leaderboard 🏆**\n"
    for i, player in enumerate(sorted_us[:5], 1):
        msg += f"{i}. {player['name']} – {player['score']} pts\n"
    await ctx.send(msg)

# --- Run Bot ---
token = os.getenv("DISCORD_TOKEN")
if not token:
    logging.error("DISCORD_TOKEN missing in env.")
    exit(1)

bot.run(token)




