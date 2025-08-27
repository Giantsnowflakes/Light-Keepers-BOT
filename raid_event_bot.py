import discord
from discord.ext import tasks, commands
from discord.utils import get
import datetime
import pytz
import asyncio

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Timezone for BST
london_tz = pytz.timezone("Europe/London")

# Fireteam and backup limits
FIRETEAM_SIZE = 6
BACKUP_SIZE = 2

# Store messages and participants
event_messages = {}
participants = {}

# Days of the week
days_of_week = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    schedule_weekly_posts.start()

@tasks.loop(hours=168)  # Every 7 days
async def schedule_weekly_posts():
    now = datetime.datetime.now(london_tz)
    if now.weekday() == 6 and now.hour == 9:  # Sunday at 9am BST
        guild = discord.utils.get(bot.guilds)
        channel = discord.utils.get(guild.text_channels, name="raid-events")  # Change to your channel name
        today = now.date()
        for i, day in enumerate(days_of_week):
            raid_date = today + datetime.timedelta(days=i)
            message = await channel.send(
                f"@everyone
"
                f"🔥 **CLAN EVENT - Desert Perpetual** 🔥
"
                f"📅 **Day:** {day} | {raid_date.strftime('%d %B')} ⏰ @ 20:00 BST

"
                f"🧑‍🤝‍🧑 **Fireteam includes:**
"
                f"Empty Slot
" * FIRETEAM_SIZE +
                f"
🛡️ **Back-up players:**
"
                f"Empty Slot
" * BACKUP_SIZE +
                f"
💬 React with ✅ to join or ❌ to decline. You'll receive a confirmation and a reminder 1 hour before the raid!"
            )
            await message.add_reaction("✅")
            await message.add_reaction("❌")
            event_messages[message.id] = {
                "date": raid_date,
                "fireteam": [],
                "backup": [],
                "message": message
            }

@bot.event
async def on_raw_reaction_add(payload):
    if payload.message_id in event_messages and payload.user_id != bot.user.id:
        guild = discord.utils.get(bot.guilds)
        member = guild.get_member(payload.user_id)
        if not member:
            return
        event = event_messages[payload.message_id]
        if str(payload.emoji) == "✅":
            if member.id in event["fireteam"] or member.id in event["backup"]:
                return
            if len(event["fireteam"]) < FIRETEAM_SIZE:
                event["fireteam"].append(member.id)
                await member.send(
                    f"Hi {member.display_name}, you've been added to the raid team for Desert Perpetual on {event['date'].strftime('%A %d %B')} at 20:00 BST. "
                    f"Thanks for joining! You'll receive a reminder one hour before the raid starts."
                )
            elif len(event["backup"]) < BACKUP_SIZE:
                event["backup"].append(member.id)
                await member.send(
                    f"Hi {member.display_name}, you've been added as a backup for the raid team on {event['date'].strftime('%A %d %B')} at 20:00 BST. "
                    f"You'll be notified if a slot opens up."
                )
            await update_event_message(event)
            schedule_reminder(member, event["date"])
        elif str(payload.emoji) == "❌":
            await member.send(f"Thanks for letting us know, {member.display_name}. Maybe next time!")

async def update_event_message(event):
    fireteam_names = []
    backup_names = []
    guild = discord.utils.get(bot.guilds)
    for uid in event["fireteam"]:
        member = guild.get_member(uid)
        fireteam_names.append(member.display_name if member else "Unknown")
    for uid in event["backup"]:
        member = guild.get_member(uid)
        backup_names.append(member.display_name if member else "Unknown")
    fireteam_text = "
".join(fireteam_names + ["Empty Slot"] * (FIRETEAM_SIZE - len(fireteam_names)))
    backup_text = "
".join(backup_names + ["Empty Slot"] * (BACKUP_SIZE - len(backup_names)))
    new_content = (
        f"@everyone
"
        f"🔥 **CLAN EVENT - Desert Perpetual** 🔥
"
        f"📅 **Day:** {event['date'].strftime('%A')} | {event['date'].strftime('%d %B')} ⏰ @ 20:00 BST

"
        f"🧑‍🤝‍🧑 **Fireteam includes:**
{fireteam_text}

"
        f"🛡️ **Back-up players:**
{backup_text}

"
        f"💬 React with ✅ to join or ❌ to decline. You'll receive a confirmation and a reminder 1 hour before the raid!"
    )
    await event["message"].edit(content=new_content)

def schedule_reminder(member, raid_date):
    now = datetime.datetime.now(london_tz)
    raid_time = london_tz.localize(datetime.datetime.combine(raid_date, datetime.time(20, 0)))
    reminder_time = raid_time - datetime.timedelta(hours=1)
    delay = (reminder_time - now).total_seconds()
    if delay > 0:
        asyncio.get_event_loop().call_later(delay, lambda: asyncio.create_task(send_reminder(member, raid_date)))

async def send_reminder(member, raid_date):
    await member.send(
        f"Reminder: Your raid team for Desert Perpetual will assemble in one hour at 20:00 BST on {raid_date.strftime('%A %d %B')}.
"
        f"Please be ready, punctual, and have fun!"
    )

# Replace 'YOUR_BOT_TOKEN' with your actual bot token
bot.run("YOUR_BOT_TOKEN")
