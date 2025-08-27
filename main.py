
import discord
from discord.ext import tasks, commands
import datetime
import pytz

# Privileged intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True

# Bot setup
bot = commands.Bot(command_prefix="!", intents=intents)

# Channel ID for posting raid messages
RAID_CHANNEL_ID = 1209484610568720384

# Store message and sign-ups
raid_message = None
team_slots = []
backup_slots = []
accepted_users = set()

# Reset slots each day
def reset_slots():
    global team_slots, backup_slots, accepted_users
    team_slots = []
    backup_slots = []
    accepted_users = set()

# Format the raid message
def format_raid_message(day_name):
    msg = f"**Desert Perpetual Raid Sign-Up – {day_name} at 20:00 UK time**\n\n"
    msg += "React with ✅ to join the raid.\nReact with ❌ if you can't make it.\n\n"
    msg += "**Team Slots (6 Players):**\n"
    for i in range(6):
        msg += f"{i+1}. {team_slots[i] if i < len(team_slots) else '[Empty]'}\n"
    msg += "\n**Backup Slots (2 Players):**\n"
    for i in range(2):
        msg += f"{i+1}. {backup_slots[i] if i < len(backup_slots) else '[Empty]'}\n"
    msg += "\n*Slots will be filled automatically based on reactions.*"
    return msg

# Post the daily raid message
@tasks.loop(minutes=1)
async def daily_raid_post():
    global raid_message
    now = datetime.datetime.now(pytz.timezone("Europe/London"))
    if now.hour == 9 and now.minute == 0:
        reset_slots()
        channel = bot.get_channel(RAID_CHANNEL_ID)
        if channel:
            # Delete previous message
            if raid_message:
                try:
                    await raid_message.delete()
                except discord.NotFound:
                    pass  # Message already deleted

            day_name = now.strftime("%A")
            content = format_raid_message(day_name)
            raid_message = await channel.send(content)
            await raid_message.add_reaction("✅")
            await raid_message.add_reaction("❌")

# Send reminders at 19:00 UK time
@tasks.loop(minutes=1)
async def send_reminders():
    now = datetime.datetime.now(pytz.timezone("Europe/London"))
    if now.hour == 19 and now.minute == 0:
        for user_id in accepted_users:
            user = await bot.fetch_user(user_id)
            try:
                await user.send("⏰ Reminder: The Desert Perpetual raid starts in 1 hour at 20:00 UK time. Get ready!")
            except discord.Forbidden:
                print(f"Could not send reminder to {user.name}")

# Handle reactions
@bot.event
async def on_raw_reaction_add(payload):
    global raid_message
    if raid_message and payload.message_id == raid_message.id:
        guild = bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id)
        if member and not member.bot:
            name = member.display_name
            day_name = datetime.datetime.now(pytz.timezone("Europe/London")).strftime("%A")

            if str(payload.emoji) == "✅":
                if name not in team_slots and name not in backup_slots:
                    if len(team_slots) < 6:
                        team_slots.append(name)
                    elif len(backup_slots) < 2:
                        backup_slots.append(name)

                    if member.id not in accepted_users:
                        try:
                            await member.send(f"✅ You’ve been accepted into the Desert Perpetual raid team for today ({day_name})!")
                            accepted_users.add(member.id)
                        except discord.Forbidden:
                            print(f"Could not DM {member.display_name}")

            elif str(payload.emoji) == "❌":
                if name in team_slots:
                    team_slots.remove(name)
                if name in backup_slots:
                    backup_slots.remove(name)
                if member.id in accepted_users:
                    accepted_users.remove(member.id)

            new_content = format_raid_message(day_name)
            await raid_message.edit(content=new_content)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    daily_raid_post.start()
    send_reminders.start()


# In-memory score tracking
user_scores = {}

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

import random
@bot.command(name="roll")
async def roll_dice(ctx, sides: int = 6):
    """Rolls a dice and updates the user's score."""
    allowed_channel_id = 1409621956336287774  # 🔁 

    if ctx.channel.id != allowed_channel_id:
        await ctx.send("❌ This command can only be used in the designated dice game channel.")
        return

    if sides < 2:
        await ctx.send("Dice must have at least 2 sides!")
        return

    result = random.randint(1, sides)
    user_id = str(ctx.author.id)
    user_name = ctx.author.display_name

    # Update score
    if user_id not in user_scores:
        user_scores[user_id] = {"name": user_name, "score": 0}
    user_scores[user_id]["score"] += result
    await ctx.send(f"🎲 {user_name} rolled a {result} on a {sides}-sided die! Total score: {user_scores[user_id]['score']}")

@bot.command(name="leaderboard")
async def show_leaderboard(ctx):
    """Displays the top 5 players by score."""
    if not user_scores:
        await ctx.send("No scores yet! Roll the dice with `!roll`.")
        return

    # Sort by score descending
    sorted_scores = sorted(user_scores.values(), key=lambda x: x["score"], reverse=True)
    top_players = sorted_scores[:5]

    leaderboard = "**🏆 Leaderboard 🏆**\n"
    for i, player in enumerate(top_players, start=1):
        leaderboard += f"{i}. {player['name']} - {player['score']} points\n"

    await ctx.send(leaderboard)

# Get the token from Railway's environment variables
token = os.getenv("DISCORD_TOKEN")
bot.run(token)
