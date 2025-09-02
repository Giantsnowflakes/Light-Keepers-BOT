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
user_cache = {}

async def get_cached_user(uid):
    if uid not in user_cache:
        user_cache[uid] = await bot.fetch_user(uid)
    return user_cache[uid]

user_timezones = {}  # { user_id: 'Europe/London' }

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

# Globals
recent_changes = {}           # Tracks who just joined or left for visual feedback
scores = {}                   # { user_id: points }
user_scores = {}              # dice‐game scores
previous_week_messages = []   # IDs of last Sunday’s 7 posts
last_schedule_date = None     # to ensure one run/Sunday
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

    # Fireteam slots
    for i in range(6):
        uid = fire_slots.get(i)
        if uid:
            user = await get_cached_user(uid)
            lines.append(f"{i+1}. {user.display_name}")
        else:
            lines.append(f"{i+1}. Empty Slot")

    lines.append("")
    lines.append("🛡️ **Backup Players (2):**")

    # Backup slots
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

        # ✅ Safety check: convert old list data to dict
        if isinstance(fireteams.get(date_str), list):
            fireteams[date_str] = {}
        if isinstance(backups.get(date_str), list):
            backups[date_str] = {}

        # ✅ Ensure slot-based dicts
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

member_cache = {}

def is_already_signed_up(date_str, member_id):
    return member_id in fireteams[date_str].values() or member_id in backups[date_str].values()

async def get_member(guild, user_id):
    if user_id in member_cache:
        return member_cache[user_id]
    member = guild.get_member(user_id)
    if not member:
        member = await guild.fetch_member(user_id)
    member_cache[user_id] = member
    return member

async def notify_invalid_emoji(member):
    try:
        await member.send("Only ✅ or ❌ are used for raid signups. Try again with the right emoji!")
    except discord.Forbidden:
        logging.warning(f"Could not DM {member.display_name}")

@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return

    guild = bot.get_guild(payload.guild_id)
    member = await get_member(guild, payload.user_id)
    emoji = str(payload.emoji)

    # ✅ Emoji validation
    if emoji not in ["✅", "❌"]:
        await notify_invalid_emoji(member)
        return

    async with lock:
        channel = bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        date_str = extract_date(message.content)
        if not date_str:
            print("Could not extract date from message.")
            return

        fireteams.setdefault(date_str, {})
        backups.setdefault(date_str, {})

        print(f"Reaction added: {emoji} by {member.display_name}")

        if emoji == "❌":
            print(f"{member.display_name} opted out. Removing from slots...")
            for slot, uid in list(fireteams[date_str].items()):
                if uid == member.id:
                    del fireteams[date_str][slot]
            for slot, uid in list(backups[date_str].items()):
                if uid == member.id:
                    del backups[date_str][slot]
            recent_changes[member.id] = "left"

        elif emoji == "✅":
            if is_already_signed_up(date_str, member.id):
                if not ALLOW_OVERWRITE:
                    await member.send("You're already signed up. Remove your reaction first to change your slot.")
                    return
                else:
                    print(f"{member.display_name} is already assigned, but overwrite is allowed.")

            assigned = False

            # Assign to first available fireteam slot
            for i in range(6):
                if i not in fireteams[date_str]:
                    fireteams[date_str][i] = member.id
                    print(f"{member.display_name} assigned to fireteam slot {i+1} ✨")
                    recent_changes[member.id] = "joined"
                    assigned = True
                    break

            # If fireteam full, assign to backup
            if not assigned:
                for i in range(2):
                    if i not in backups[date_str]:
                        backups[date_str][i] = member.id
                        print(f"{member.display_name} assigned to backup slot {i+1} ✨")
                        recent_changes[member.id] = "joined"
                        assigned = True
                        break
                if assigned:
                    await member.send("You're on the backup list for now — if a slot opens up, you'll be moved automatically!")

            if assigned:
                try:
                    await member.send(f"✅ You’re confirmed for the raid on **{date_str}** at 20:00 BST!")
                except discord.Forbidden:
                    logging.warning(f"Could not DM {member.display_name}")

        # Final update
        await update_raid_message(payload.message_id, date_str)
@bot.event
async def on_raw_reaction_remove(payload):
    if payload.user_id == bot.user.id:
        return

    guild = bot.get_guild(payload.guild_id)
    member = await guild.fetch_member(payload.user_id)
    emoji = str(payload.emoji)

    async with lock:
        channel = bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        date_str = extract_date(message.content)
        if not date_str:
            print("Could not extract date from message.")
            return

        fireteams.setdefault(date_str, {})
        backups.setdefault(date_str, {})

        print(f"Reaction removed: {emoji} by {member.display_name}")

        if emoji in ["✅", "❌"]:
            for slot, uid in list(fireteams[date_str].items()):
                if uid == member.id:
                    del fireteams[date_str][slot]
            for slot, uid in list(backups[date_str].items()):
                if uid == member.id:
                    del backups[date_str][slot]

            await update_raid_message(payload.message_id, date_str)

