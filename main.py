import os
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import pytz
import random
import logging
import re
import asyncio
import json

# === Configuration ===
ALLOW_OVERWRITE = False  # Toggle for slot overwrite protection

# === Caching & Timezones ===
user_cache: dict[int, discord.User] = {}
async def get_cached_user(uid: int) -> discord.User:
    if uid not in user_cache:
        user_cache[uid] = await bot.fetch_user(uid)
    return user_cache[uid]

user_timezones: dict[str, str] = {}       # { user_id: 'Europe/London' }
TIMEZONE_FILE = "user_timezones.json"

def load_timezones():
    global user_timezones
    try:
        with open(TIMEZONE_FILE, "r") as f:
            user_timezones = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        user_timezones = {}

def save_timezones():
    with open(TIMEZONE_FILE, "w") as f:
        json.dump(user_timezones, f)

# === Raid Data Structures ===
fireteams: dict[str, dict[int, int]] = {}  # { date_str: {slot_index: user_id} }
backups: dict[str, dict[int, int]] = {}    # { date_str: {slot_index: user_id} }
lock = asyncio.Lock()

# In‐memory tracking for visual feedback & reminders
recent_changes: dict[int, str] = {}  # user_id → "joined" or "left"
previous_week_messages: list[int] = [] 
last_schedule_date: datetime.date | None = None

# === Logging & Intents ===
logging.basicConfig(level=logging.INFO)
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
CHANNEL_ID = 1209484610568720384  # your raid channel ID

# —————————————————————————————————————————
# Helper: Build the exact raid message text
# —————————————————————————————————————————
async def build_raid_message(date_str: str) -> str:
    fire_slots = fireteams.get(date_str, {})
    backup_slots = backups.get(date_str, {})
    lines = [
        "🔥 **CLAN RAID EVENT: Desert Perpetual** 🔥",
        "",
        f"📅 **Day:** {date_str} | 🕗 **Time:** 20:00 BST",
        "",
        "🎯 **Fireteam Lineup (6 Players):**"
    ]
    for i in range(6):
        uid = fire_slots.get(i)
        if uid:
            user = await get_cached_user(uid)
            lines.append(f"{i+1}. {user.display_name}")
        else:
            lines.append(f"{i+1}. Empty Slot")

    lines.append("")
    lines.append("🛡️ **Backup Players (2):**")
    for i in range(2):
        uid = backup_slots.get(i)
        if uid:
            user = await get_cached_user(uid)
            lines.append(f"Backup {i+1}: {user.display_name}")
        else:
            lines.append(f"Backup {i+1}: Empty")

    lines.extend([
        "",
        "✅ React with a ✅ if you're joining the raid.",
        "❌ React with a ❌ if you can't make it.",
        "",
        "⚔️ Let’s assemble a legendary team and conquer the Desert Perpetual!"
    ])
    return "\n".join(lines)

# —————————————————————————————————————————
# Scheduler: Run once each Sunday at 09:00 BST
# —————————————————————————————————————————
@tasks.loop(minutes=1)
async def sunday_scheduler():
    global last_schedule_date
    tz = pytz.timezone("Europe/London")
    now = datetime.now(tz)
    if now.weekday() == 6 and now.hour == 9 and last_schedule_date != now.date():
        await schedule_weekly_posts_function()
        last_schedule_date = now.date()
    if now.weekday() != 6:
        last_schedule_date = None

async def schedule_weekly_posts_function():
    tz = pytz.timezone("Europe/London")
    now = datetime.now(tz)
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        logging.error(f"Could not find channel {CHANNEL_ID}")
        return

    # Delete old week
    for msg_id in previous_week_messages:
        try:
            old = await channel.fetch_message(msg_id)
            await old.delete()
        except discord.NotFound:
            pass
    previous_week_messages.clear()

    # Avoid duplicate dates
    posted_dates = set()
    async for m in channel.history(limit=200):
        if m.author == bot.user and "CLAN RAID EVENT" in m.content:
            match = re.search(r"\*\*Day:\*\*\s*(.+?)\s*\|", m.content)
            if match:
                posted_dates.add(match.group(1).strip())

    # Post next 7 days
    for delta in range(7):
        raid_dt = now + timedelta(days=delta)
        date_str = raid_dt.strftime("%A, %d %B")
        if date_str in posted_dates:
            continue

        fireteams.setdefault(date_str, {})
        backups.setdefault(date_str, {})

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
    load_timezones()
    logging.info(f"Bot started as {bot.user}")
    sunday_scheduler.start()
    print(f"Logged in as {bot.user}")
    asyncio.create_task(reminder_loop())

    # On cold start, backfill any existing posts
    if not previous_week_messages:
        logging.info("No existing raid posts found on startup – posting initial week block.")
        await schedule_weekly_posts_function()

