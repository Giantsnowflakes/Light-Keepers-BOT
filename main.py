import os
import discord
import pytz
import random
import logging
import re
import asyncio
import json
import time
from collections import deque
from discord.ext import commands, tasks
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from discord import Message, User

# ─────────────────────────────────────────────────────
# Rate‐limit helper for embed edits
# ─────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self, max_calls: int, per: float):
        self.max_calls = max_calls
        self.per       = per
        self.calls     = deque()

    async def wait(self):
        now = time.monotonic()
        # drop timestamps older than our window
        while self.calls and now - self.calls[0] > self.per:
            self.calls.popleft()

        if len(self.calls) >= self.max_calls:
            to_wait = self.per - (now - self.calls[0])
            await asyncio.sleep(to_wait)

        self.calls.append(time.monotonic())

# one global instance—you’ll call edit_limiter.wait() before each embed.edit()
edit_limiter = RateLimiter(max_calls=5, per=5.0)

# ─────────────────────────────────────────────────────
# Badge System Persistence & Definitions
# ─────────────────────────────────────────────────────
BADGES_FILE = "badges.json"

# Stats keys → total counts for each user
user_stats: dict[str, dict[str,int]]  = {}  # e.g. {"1234": {"raids_joined": 7, "promotions": 2}}

# Which badges each user has earned
user_badges: dict[str, list[str]]      = {}  # e.g. {"1234": ["consecutive_raider_5", "backup_champion_3"]}

# Badge definitions: key → name, emoji, and the stat threshold
BADGE_DEFINITIONS = {
    "raid_veteran_10": {
        "name": "Raid Veteran",
        "emoji": "🏆",
        "threshold": {"stats_key": "raids_joined", "value": 10}
    },
    "backup_champion_3": {
        "name": "Backup Champion",
        "emoji": "🛡️",
        "threshold": {"stats_key": "promotions", "value": 3}
    },
    # add more badges here...
}

def load_badges():
    global user_stats, user_badges
    try:
        with open(BADGES_FILE, "r") as f:
            data = json.load(f)
            user_stats  = data.get("stats", {})
            user_badges = data.get("badges", {})
    except (FileNotFoundError, json.JSONDecodeError):
        user_stats  = {}
        user_badges = {}

def save_badges():
    with open(BADGES_FILE, "w") as f:
        json.dump({"stats": user_stats, "badges": user_badges}, f)

async def check_for_new_badges(member: discord.User, stats: dict[str,int]):
    """
    Looks at stats, awards any badges not yet given, DMs the user.
    """
    uid = str(member.id)
    earned = user_badges.setdefault(uid, [])
    for key, badge in BADGE_DEFINITIONS.items():
        skey  = badge["threshold"]["stats_key"]
        need  = badge["threshold"]["value"]
        if stats.get(skey, 0) >= need and key not in earned:
            earned.append(key)
            # DM them their new badge
            try:
                await member.send(
                    f"🎉 **Congratulations!** You earned the {badge['emoji']} **{badge['name']}** badge!"
                )
            except discord.Forbidden:
                logging.warning(f"Could not DM badge to {member.display_name}")

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
lock       = asyncio.Lock()
slot_lock  = asyncio.Lock()
last_schedule_date = None
recent_changes: dict[int, str] = {}
previous_week_messages: list[int] = []

# ─────────────────────────────────────────────────────
# Extract date from an embed’s hidden field  
# ─────────────────────────────────────────────────────
def extract_date(message: discord.Message) -> str | None:
    if not message.embeds:
        return None
    embed = message.embeds[0]
    if not embed.fields:
        return None
    return embed.fields[0].value