async def update_raid_message(message_id, date_str):
    channel = bot.get_channel(CHANNEL_ID)
    message = await channel.fetch_message(message_id)

    fireteams.setdefault(date_str, {})
    backups.setdefault(date_str, {})

    lines = [
        "🔥 **CLAN RAID EVENT: Desert Perpetual** 🔥",
        "",
        f"📅 **Day:** {date_str} | 🕗 **Time:** 20:00 BST",
        "",
        "🎯 **Fireteam Lineup (6 Players):**"
    ]

    # Fireteam slots
    for i in range(6):
        uid = fireteams[date_str].get(i)
        if uid:
            user = await get_cached_user(uid)
            name = user.display_name
            if recent_changes.get(uid) == "joined":
                lines.append(f"{i+1}. {name} ✅")
            else:
                lines.append(f"{i+1}. {name}")
        else:
            left_uid = next((uid for uid, status in recent_changes.items() if status == f"left_{i}"), None)
            if left_uid:
                lines.append(f"{i+1}. ❌ (just left)")
            else:
                lines.append(f"{i+1}. Empty Slot")

    lines.append("")
    lines.append("🛡️ **Backup Players (2):**")

    # Backup slots
    for i in range(2):
        uid = backups[date_str].get(i)
        if uid:
            user = await get_cached_user(uid)
            name = user.display_name
            if recent_changes.get(uid) == "joined":
                lines.append(f"Backup {i+1}: {name} ✅")
            else:
                lines.append(f"Backup {i+1}: {name}")
        else:
            left_uid = next((uid for uid, status in recent_changes.items() if status == f"left_b{i}"), None)
            if left_uid:
                lines.append(f"Backup {i+1}: ❌ (just left)")
            else:
                lines.append(f"Backup {i+1}: Empty")

    lines.extend([
        "",
        "✅ React with a ✅ if you're joining the raid.",
        "❌ React with a ❌ if you can't make it.",
        "",
        "⚔️ Let’s assemble a legendary team and conquer the Desert Perpetual!"
    ])

    await message.edit(content="\n".join(lines))

    recent_changes.clear()  # Reset visual flags after update

async def reminder_loop():
    await bot.wait_until_ready()
    tz = pytz.timezone("Europe/London")

    while not bot.is_closed():
        now = datetime.now(tz)

        for date_str in list(fireteams.keys()):
            try:
                # Find the original raid message for this date to extract the EVENT NAME
                channel = bot.get_channel(CHANNEL_ID)
                event_name = "the raid"

                async for msg in channel.history(limit=200):
                    if msg.author == bot.user and date_str in msg.content:
                        # Look for the first line starting with 🔥 **CLAN RAID EVENT:
                        for line in msg.content.splitlines():
                            if line.strip().startswith("🔥 **CLAN RAID EVENT:"):
                                # Extract the text between the colon and the final ** or emoji
                                event_name = line.split("CLAN RAID EVENT:", 1)[1].strip(" 🔥*")
                                break
                        break

                # Parse the raid start time
            raid_dt = datetime.strptime(date_str, "%A, %d %B").replace(
                year=now.year, hour=20, minute=0, tzinfo=tz
            )
            if raid_dt < now:
                raid_dt = raid_dt.replace(year=now.year + 1)  # handles year rollover

                
            except ValueError:
                continue

            delta = raid_dt - now
            if 59 <= delta.total_seconds() / 60 <= 61:  # ~1 hour before
                for uid in list(fireteams[date_str].values()) + list(backups[date_str].values()):
                    try:
                        user = await bot.fetch_user(uid)
                        user_tz = pytz.timezone(user_timezones.get(str(uid), "Europe/London"))
                        local_time = raid_dt.astimezone(user_tz)
                        event_time_str = local_time.strftime('%H:%M %Z')

                        await user.send(
                            f"⏰ **One hour to glory!**\n"
                            f"🔥 The **{event_name}** kicks off on **{date_str}** at **{local_time_str}**.\n"
                            f"🛡️ Gear up, rally your fireteam, and be ready to make history!"
                        )
                    except discord.Forbidden:
                        logging.warning(f"Could not DM {user.display_name}")

        await asyncio.sleep(60)  # Check every minute

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
            user = await get_cached_user(uid)
            lines.append(f"{i+1}. {user.display_name}")
        else:
            lines.append(f"{i+1}. Empty Slot")

    # Backup slots
    lines.append("\nBackups:")
    for i in range(2):
        uid = backups.get(date_str, {}).get(i)
        if uid:
            user = await get_cached_user(uid)
            lines.append(f"Backup {i+1}: {user.display_name}")
        else:
            lines.append(f"Backup {i+1}: Empty")

    await ctx.send("\n".join(lines))

@bot.command()
async def settimezone(ctx, tz_name):
    try:
        pytz.timezone(tz_name)  # validate
        user_timezones[str(ctx.author.id)] = tz_name
        save_timezones()
        await ctx.send(f"✅ Timezone set to `{tz_name}` for {ctx.author.display_name}")
    except pytz.UnknownTimeZoneError:
        await ctx.send("❌ Invalid timezone name. Try something like `Europe/Paris` or `America/New_York`.")

@bot.command()
async def mytimezone(ctx):
    tz = user_timezones.get(str(ctx.author.id))
    if tz:
        await ctx.send(f"🕒 Your timezone is set to `{tz}`.")
    else:
        await ctx.send("🌍 You haven’t set a timezone yet. Use `!settimezone <Region/City>` to set one.")

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