@bot.event
async def on_resumed():
    logging.info("Session RESUMED → checking for missing raid posts")
    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        previous_week_messages.clear()
        async for m in channel.history(limit=200):
            if m.author == bot.user and "CLAN RAID EVENT" in m.content:
                previous_week_messages.append(m.id)
        if not previous_week_messages:
            logging.info("No raid posts found on resume → posting week block now")
            await schedule_weekly_posts_function()

# —————————————————————————————————————————
# Reaction Handling: ✅ join / ❌ leave
# —————————————————————————————————————————
def extract_date(content: str) -> str | None:
    m = re.search(r"\*\*Day:\*\*\s*(.+?)\s*\|", content)
    return m.group(1).strip() if m else None

@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return

    guild = bot.get_guild(payload.guild_id)
    member = await guild.fetch_member(payload.user_id)
    emoji = str(payload.emoji)

    if emoji not in ["✅", "❌"]:
        try:
            await member.send("Only ✅ or ❌ are used for raid signups. Try again with the right emoji!")
        except discord.Forbidden:
            logging.warning(f"Could not DM {member.display_name}")
        return

    async with lock:
        channel = bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        date_str = extract_date(message.content)
        if not date_str:
            return

        fireteams.setdefault(date_str, {})
        backups.setdefault(date_str, {})
        logging.info(f"Reaction {emoji} by {member.display_name} on {date_str}")

        # ❌ remove from both lists
        if emoji == "❌":
            for slot, uid in list(fireteams[date_str].items()):
                if uid == member.id:
                    del fireteams[date_str][slot]
            for slot, uid in list(backups[date_str].items()):
                if uid == member.id:
                    del backups[date_str][slot]
            recent_changes[member.id] = "left"

        # ✅ assign
        if emoji == "✅":
            already = member.id in fireteams[date_str].values() or member.id in backups[date_str].values()
            if already and not ALLOW_OVERWRITE:
                try:
                    await member.send("You're already signed up. Remove your reaction first to change your slot.")
                except discord.Forbidden:
                    logging.warning(f"Could not DM {member.display_name}")
                return

            if already and ALLOW_OVERWRITE:
                # remove old slot then proceed
                for d in (fireteams, backups):
                    for slot, uid in list(d[date_str].items()):
                        if uid == member.id:
                            del d[date_str][slot]

            # fill a fireteam slot first
            assigned = False
            for i in range(6):
                if i not in fireteams[date_str]:
                    fireteams[date_str][i] = member.id
                    assigned = True
                    break

            # if full, go to backups
            if not assigned:
                for i in range(2):
                    if i not in backups[date_str]:
                        backups[date_str][i] = member.id
                        assigned = True
                        break

            recent_changes[member.id] = "joined"
            # send DMs
            try:
                await member.send(f"✅ You’re confirmed for the raid on **{date_str}** at 20:00 BST!")
                if not assigned:
                    await member.send("You're on the backup list for now — if a slot opens up, you'll be moved automatically!")
            except discord.Forbidden:
                logging.warning(f"Could not DM {member.display_name}")

        # finally, update the message
        await update_raid_message(message.id, date_str)

@bot.event
async def on_raw_reaction_remove(payload):
    if payload.user_id == bot.user.id:
        return

    guild = bot.get_guild(payload.guild_id)
    member = await guild.fetch_member(payload.user_id)
    emoji = str(payload.emoji)

    if emoji not in ["✅", "❌"]:
        return

    async with lock:
        channel = bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        date_str = extract_date(message.content)
        if not date_str:
            return

        for slot, uid in list(fireteams[date_str].items()):
            if uid == member.id:
                del fireteams[date_str][slot]
        for slot, uid in list(backups[date_str].items()):
            if uid == member.id:
                del backups[date_str][slot]

        recent_changes[member.id] = "left"
        await update_raid_message(message.id, date_str)