# —————————————————————————————————————————
# Shared Helper: Build Raid Message Lines
# —————————————————————————————————————————
async def build_raid_lines(date_str: str) -> list[str]:
    # Ensure the dicts exist
    fire_slots   = fireteams.setdefault(date_str, {})
    backup_slots = backups.setdefault(date_str, {})

    lines = [
        f"📅 **Day:** {date_str} | 🕗 **Time:** 20:00 BST",
        "",
        "🎯 **Fireteam Lineup (6 Players):**"
    ]

    # Fireteam slots
    for i in range(6):
        uid = fire_slots.get(i)
        if not uid:
            lines.append(f"{i+1}. Empty Slot")
            continue

        try:
            user = await get_cached_user(uid)
        except Exception as e:
            logging.warning(f"Could not fetch user {uid}: {e}")
            lines.append(f"{i+1}. Unknown User")
            continue

        mark = " ✅" if recent_changes.get(uid) == "joined" else ""
        badge_emojis = [
            BADGE_DEFINITIONS[b]["emoji"]
            for b in user_badges.get(str(uid), [])
        ]
        badge_str = " " + "".join(badge_emojis) if badge_emojis else ""
        lines.append(f"{i+1}. {user.display_name}{mark}{badge_str}")

    # Backup slots
    lines.extend(["", "🛡️ **Backup Players (2):**"])
    for i in range(2):
        uid = backup_slots.get(i)
        if not uid:
            lines.append(f"Backup {i+1}: Empty")
            continue

        try:
            user = await get_cached_user(uid)
        except Exception as e:
            logging.warning(f"Could not fetch backup user {uid}: {e}")
            lines.append(f"Backup {i+1}: Unknown User")
            continue

        mark = " ✅" if recent_changes.get(uid) == "joined" else ""
        badge_emojis = [
            BADGE_DEFINITIONS[b]["emoji"]
            for b in user_badges.get(str(uid), [])
        ]
        badge_str = " " + "".join(badge_emojis) if badge_emojis else ""
        lines.append(f"Backup {i+1}: {user.display_name}{mark}{badge_str}")

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

# === Logging & Intents ===
logging.basicConfig(level=logging.INFO)
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
EVENT_NAME   = "Desert Perpetual"
EVENT_TITLE  = f"🔥 CLAN RAID EVENT: {EVENT_NAME} 🔥"
EMBED_COLOR  = 0xFF4500
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

    # If it’s Sunday at 09:00 BST and we haven’t run today, schedule
    if now.weekday() == 6:
        if now.hour == 9 and last_schedule_date != now.date():
            await schedule_weekly_posts_function()
            last_schedule_date = now.date()
    else:
        # Once it’s no longer Sunday, clear the flag so we’ll run again next week
        last_schedule_date = None

async def schedule_weekly_posts_function():
    tz      = pytz.timezone("Europe/London")
    now     = datetime.now(tz)
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        logging.error(f"Could not find channel {CHANNEL_ID}")
        return

    # 1) Delete last week’s messages
    for mid in previous_week_messages:
        try:
            msg = await channel.fetch_message(mid)
            await msg.delete()
        except discord.NotFound:
            pass
    previous_week_messages.clear()

    # 2) Build sorted list of dates and prune
    week_dts   = sorted(now + timedelta(days=i) for i in range(7))
    valid_strs = {dt.strftime("%A, %d %B") for dt in week_dts}
    logging.info(f"Pruning dates: {set(fireteams) - valid_strs}")
    for ds in list(fireteams):
        if ds not in valid_strs:
            fireteams.pop(ds, None)
            backups.pop(ds, None)
    save_raids()

    # 3) Detect already-posted days via embed metadata
    posted_dates = set()
    async for m in channel.history(limit=200):
        if m.author == bot.user and m.embeds:
            hidden = extract_date(m)
            if hidden:
                posted_dates.add(hidden)

    # 4) Post missing days, one-by-one in try/except
    for dt in week_dts:
        date_str = dt.strftime("%A, %d %B")
        if date_str in posted_dates:
            continue

        fireteams.setdefault(date_str, {})
        backups.setdefault(date_str, {})

        try:
            description = await build_raid_message(date_str)
            embed = discord.Embed(
                title=EVENT_TITLE,
                description=description,
                color=EMBED_COLOR
            )
            embed.add_field(name="\u200b", value=date_str, inline=False)

            msg = await channel.send(embed=embed)
            logging.info(f"Posted {date_str} as message ID {msg.id}")

            await msg.add_reaction("✅")
            await msg.add_reaction("❌")
            previous_week_messages.append(msg.id)

        except Exception as e:
            logging.error(f"Failed posting {date_str}: {e}")

    # 5) Persist so new slots survive restarts
    save_raids()
