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

# === Dice Game Scores ===
SCORES_FILE = "scores.json"
user_scores = {}

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

# === Raid Persistence ===
RAIDS_FILE = "raids.json"

def load_raids():
    global fireteams, backups
    try:
        with open(RAIDS_FILE, "r") as f:
            data = json.load(f)
            fireteams = {k: {int(slot): v for slot, v in d.items()}
                         for k, d in data.get("fireteams", {}).items()}
            backups   = {k: {int(slot): v for slot, v in d.items()}
                         for k, d in data.get("backups", {}).items()}
    except (FileNotFoundError, json.JSONDecodeError):
        fireteams = {}
        backups   = {}

def save_raids():
    with open(RAIDS_FILE, "w") as f:
        json.dump({
            "fireteams": {k: v for k, v in fireteams.items()},
            "backups":   {k: v for k, v in backups.items()}
        }, f)

# === Raid Data Structures ===
fireteams: dict[str, dict[int, int]] = {}  # { date_str: {slot_index: user_id} }
backups: dict[str, dict[int, int]] = {}    # { date_str: {slot_index: user_id} }
lock = asyncio.Lock()
slot_lock = asyncio.Lock()

# —————————————————————————————————————————
# Shared Helper: Build Raid Message Lines
# —————————————————————————————————————————
async def build_raid_lines(date_str: str) -> list[str]:
    # Ensure the dicts exist
    fire_slots   = fireteams.setdefault(date_str, {})
    backup_slots = backups.setdefault(date_str, {})

    lines = [
        "🔥 **CLAN RAID EVENT: Desert Perpetual** 🔥",
        "",
        f"📅 **Day:** {date_str} | 🕗 **Time:** 20:00 BST",
        "",
        "🎯 **Fireteam Lineup (6 Players):**"
    ]

    # Fireteam
    for i in range(6):
        uid = fire_slots.get(i)
        if uid:
            user = await get_cached_user(uid)
            mark = " ✅" if recent_changes.get(uid) == "joined" else ""
            lines.append(f"{i+1}. {user.display_name}{mark}")
        else:
            lines.append(f"{i+1}. Empty Slot")

    # Backups
    lines.extend(["", "🛡️ **Backup Players (2):**"])
    for i in range(2):
        uid = backup_slots.get(i)
        if uid:
            user = await get_cached_user(uid)
            mark = " ✅" if recent_changes.get(uid) == "joined" else ""
            lines.append(f"Backup {i+1}: {user.display_name}{mark}")
        else:
            lines.append(f"Backup {i+1}: Empty")

    # Footer
    lines.extend([
        "",
        "✅ React with a ✅ if you're joining the raid.",
        "❌ React with a ❌ if you can't make it.",
        "",
        "⚔️ Let’s assemble a legendary team and conquer the Desert Perpetual!"
    ])

    return lines

# —————————————————————————————————————————
# Debounced Embed Updates
# —————————————————————————————————————————
update_tasks: dict[int, asyncio.Task] = {}

def schedule_update(message_id: int, date_str: str):
    if message_id in update_tasks:
        update_tasks[message_id].cancel()
    update_tasks[message_id] = asyncio.create_task(_debounced_update(message_id, date_str))

async def _debounced_update(message_id: int, date_str: str):
    await asyncio.sleep(1)   # wait for other reactions to settle
    await update_raid_message(message_id, date_str)
    update_tasks.pop(message_id, None)

# In‐memory tracking for visual feedback & reminders
recent_changes: dict[int, str] = {}  # user_id → "joined" or "left"
previous_week_messages: list[int] = [] 
last_schedule_date = None

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
    lines = await build_raid_lines(date_str)
    return "\n".join(lines)

# —————————————————————————————————————————
# Scheduler: Run once each Sunday at 09:00 BST
# —————————————————————————————————————————
@tasks.loop(minutes=1)
async def sunday_scheduler():
    global last_schedule_date
    tz  = pytz.timezone("Europe/London")
    now = datetime.now(tz)

    # Only run once each Sunday at 09:00 BST
    if now.weekday() == 6 and now.hour == 9 and last_schedule_date != now.date():
        await schedule_weekly_posts_function()
        last_schedule_date = now.date()
    if now.weekday() != 6:
        last_schedule_date = None

