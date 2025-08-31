
import os
import json
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import pytz
import random
import logging

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
fireteams = {}  # {date: [players]}
backups = {}    # {date: [players]}
scores = {}     # {user_id: points}
user_scores = {}  # Dice game scores
previous_week_messages = []  # Message IDs to delete

CHANNEL_ID = 1209484610568720384  # Raid channel ID

# Persistent storage functions
def load_data():
    global fireteams, backups
    if os.path.exists("fireteams.json"):
        with open("fireteams.json", "r") as f:
            fireteams = {k: v for k, v in json.load(f).items()}
    if os.path.exists("backups.json"):
        with open("backups.json", "r") as f:
            backups = {k: v for k, v in json.load(f).items()}

def save_data():
    with open("fireteams.json", "w") as f:
        json.dump(fireteams, f)
    with open("backups.json", "w") as f:
        json.dump(backups, f)

# ✅ Helper function to post weekly raid schedule
async def schedule_weekly_posts_function():
    london = pytz.timezone("Europe/London")
    now = datetime.now(london)
    print(f"📅 Running schedule_weekly_posts_function at {now}")

    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print(f"❌ Could not find channel with ID {CHANNEL_ID}")
        return

    print(f"📨 Found channel: {channel.name} (ID: {channel.id})")

    # 🧹 Delete previous week's messages if it's Sunday
    if now.weekday() == 6 and previous_week_messages:
        print("🧹 Deleting previous week's messages...")
        for msg_id in previous_week_messages:
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.delete()
                print(f"🗑️ Deleted message ID {msg_id}")
            except discord.NotFound:
                print(f"⚠️ Message ID {msg_id} not found.")
        previous_week_messages.clear()

    organiser_id = bot.user.id
    scores[organiser_id] = scores.get(organiser_id, 0) + 7

    # Fetch recent messages to check for duplicates
    recent_messages = [msg async for msg in channel.history(limit=100)]

    for i in range(7):
        raid_date = now + timedelta(days=i)
        date_str = raid_date.strftime("%A, %d %B")

        # Check if a message for this date already exists
        if any(date_str in msg.content for msg in recent_messages):
            print(f"⏳ Skipping {date_str} — already posted.")
            continue

        fireteams[date_str] = []
        backups[date_str] = []

        try:
            msg = await channel.send(
                f"@everyone\n🔥 CLAN RAID EVENT: Desert Perpetual 🔥\n"
                f"🗓️ Day: {date_str} | 🕗 Time: 20:00 BST\n\n"
                f"🎯 Fireteam Lineup (6 Players):\n" +
                "\n".join([f"{i+1}. Empty Slot" for i in range(6)]) +
                "\n\n🛡️ Backup Players (2):\n" +
                "\n".join([f"{i+1}. Empty Slot" for i in range(2)]) +
                "\n\n✅ React with a ✅ if you can join this raid.\n❌ React with a ❌ if you can't make it."
            )
            await msg.add_reaction("✅")
            await msg.add_reaction("❌")
            previous_week_messages.append(msg.id)
            print(f"✅ Posted raid message for {date_str}")
        except Exception as e:
            print(f"❌ Failed to post message for {date_str}: {e}")

    save_data()

# ✅ Check if bot missed the scheduled post
async def check_missed_schedule():
    london = pytz.timezone("Europe/London")
    now = datetime.now(london)
    print(f"🔍 check_missed_schedule() called at {now}")

    try:
        with open("last_schedule_time.txt", "r") as f:
            last_run_str = f.read().strip()
            last_run = datetime.strptime(last_run_str, "%Y-%m-%d %H:%M:%S")
            print(f"📄 Last run was at {last_run}")
    except (FileNotFoundError, ValueError):
        print("⚠️ No valid last_schedule_time.txt found.")
        last_run = None

    if now.weekday() == 6 and now.hour >= 9:
        if not last_run or last_run.date() != now.date():
            print("✅ Missed schedule detected — posting now.")
            await schedule_weekly_posts_function()
            with open("last_schedule_time.txt", "w") as f:
                f.write(now.strftime("%Y-%m-%d %H:%M:%S"))
        else:
            print("⏳ Already posted today — skipping.")
    else:
        print("🕘 Not Sunday after 9am — skipping.")

@bot.event
async def on_ready():
    print(f"✅ Bot started at {datetime.now()} as {bot.user}")
    load_data()
    schedule_weekly_posts.start()
    send_reminders.start()
    await check_missed_schedule()