# —————————————————————————————————————————
# Bot Events
# —————————————————————————————————————————
@bot.event
async def on_ready():
    load_timezones()
    load_raids()
    load_badges()
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
            
        # ───────── Badge logic ─────────
        uid = str(member.id)
        stats = user_stats.setdefault(uid, {"raids_joined": 0, "promotions": 0})
        stats["raids_joined"] += 1

        await check_for_new_badges(member, stats)
        save_badges()
        # ───────────────────────────────

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
                    
            # ───────── Badge logic ─────────
            puid = str(promoted_uid)
            pstats = user_stats.setdefault(puid, {"raids_joined": 0, "promotions": 0})
            pstats["promotions"] += 1

            # fetch_user only once
            user_obj = promoted_member or await bot.fetch_user(promoted_uid)
            await check_for_new_badges(user_obj, pstats)
            save_badges()
            # ─────────────────────────────────
        
        recent_changes[member.id] = "left"
        schedule_udate(message.id, date_str)
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
        date_str = extract_date(message)
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
        date_str = extract_date(message)
        if not date_str:
            return

    await handler(payload, member, message, date_str)
    
async def update_raid_message(message_id: int, date_str: str):
    # give Discord a moment before patching
    await edit_limiter.wait()  

    channel = bot.get_channel(CHANNEL_ID)
    message = await channel.fetch_message(message_id)

    description = await build_raid_message(date_str)
    embed = message.embeds[0]
    embed.description = description

    # edit inside a try/except block
    try:
        await message.edit(embed=embed)
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
                    except Exception as e:
                        logging.warning(f"Could not fetch user {uid} for reminder: {e}")
                        continue
                
                    try:
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
    if not user_scores:
        return await ctx.send("No scores yet. Start raiding to earn points!")

    sorted_scores = sorted(
        [(uid, data["score"]) for uid, data in user_scores.items()
         if uid != str(bot.user.id)],
        key=lambda x: x[1], reverse=True
    )
    lines = []
    for uid, pts in sorted_scores:
        user = await bot.fetch_user(int(uid))
        lines.append(f"**{user.name}**: {pts} point{'s' if pts != 1 else ''}")

    await ctx.send("🏆 **Raid Leaderboard** 🏆\n" + "\n".join(lines))

@bot.command(name="showlineup")
async def show_lineup(ctx, *, date_str: str):
    if date_str not in fireteams and date_str not in backups:
        await ctx.send(f"No lineup found for **{date_str}**.")
        return

    lines = [f"**Lineup for {date_str}:**"]

    # Fireteam slots
    for i in range(6):
        uid = fireteams.get(date_str, {}).get(i)
        if not uid:
            lines.append(f"{i+1}. Empty Slot")
            continue

        try:
            user = await get_cached_user(uid)
        except Exception as e:
            logging.warning(f"Could not fetch user {uid} for show_lineup: {e}")
            lines.append(f"{i+1}. Unknown User")
            continue

        lines.append(f"{i+1}. {user.display_name}")

    # Backup slots
    for i in range(2):
        uid = backups.get(date_str, {}).get(i)
        if not uid:
            lines.append(f"Backup {i+1}: Empty")
            continue

        try:
            user = await get_cached_user(uid)
        except Exception as e:
            logging.warning(f"Could not fetch backup user {uid}: {e}")
            lines.append(f"Backup {i+1}: Unknown User")
            continue

        lines.append(f"Backup {i+1}: {user.display_name}")

    # Send once, after building all lines
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