async def schedule_weekly_posts_function():
    tz      = pytz.timezone("Europe/London")
    now     = datetime.now(tz)
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        logging.error(f"Could not find channel {CHANNEL_ID}")
        return

    # 1) Delete messages from last week
    for msg_id in previous_week_messages:
        try:
            old = await channel.fetch_message(msg_id)
            await old.delete()
        except discord.NotFound:
            pass
    previous_week_messages.clear()

    # 2) Compute the coming week’s date strings
    week_dates = {
        (now + timedelta(days=i)).strftime("%A, %d %B")
        for i in range(7)
    }

    # 3) Prune any dates outside this week, then persist
    for date_str in list(fireteams):
        if date_str not in week_dates:
            fireteams.pop(date_str, None)
            backups.pop(date_str,   None)
    save_raids()

    # 4) Detect which of this week’s dates are already posted
    posted_dates = set()
    async for m in channel.history(limit=200):
        if m.author == bot.user and "CLAN RAID EVENT" in m.content:
            match = re.search(r"\*\*Day:\*\*\s*(.+?)\s*\|", m.content)
            if match:
                posted_dates.add(match.group(1).strip())

    # 5) Post any missing days and record their IDs
    for date_str in sorted(week_dates):
        if date_str in posted_dates:
            continue

        fireteams.setdefault(date_str, {})
        backups.setdefault(date_str, {})

        content = await build_raid_message(date_str)
        msg     = await channel.send(content)
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")
        previous_week_messages.append(msg.id)

    # 6) Persist again so newly scheduled slots survive restarts
    save_raids()


# —————————————————————————————————————————
# Bot Events
# —————————————————————————————————————————
@bot.event
async def on_ready():
    load_timezones()
    load_raids()
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

async def handle_reaction_add(payload, member, message, date_str):
    async with slot_lock:
        fireteams.setdefault(date_str, {})
        backups.setdefault(date_str, {})

        already = member.id in fireteams[date_str].values() or member.id in backups[date_str].values()
        if already and not ALLOW_OVERWRITE:
            try:
                await member.send("You're already signed up. Remove your reaction first to change your slot.")
            except discord.Forbidden:
                logging.warning(f"Could not DM {member.display_name}")
            return

        if already and ALLOW_OVERWRITE:
            for d in (fireteams, backups):
                for slot, uid in list(d[date_str].items()):
                    if uid == member.id:
                        del d[date_str][slot]

        # Assign to fireteam first
        assigned = False
        for i in range(6):
            if i not in fireteams[date_str]:
                fireteams[date_str][i] = member.id
                assigned = True
                break

        # If full, assign to backup
        if not assigned:
            for i in range(2):
                if i not in backups[date_str]:
                    backups[date_str][i] = member.id
                    assigned = True
                    break

        recent_changes[member.id] = "joined"

        try:
            await member.send(f"✅ You’re confirmed for the raid on **{date_str}** at 20:00 BST!")
            if not assigned:
                await member.send("You're on the backup list for now — if a slot opens up, you'll be moved automatically!")
        except discord.Forbidden:
            logging.warning(f"Could not DM {member.display_name}")

        schedule_update(message.id, date_str)
        save_raids()



async def handle_reaction_remove(payload, member, message, date_str):
    async with slot_lock:
        fireteams.setdefault(date_str, {})
        backups.setdefault(date_str, {})

        # Remove user from both lists
        for slot, uid in list(fireteams[date_str].items()):
            if uid == member.id:
                del fireteams[date_str][slot]
        for slot, uid in list(backups[date_str].items()):
            if uid == member.id:
                del backups[date_str][slot]

        # Auto-promote backup if fireteam has space
        promoted_uid = None
        if len(fireteams[date_str]) < 6:
            for i in range(2):
                uid = backups[date_str].get(i)
                if uid and uid not in fireteams[date_str].values():
                    next_slot = max(fireteams[date_str].keys(), default=-1) + 1
                    fireteams[date_str][next_slot] = uid
                    del backups[date_str][i]
                    recent_changes[uid] = "joined"
                    promoted_uid = uid
                    break

        # Notify promoted user
        if promoted_uid:
            promoted_member = message.guild.get_member(promoted_uid)
            if promoted_member:
                try:
                    await promoted_member.send(f"You’ve been promoted to the fireteam for {date_str}! 🎉 Get ready to raid.")
                except discord.Forbidden:
                    pass

        recent_changes[member.id] = "left"
                
        schedule_update(message.id, date_str)
        save_raids()
          