@bot.event
async def on_command_error(ctx, error):
    print(f"⚠️ Command error: {error}")

@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return

    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)
    channel = bot.get_channel(payload.channel_id)
    message = await channel.fetch_message(payload.message_id)

    import re
    match = re.search(r'Day:\s*(.+?)\s*\|', message.content)
    if not match:
        print("⚠️ Could not extract date from message.")
        return

    date_str = match.group(1).strip()

    if date_str not in fireteams:
        fireteams[date_str] = []
    if date_str not in backups:
        backups[date_str] = []

    if payload.emoji.name == "✅":
        if member.id in fireteams[date_str] or member.id in backups[date_str]:
            return

        if len(fireteams[date_str]) < 6:
            fireteams[date_str].append(member.id)
            await member.send(
                f"You're in! 🎉\nThanks for joining the Desert Perpetual raid team on {date_str} at 20:00 BST.\n"
                "You'll receive a reminder one hour before the raid begins. Get ready to bring your A-game!"
            )
        elif len(backups[date_str]) < 2:
            backups[date_str].append(member.id)
            await member.send(
                f"You've been added as a backup for the Desert Perpetual raid on {date_str} at 20:00 BST.\n"
                "We'll notify you if a slot opens up!"
            )
        else:
            await member.send(
                f"Sorry, the raid on {date_str} is full (6 fireteam + 2 backups). "
                "You're welcome to join next time or keep an eye out for cancellations!"
            )

    elif payload.emoji.name == "❌":
        removed = False
        if member.id in fireteams[date_str]:
            fireteams[date_str].remove(member.id)
            removed = True
        if member.id in backups[date_str]:
            backups[date_str].remove(member.id)
            removed = True

        if removed:
            await member.send(
                f"You've been removed from the Desert Perpetual raid team or backup list for {date_str}.\n"
                "Thanks for letting us know — hope to see you in the next raid!"
            )

    fireteam_names = []
    for i in range(6):
        if i < len(fireteams[date_str]):
            user = await bot.fetch_user(fireteams[date_str][i])
            fireteam_names.append(f"{i+1}. {user.display_name}")
        else:
            fireteam_names.append(f"{i+1}. Empty Slot")

    backup_names = []
    for i in range(2):
        if i < len(backups[date_str]):
            user = await bot.fetch_user(backups[date_str][i])
            backup_names.append(f"{i+1}. {user.display_name}")
        else:
            backup_names.append(f"{i+1}. Empty Slot")

    new_content = (
        f"@everyone\n🔥 **CLAN RAID EVENT: Desert Perpetual** 🔥\n\n"
        f"📅 **Day:** {date_str}  |  🕗 **Time:** 20:00 BST\n\n"
        f"🎯 **Fireteam Lineup (6 Players):**\n" + "\n".join(fireteam_names) +
        "\n\n🛡️ **Backup Players (2):**\n" + "\n".join(backup_names) +
        "\n\n✅ React with a ✅ if you're joining the raid.\n"
        "❌ React with a ❌ if you can't make it.\n\n"
        "Let’s assemble a legendary team and conquer the Desert Perpetual!"
    )

    await message.edit(content=new_content)
    save_data()

@tasks.loop(hours=168)
async def schedule_weekly_posts():
    await schedule_weekly_posts_function()

@tasks.loop(minutes=1)
async def send_reminders():
    london = pytz.timezone("Europe/London")
    now = datetime.now(london)

    for date_str, players in fireteams.items():
        try:
            cleaned_date_str = date_str.lstrip("* ").strip()
            raid_time = datetime.strptime(cleaned_date_str, "%A, %d %B").replace(hour=19, minute=0)

            if now.strftime("%A, %d %B %H:%M") == raid_time.strftime("%A, %d %B %H:%M"):
                for user_id in players:
                    user = await bot.fetch_user(user_id)
                    await user.send(
                        "⏳ One hour to go!\nYour raid team for Desert Perpetual assembles at 20:00 BST tonight.\n"
                        "Please be punctual, geared up, and ready to dive in. Let’s make this a legendary run!"
                    )
                    scores[user_id] = scores.get(user_id, 0) + 1

        except ValueError as e:
            print(f"[send_reminders] Date parsing error for '{date_str}': {e}")
        except Exception as e:
            print(f"[send_reminders] Unexpected error: {e}")

# Run bot
token = os.getenv("DISCORD_TOKEN")
if not token:
    print("❌ DISCORD_TOKEN is missing. Check your Railway environment variables.")
    exit()

bot.run(token)