async def update_raid_message(message_id: int, date_str: str):
    channel = bot.get_channel(CHANNEL_ID)
    message = await channel.fetch_message(message_id)

    fireteams.setdefault(date_str, {})
    backups.setdefault(date_str, {})

    # rebuild content (same as build_raid_message but with visual flags)
    lines = [
        "🔥 **CLAN RAID EVENT: Desert Perpetual** 🔥",
        "",
        f"📅 **Day:** {date_str} | 🕗 **Time:** 20:00 BST",
        "",
        "🎯 **Fireteam Lineup (6 Players):**"
    ]
    for i in range(6):
        uid = fireteams[date_str].get(i)
        if uid:
            user = await get_cached_user(uid)
            mark = " ✅" if recent_changes.get(uid) == "joined" else ""
            lines.append(f"{i+1}. {user.display_name}{mark}")
        else:
            lines.append(f"{i+1}. Empty Slot")

    lines.append("")
    lines.append("🛡️ **Backup Players (2):**")
    for i in range(2):
        uid = backups[date_str].get(i)
        if uid:
            user = await get_cached_user(uid)
            mark = " ✅" if recent_changes.get(uid) == "joined" else ""
            lines.append(f"Backup {i+1}: {user.display_name}{mark}")
        else:
            lines.append(f"Backup {i+1}: Empty")

    lines.extend([
        "",
        "✅ React with a ✅ if you're joining the raid.",
        "❌ React with a ❌ if you can't make it.",
        "",
        "⚔️ Let’s assemble a legendary team and conquer the Desert Perpetual!"
    ])

    try:
        await message.edit(content="\n".join(lines))
    except discord.HTTPException as e:
        logging.warning(f"Failed to edit raid message {message_id}: {e}")

    recent_changes.clear()

# —————————————————————————————————————————
# Reminder Loop: 1 hour before each raid
# —————————————————————————————————————————
async def reminder_loop():
    await bot.wait_until_ready()
    tz = pytz.timezone("Europe/London")
    while not bot.is_closed():
        now = datetime.now(tz)
        for date_str, team in fireteams.items():
            try:
                raid_dt = datetime.strptime(date_str, "%A, %d %B")
                raid_dt = raid_dt.replace(year=now.year, hour=20, minute=0, tzinfo=tz)
                if raid_dt < now:
                    raid_dt = raid_dt.replace(year=now.year + 1)
            except ValueError:
                continue

            delta_minutes = (raid_dt - now).total_seconds() / 60
            if 59 <= delta_minutes <= 61:
                # find event name
                channel = bot.get_channel(CHANNEL_ID)
                event_name = "the raid"
                async for m in channel.history(limit=200):
                    if m.author == bot.user and date_str in m.content:
                        for line in m.content.splitlines():
                            if line.startswith("🔥 **CLAN RAID EVENT:"):
                                event_name = line.split("CLAN RAID EVENT:", 1)[1].strip(" 🔥*")
                                break
                        break

                local_members = list(team.values()) + list(backups.get(date_str, {}).values())
                for uid in local_members:
                    try:
                        user = await bot.fetch_user(uid)
                        user_tz = pytz.timezone(user_timezones.get(str(uid), "Europe/London"))
                        local_time = raid_dt.astimezone(user_tz)
                        event_time_str = local_time.strftime('%H:%M %Z')
                        await user.send(
                            f"⏰ **One hour to glory!**\n"
                            f"🔥 The **{event_name}** kicks off on **{date_str}** at **{event_time_str}**.\n"
                            f"🛡️ Gear up, rally your fireteam, and be ready to make history!"
                        )
                    except discord.Forbidden:
                        logging.warning(f"Could not DM user {uid}")
        await asyncio.sleep(60)

# —————————————————————————————————————————
# Commands (unchanged)
# —————————————————————————————————————————
@bot.command(name="Raidleaderboard")
async def Raidleaderboard(ctx):
    # … your existing code …

@bot.command(name="showlineup")
async def show_lineup(ctx, *, date_str: str):
    # … your existing code …

@bot.command()
async def settimezone(ctx, tz_name):
    # … your existing code …

@bot.command()
async def mytimezone(ctx):
    # … your existing code …

@bot.command(name="roll")
async def roll_dice(ctx, sides: int = 6):
    # … your existing code …

@bot.command(name="leaderboard")
async def show_leaderboard(ctx):
    # … your existing code …

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