@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return

    guild  = bot.get_guild(payload.guild_id)
    member = await guild.fetch_member(payload.user_id)
    emoji  = str(payload.emoji)

    # ✅ adds sign-up, ❌ triggers leave
    if emoji == "✅":
        handler = handle_reaction_add
    elif emoji == "❌":
        handler = handle_reaction_remove
    else:
        return

    async with lock:
        channel  = bot.get_channel(payload.channel_id)
        message  = await channel.fetch_message(payload.message_id)
        date_str = extract_date(message.content)
        if not date_str:
            return

    await handler(payload, member, message, date_str)


@bot.event
async def on_raw_reaction_remove(payload):
    if payload.user_id == bot.user.id:
        return

    guild  = bot.get_guild(payload.guild_id)
    member = await guild.fetch_member(payload.user_id)
    emoji  = str(payload.emoji)

    # Removing ✅ also means “leave”
    if emoji == "✅":
        handler = handle_reaction_remove
    else:
        return

    async with lock:
        channel  = bot.get_channel(payload.channel_id)
        message  = await channel.fetch_message(payload.message_id)
        date_str = extract_date(message.content)
        if not date_str:
            return

    await handler(payload, member, message, date_str)
    
async def update_raid_message(message_id: int, date_str: str):
    # give Discord a moment before patching
    await asyncio.sleep(0.3)

    channel = bot.get_channel(CHANNEL_ID)
    message = await channel.fetch_message(message_id)

    # build the fresh content
    lines = await build_raid_lines(date_str)

    # edit inside a try/except block
    try:
        await message.edit(content="\n".join(lines))
    except discord.HTTPException as e:
        logging.warning(f"Failed to edit raid message {message_id}: {e}")

    # clear visual‐flag markers
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
# Utility Functions for Dice game
# —————————————————————————————————————————

def load_scores():
    if os.path.exists(SCORES_FILE):
        with open(SCORES_FILE, "r") as f:
            return json.load(f)
    return {}

def save_scores(scores):
    with open(SCORES_FILE, "w") as f:
        json.dump(scores, f)

# 🧠 Global score store
user_scores = load_scores()
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
async def settimezone(ctx, *, tz_name: str = None):
    if not tz_name:
        return await ctx.send("❌ Usage: `!settimezone <Region/City>`")

    # Strip <>, replace spaces with slash, normalize casing
    cleaned   = tz_name.strip("<>").replace(" ", "/")
    region, _, city = cleaned.partition("/")
    tz_clean  = f"{region.capitalize()}/{city.title()}" if city else cleaned.title()

    try:
        # Validate against pytz
        pytz.timezone(tz_clean)
        user_timezones[str(ctx.author.id)] = tz_clean
        save_timezones()
        await ctx.send(f"✅ Timezone set to `{tz_clean}`.")
    except pytz.UnknownTimeZoneError:
        await ctx.send(
            "❌ Invalid timezone—try `Europe/Paris` or `America/New_York`."
        )

@bot.command()
async def mytimezone(ctx):
    tz = user_timezones.get(str(ctx.author.id))
    if tz:
        await ctx.send(f"🕒 Your timezone is set to `{tz}`.")
    else:
        await ctx.send("🌍 You haven’t set a timezone yet. Use `!settimezone <Region/City>` to set one.")

@bot.command()
async def roll(ctx):
    uid = str(ctx.author.id)
    user_scores.setdefault(uid, {"name": ctx.author.display_name, "score": 0})

    roll = random.randint(1, 6)
    user_scores[uid]["score"] += roll
    save_scores(user_scores)

    # 🎉 Reactions based on roll
    if roll == 6:
        reaction = "🔥 Critical hit!"
    elif roll == 1:
        reaction = "😬 Oof... better luck next time."
    else:
        reaction = "🎲 Nice roll!"

    await ctx.send(f"{ctx.author.mention} rolled a {roll}! Total score: {user_scores[uid]['score']}\n{reaction}")
    
@bot.command()
async def leaderboard(ctx):
    if not user_scores:
        await ctx.send("No scores yet! Be the first to roll 🎲")
        return

    # Sort by score descending
    top_players = sorted(user_scores.items(), key=lambda x: x[1]["score"], reverse=True)[:5]

    # Format leaderboard
    leaderboard_text = "**🏆 Weekly Dice Leaderboard 🏆**\n"
    for i, (uid, data) in enumerate(top_players, start=1):
        leaderboard_text += f"{i}. {data['name']} — {data['score']} points\n"

    await ctx.send(leaderboard_text)

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
