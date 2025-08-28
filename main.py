import os
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import pytz

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

fireteams = {}  # {date: [players]}
backups = {}    # {date: [players]}
scores = {}     # {user_id: points}

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    schedule_weekly_posts.start()

CHANNEL_ID = 1209484610568720384  

@tasks.loop(hours=168)  # Every 7 days
async def schedule_weekly_posts():
    london = pytz.timezone("Europe/London")
    now = datetime.now(london)
    if now.hour == 9:
        channel = bot.get_channel(CHANNEL_ID)
        
  # 🧹 Delete previous week's messages if it's Sunday
        if now.weekday() == 6 and previous_week_messages:
            for msg_id in previous_week_messages:
                try:
                    msg = await channel.fetch_message(msg_id)
                    await msg.delete()
                except discord.NotFound:
                    pass  # Message already deleted or doesn't exist
            previous_week_messages.clear()

        organiser_id = bot.user.id
        scores[organiser_id] = scores.get(organiser_id, 0) + 7

        for i in range(7):
            raid_date = now + timedelta(days=i)
            date_str = raid_date.strftime("%A, %d %B")
            fireteams[date_str] = []
            backups[date_str] = []

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
            
            # 📝 Store message ID
            previous_week_messages.append(msg.id)

@bot.event
async def on_raw_reaction_add(payload):
    if payload.emoji.name != "✅":
        return

    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)
    channel = bot.get_channel(payload.channel_id)
    message = await channel.fetch_message(payload.message_id)

    date_line = next((line for line in message.content.split("\n") if "Day:" in line), None)
    if not date_line:
        return
    date_str = date_line.split("Day: ")[1].split(" |")[0]

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

@tasks.loop(minutes=1)
async def send_reminders():
    london = pytz.timezone("Europe/London")
    now = datetime.now(london)
    for date_str, players in fireteams.items():
        raid_time = datetime.strptime(date_str, "%A, %d %B").replace(hour=19, minute=0)  # 1 hour before
        if now.strftime("%A, %d %B %H:%M") == raid_time.strftime("%A, %d %B %H:%M"):
            for user_id in players:
                user = await bot.fetch_user(user_id)
                await user.send(
                    "⏳ One hour to go!\nYour raid team for Desert Perpetual assembles at 20:00 BST tonight.\n"
                    "Please be punctual, geared up, and ready to dive in. Let’s make this a legendary run!"
                )
                scores[user_id] = scores.get(user_id, 0) + 1
                
@bot.command(name="Raidleaderboard")
async def Raidleaderboard(ctx):
    if not scores:
        await ctx.send("No scores yet. Start raiding to earn points!")
        return

    bot_id = bot.user.id
    sorted_scores = sorted(
        [(uid, pts) for uid, pts in scores.items() if uid != bot_id],
        key=lambda x: x[1],
        reverse=True
    )

    if not sorted_scores:
        await ctx.send("No player scores yet. Get involved in raids to earn points!")
        return

    leaderboard = []
    for user_id, points in sorted_scores:
        user = await bot.fetch_user(user_id)
        leaderboard.append(f"**{user.name}**: {points} point{'s' if points != 1 else ''}")

    await ctx.send("🏆 **Raid Leaderboard** 🏆\n" + "\n".join(leaderboard))

# In-memory score tracking
user_scores = {}

import random
@bot.command(name="roll")
async def roll_dice(ctx, sides: int = 6):
    """Rolls a dice and updates the user's score."""
channel_name = ctx.channel.name if ctx.channel.name else "Unknown"
print(f"ROLL command triggered by {ctx.author} in {channel_name} at {datetime.now()}")
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


@bot.event
async def on_command_error(ctx, error):
    print(f"⚠️ Command error: {erro


# Get the token from Railway's environment variables
token = os.getenv("DISCORD_TOKEN")
bot.run(token)
