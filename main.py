import os
import re
import logging
import json
import asyncio
from typing import Dict, List
from datetime import datetime, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv

import httpx  # fetch hackathons from GitHub JSON

from openai import OpenAI  # used with Hugging Face router

# -------------------------------------------------
# 1) ENV + CONFIG
# -------------------------------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Hugging Face token + model for /ask (via router.huggingface.co/v1)
HF_TOKEN = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN")
HF_MODEL = os.getenv(
    "HUGGINGFACE_MODEL",
    "Qwen/Qwen2.5-72B-Instruct"  # change to any HF router-supported chat model
)

# channels
WELCOME_CHANNEL_NAME = "welcome"             # public welcome channel
HACKATHON_CHANNEL_NAME = "all-hackathons"    # global hackathons feed
ANNOUNCEMENTS_CHANNEL_NAME = "announcements" # where you announce winners (for auto-detect)

# roles
ROLE_VERIFY = "Verified Hackeroos"
ROLE_TECH = "Tech Hackeroos"
ROLE_COMMUNITY = "Community Hackeroos"

# naughty words 😀 (extended for family-friendly community)
BLOCKED_WORDS = [
    "shit", "fuck", "bitch", "bastard", "cunt", "slut", "whore",
    "dick", "pussy", "fag", "faggot", "nigga", "nigger",
    "bloody hell", "asshole", "retard", "moron", "idiot",
    "porn", "nsfw", "sex", "cum", "jerk off", "jerking", "rape",
]

# data files
WINNERS_FILE = "winners.json"
HACKATHON_WINNERS: Dict[str, dict] = {}

# in-memory cache of last hackathon list (for auto alerts)
LAST_HACKATHONS: List[dict] = []

# GitHub JSON with merged hackathons (configurable via .env)
HACKATHONS_JSON_URL = os.getenv(
    "HACKATHONS_JSON_URL",
    "https://raw.githubusercontent.com/aadarsh1282/pika-bot/main/data/hackathons.json",
)

# -------------------------------------------------
# Emoji stripper (to clean "🎃 WINNERS 🎃")
# -------------------------------------------------
EMOJI_PATTERN = re.compile(
    "["                     # Start of character class
    "\U0001F300-\U0001F5FF" # Misc symbols and pictographs
    "\U0001F600-\U0001F64F" # Emoticons
    "\U0001F680-\U0001F6FF" # Transport & map symbols
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\u2600-\u26FF"         # Misc symbols
    "\u2700-\u27BF"         # Dingbats
    "]+",
    flags=re.UNICODE,
)


def strip_emojis(text: str) -> str:
    """Remove most emoji characters from the text."""
    return EMOJI_PATTERN.sub("", text)

# -------------------------------------------------
# 2) LOGGING
# -------------------------------------------------
handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("pika-bot")

# -------------------------------------------------
# 3) INTENTS + BOT
# -------------------------------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# -------------------------------------------------
# 4) HACKATHONS: FETCH FROM GITHUB JSON
# -------------------------------------------------


async def fetch_hackathons_from_github() -> List[dict]:
    """
    Fetch merged hackathon list from GitHub (updated by GitHub Actions).
    Returns a list of dicts with keys: title, url, start_date, location, source.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(HACKATHONS_JSON_URL)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                log.info("Fetched %d hackathons from GitHub JSON", len(data))
                return data
            log.warning("Hackathons JSON is not a list, got: %s", type(data))
            return []
    except Exception as e:
        log.warning("⚠️ Could not fetch hackathons JSON: %s", e)
        return []


async def pin_and_unpin(message: discord.Message):
    """
    Pins the new message and unpins the previous pinned one in that channel.
    Keeps the latest hackathon announcement at the top.
    """
    channel = message.channel
    try:
        pinned = await channel.pins()
        # unpin the last pinned message if exists
        if pinned:
            try:
                await pinned[0].unpin()
            except Exception:
                pass

        await message.pin(reason="New Hackathon Announcement")
    except Exception as e:
        log.warning("⚠️ Could not pin/unpin: %s", e)


# -------------------------------------------------
# AUTO ALERTS LOOP (improved)
# -------------------------------------------------
async def auto_alerts_loop():
    global LAST_HACKATHONS
    await bot.wait_until_ready()
    log.info("Auto-alerts loop started — checking every 3 hours")

    while not bot.is_closed():
        try:
            events = await fetch_hackathons_from_github()
            if not events:
                log.warning("Hackathons JSON empty or unreachable")
                await asyncio.sleep(3 * 60 * 60)
                continue

            # First run: just cache
            if not LAST_HACKATHONS:
                LAST_HACKATHONS = events
                log.info("First run: cached %d hackathons", len(events))
                await asyncio.sleep(3 * 60 * 60)
                continue

            # Detect new events by URL (primary)
            old_urls = {e.get("url") for e in LAST_HACKATHONS if e.get("url")}
            new_events = [
                e for e in events
                if e.get("url") and e["url"] not in old_urls
            ]

            # (Optional) fallback: if URL missing, we could compare title+date here

            if new_events:
                log.info("NEW HACKATHONS DETECTED: %d", len(new_events))

                for guild in bot.guilds:
                    channel = discord.utils.get(guild.text_channels, name=HACKATHON_CHANNEL_NAME)
                    if not channel:
                        continue

                    embed = discord.Embed(
                        title="NEW GLOBAL HACKATHONS!",
                        description=f"{len(new_events)} new event(s) just dropped!",
                        color=0x00ff88,
                        timestamp=datetime.now(timezone.utc),
                    )
                    for e in new_events[:10]:
                        title = (e.get("title") or "Untitled")[:80]
                        source = e.get("source", "Unknown")
                        start = e.get("start_date", "")[:10] if e.get("start_date") else "TBA"
                        loc = e.get("location", "Online")
                        url = e.get("url", "#")
                        embed.add_field(
                            name=f"{source} · {title}",
                            value=f"{loc} • {start} • [Register]({url})",
                            inline=False,
                        )
                    embed.set_footer(text="Pika-Bot • Auto-updated from GitHub")

                    msg = await channel.send(embed=embed, content="@here New hackathons!")
                    await pin_and_unpin(msg)
            else:
                log.info("No new hackathons this cycle")

            LAST_HACKATHONS = events[:100]  # keep memory lean

        except Exception as e:
            log.exception("auto_alerts_loop crashed: %s", e)

        await asyncio.sleep(3 * 60 * 60)  # 3 hours

# -------------------------------------------------
# WINNERS UTILITIES
# -------------------------------------------------


def load_winners():
    global HACKATHON_WINNERS
    if os.path.exists(WINNERS_FILE):
        try:
            with open(WINNERS_FILE, "r", encoding="utf-8") as f:
                HACKATHON_WINNERS = json.load(f)
            log.info("Loaded %d winners from %s", len(HACKATHON_WINNERS), WINNERS_FILE)
        except Exception as e:
            log.warning("⚠️ Could not load winners: %s", e)
            HACKATHON_WINNERS = {}
    else:
        HACKATHON_WINNERS = {}


def save_winners():
    try:
        with open(WINNERS_FILE, "w", encoding="utf-8") as f:
            json.dump(HACKATHON_WINNERS, f, indent=2, ensure_ascii=False)
        log.info("Saved %d winners to %s", len(HACKATHON_WINNERS), WINNERS_FILE)
    except Exception as e:
        log.warning("⚠️ Could not save winners: %s", e)

# -------------------------------------------------
# 6) ON READY
# -------------------------------------------------
@bot.event
async def on_ready():
    load_winners()

    # start auto alerts loop
    bot.loop.create_task(auto_alerts_loop())

    # sync slash commands globally
    try:
        await bot.tree.sync()
        log.info("Slash commands synced globally")
        print("✅ Slash commands synced globally.")  # optional console print
    except Exception as e:
        log.warning("⚠️ Error syncing slash commands: %s", e)
        print("⚠️ Error syncing slash commands:", e)

    log.info("Pika-Bot online | Guilds: %d | Hackathons cached: %d", len(bot.guilds), len(LAST_HACKATHONS))

    print("🦘------------------------------------------------------------")
    print(f"⚡ {bot.user.name} is online and hopping!")
    print(f"🏠 Connected servers: {len(bot.guilds)}")
    print("🦘------------------------------------------------------------")

    await bot.change_presence(
        activity=discord.Game(name="Helping Hackeroos innovate ⚡🦘"),
        status=discord.Status.online
    )

    # optional welcome in #all-hackathons if present
    for guild in bot.guilds:
        hack_channel = discord.utils.get(guild.text_channels, name=HACKATHON_CHANNEL_NAME)
        if hack_channel:
            try:
                await hack_channel.send("🌍 **Pika-Bot** is live! Use `/hackathons` to see global hackathons ⚡")
            except discord.Forbidden:
                pass

# -------------------------------------------------
# 7) EVENTS
# -------------------------------------------------
@bot.event
async def on_member_join(member: discord.Member):
    # DM
    try:
        await member.send(
            f"Welcome to Hackeroos, {member.name}! 🦘💛\n"
            f"I’m **Pika-Bot**. Use `/pika-help` in the server to see what I can do.\n"
            f"To unlock channels, run `/verify` in #{WELCOME_CHANNEL_NAME}."
        )
    except discord.Forbidden:
        log.warning("⚠️ Could not DM %s", member.name)

    # public welcome
    channel = discord.utils.get(member.guild.text_channels, name=WELCOME_CHANNEL_NAME)
    if channel:
        await channel.send(
            f"⚡ G’day {member.mention}! Welcome to **{member.guild.name}** — run `/verify` to get access!"
        )


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    lowered = message.content.lower()

    # bad-word filter
    if any(bad in lowered for bad in BLOCKED_WORDS):
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        await message.channel.send(f"{message.author.mention}, let’s keep it clean, mate! 🧹")
        return

    # 🏆 Semi-automatic winner capture (supports Kasey-style "🎃 WINNERS 🎃" posts)
    if (
        isinstance(message.channel, discord.TextChannel)
        and message.channel.name == ANNOUNCEMENTS_CHANNEL_NAME
        and message.author.guild_permissions.administrator
        and "winner" in lowered  # matches "winner" and "winners"
    ):
        hackathon = team = project = prize = None

        # 1) Try structured regex first (for "Winner: Hackathon | Team: ...")
        pattern = re.compile(
            r"(?:.*?)(?:winner|winners)\s*[:\-–]?\s*(?P<hackathon>[^|\n]+)"
            r"(?:\|\s*team:\s*(?P<team>[^|]+))?"
            r"(?:\|\s*project:\s*(?P<project>[^|]+))?"
            r"(?:\|\s*prize:\s*(?P<prize>[^|]+))?",
            re.IGNORECASE,
        )
        match = pattern.search(message.content)

        if match:
            raw_hackathon = (match.group("hackathon") or "")
            cleaned_hackathon = strip_emojis(raw_hackathon)
            cleaned_hackathon = cleaned_hackathon.replace("-", "").replace("–", "").replace(":", "")
            cleaned_hackathon = cleaned_hackathon.strip()

            if cleaned_hackathon and len(cleaned_hackathon) >= 3:
                hackathon = cleaned_hackathon
                team = (match.group("team") or "").strip() or "—"
                project = (match.group("project") or "").strip() or "—"
                prize = (match.group("prize") or "").strip() or "—"
            else:
                match = None  # force fallback if hackathon is basically just emojis

        # 2) Fallback: Kasey-style multi-line post
        if not match or not hackathon:
            lines = [ln for ln in message.content.splitlines() if ln.strip()]

            if lines:
                first_clean = strip_emojis(lines[0])
                first_clean = re.sub(r"(?i)\b(winner|winners)\b", "", first_clean)
                first_clean = first_clean.replace("-", "").replace("–", "").replace(":", "")
                first_clean = first_clean.strip()

                if not first_clean and len(lines) >= 2:
                    candidate = strip_emojis(lines[1]).strip()
                    if candidate:
                        hackathon = candidate
                        team = project = prize = "—"
                elif first_clean and len(first_clean) >= 3:
                    hackathon = first_clean
                    team = project = prize = "—"

        if hackathon:
            # Preserve any existing record but add announcement details
            existing = HACKATHON_WINNERS.get(hackathon, {})
            HACKATHON_WINNERS[hackathon] = {
                "hackathon": hackathon,
                "team": team or existing.get("team", "—"),
                "project": project or existing.get("project", "—"),
                "prize": prize or existing.get("prize", "—"),
                "source": "announcement",
                "announcement_text": message.content,
            }
            save_winners()

            try:
                await message.add_reaction("🏆")
            except discord.Forbidden:
                pass

            await message.channel.send(
                f"🏆 Winner saved for **{hackathon}** (via announcement).",
                reference=message,
            )
            return

    await bot.process_commands(message)

# -------------------------------------------------
# 8) SLASH COMMANDS
# -------------------------------------------------
@bot.tree.command(name="pika-help", description="Show all Pika-Bot slash commands dynamically 🦘")
async def pika_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🦘 Pika-Bot — Hackeroos Helper",
        description="Here’s everything I can do right now 👇",
        color=0xffc300
    )

    # Dynamically list ALL registered slash commands
    for cmd in bot.tree.get_commands():
        desc = cmd.description or "No description provided"
        embed.add_field(name=f"/{cmd.name}", value=desc, inline=False)

    embed.set_footer(text="Built by Pika-Bots (AIHE Group 19) ⚡")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="hello", description="Say g’day to Pika-Bot")
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"G’day {interaction.user.mention}! Pika-Bot here — ready to hack and hop! ⚡🦘"
    )


@bot.tree.command(name="about", description="What is Hackeroos / who made this bot?")
async def about(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Hackeroos 🦘⚡",
        description="Aussie-flavoured, student-friendly tech + hackathon community.",
        color=0x00bcd4
    )
    embed.add_field(
        name="What Pika-Bot does",
        value=(
            "• welcome members (DM + public)\n"
            "• verify users with `/verify` (no reactions)\n"
            "• polls\n"
            "• global hackathon feed\n"
            "• natural language Q&A with `/ask` (grounded + HF router)\n"
            "• winners tracking with `/set-winner` + `/winners`\n"
            "• (next) dataset / project helpers"
        ),
        inline=False
    )
    embed.add_field(name="Team", value="Pika-Bots — AIHE Group 19", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="verify", description="Get the Verified Hackeroos role ✅")
async def verify(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Run this in a server, not in DMs.", ephemeral=True)
        return

    role = discord.utils.get(guild.roles, name=ROLE_VERIFY)
    if role is None:
        await interaction.response.send_message(
            f"⚠️ I couldn’t find a role called `{ROLE_VERIFY}`. Ask an admin to create it.",
            ephemeral=True
        )
        return

    member = interaction.user
    if role in member.roles:
        await interaction.response.send_message("✅ You’re already verified!", ephemeral=True)
        return

    try:
        await member.add_roles(role, reason="Self-verify via /verify")
        await interaction.response.send_message("✅ You’ve been verified. Welcome in! 🦘", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(
            "⚠️ I don’t have permission to give you that role. Tell an admin to move my role higher.",
            ephemeral=True
        )


@bot.tree.command(name="poll", description="Create a yes/no poll")
async def poll(interaction: discord.Interaction, question: str):
    embed = discord.Embed(
        title="🐾 Hackeroos Poll",
        description=question,
        color=0xffc300
    )
    msg = await interaction.channel.send(embed=embed)
    await msg.add_reaction("👍")
    await msg.add_reaction("👎")
    await interaction.response.send_message("Poll created ✅", ephemeral=True)


@bot.tree.command(name="hackathons", description="Show upcoming global hackathons 🌍")
async def hackathons(interaction: discord.Interaction):
    # Not ephemeral so others can see the list too
    await interaction.response.defer(ephemeral=False)

    events = await fetch_hackathons_from_github()

    if not events:
        await interaction.followup.send(
            "⚠️ I couldn’t fetch hackathons just now. Try again later.\n"
            "You can still check:\n"
            "• https://devpost.com/hackathons\n"
            "• https://mlh.io/events\n"
            "• https://www.hackeroos.com.au/#whats-on",
        )
        return

    embed = discord.Embed(
        title="🌍 Live Global Hackathons",
        description=(
            "Here are some current and upcoming hackathons 🦘 "
            "(source: GitHub auto-updated feed)"
        ),
        color=0x00bcd4,
        timestamp=datetime.now(timezone.utc),
    )
    for e in events[:12]:
        title = (e.get("title") or "Untitled")[:100]
        source = e.get("source", "Unknown")
        location = e.get("location", "Online")
        url = e.get("url", "#")
        embed.add_field(
            name=title,
            value=f"[{source}] • {location} • [Details]({url})",
            inline=False
        )
    embed.set_footer(text="Sources: MLH, Lu.ma, Hack Club, etc. • Auto-updated via GitHub • Pika-Bot ⚡")

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="update-hackathons", description="Post latest hackathons to #all-hackathons 🌏")
async def update_hackathons(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    events = await fetch_hackathons_from_github()
    if not events:
        await interaction.followup.send("⚠️ Could not fetch hackathons from GitHub JSON.", ephemeral=True)
        return

    for guild in bot.guilds:
        channel = discord.utils.get(guild.text_channels, name=HACKATHON_CHANNEL_NAME)
        if not channel:
            continue

        embed = discord.Embed(
            title="🌍 New Global Hackathons!",
            description="Fresh hackathons just dropped 🦘⚡",
            color=0x00bcd4,
            timestamp=datetime.now(timezone.utc),
        )
        for e in events[:12]:
            title = (e.get("title") or "Untitled")[:100]
            source = e.get("source", "Unknown")
            location = e.get("location", "Online")
            url = e.get("url", "#")
            embed.add_field(
                name=f"{source}: {title}",
                value=f"{location} • [View event]({url})",
                inline=False,
            )
        embed.set_footer(text="Auto-updated from GitHub • Pika-Bot ⚡")

        msg = await channel.send(embed=embed)
        await pin_and_unpin(msg)

    await interaction.followup.send("✅ Posted latest hackathons to #all-hackathons (where available).", ephemeral=True)


@bot.tree.command(name="refresh-hackathons", description="Force refresh hackathons feed (admin only)")
async def refresh_hackathons(interaction: discord.Interaction):
    global LAST_HACKATHONS
    await interaction.response.defer(ephemeral=True)

    # simple admin check for slash command
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("⚠️ Only admins can use `/refresh-hackathons`.", ephemeral=True)
        return

    events = await fetch_hackathons_from_github()
    LAST_HACKATHONS = events
    await interaction.followup.send(
        f"✅ Hackathons cache forcefully refreshed ({len(events)} events cached).",
        ephemeral=True
    )


@bot.tree.command(name="faq", description="Common questions about Hackeroos / Pika-Bot")
async def faq(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📘 Hackeroos FAQ",
        description="Quick answers for new members 👇",
        color=0x3b82f6
    )
    embed.add_field(
        name="1. I just joined, what now?",
        value=f"Go to **#{WELCOME_CHANNEL_NAME}** and run `/verify` to unlock channels.",
        inline=False
    )
    embed.add_field(
        name="2. How do I see global hackathons?",
        value="Use `/hackathons` — Pika-Bot will show current sources.",
        inline=False
    )
    embed.add_field(
        name="3. Can I ask AI-style questions?",
        value="Yes — use `/ask <your question>` and I’ll answer using real Hackeroos data + a Hugging Face model.",
        inline=False
    )
    embed.add_field(
        name="4. How do I see past winners?",
        value="Use `/winners` to see recent Hackeroos hackathon winners.",
        inline=False
    )
    embed.add_field(
        name="5. Who built this?",
        value="Pika-Bots — AIHE Group 19 🦘⚡",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# -------------------------------------------------
# /status — simple health check
# -------------------------------------------------
@bot.tree.command(name="status", description="Bot health check")
async def status(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"✅ Online | Servers: {len(bot.guilds)} | Winners stored: {len(HACKATHON_WINNERS)}",
        ephemeral=True
    )

# -------------------------------------------------
# /ASK — Grounded first, HF router as fallback
# -------------------------------------------------
@bot.tree.command(name="ask", description="Ask Pika-Bot in natural language 🤖")
async def ask(interaction: discord.Interaction, question: str):
    # Defer to avoid interaction timeout
    await interaction.response.defer(ephemeral=True)

    lower_q = question.lower().strip()

    # --- 1) Winner-related questions (grounded in winners.json) ---
    winner_keywords = ["winner", "winners", "who won", "who is the winner", "who are the winners"]
    if any(k in lower_q for k in winner_keywords) and HACKATHON_WINNERS:
        # Try to match a specific hackathon name from the question
        matched = None
        for name, data in HACKATHON_WINNERS.items():
            name_lower = name.lower()
            if name_lower in lower_q or lower_q in name_lower:
                matched = data
                break

        # If we found a specific hackathon in the question
        if matched:
            hackathon_name = matched.get("hackathon", "Unknown hackathon")
            source = matched.get("source")
            if source == "announcement" and matched.get("announcement_text"):
                msg = (
                    f"🏆 Here’s the saved winner announcement for **{hackathon_name}**:\n\n"
                    f"{matched['announcement_text']}"
                )
            else:
                msg = (
                    f"🏆 Winner info for **{hackathon_name}**:\n"
                    f"• Team: **{matched.get('team', '—')}**\n"
                    f"• Project: {matched.get('project', '—')}\n"
                    f"• Prize: {matched.get('prize', '—')}"
                )
            await interaction.followup.send(msg, ephemeral=True)
            return
        else:
            # No specific hackathon detected → show a short recent winners summary
            valid_entries = [
                v for v in HACKATHON_WINNERS.values()
                if v.get("hackathon", "").strip().lower() not in ("winner", "winners")
            ]
            if not valid_entries:
                await interaction.followup.send(
                    "🏆 I don’t have any valid winners stored yet.",
                    ephemeral=True
                )
                return

            entries = valid_entries[-3:]
            lines = ["Here are some recent Hackeroos winners I know about:\n"]
            for item in reversed(entries):
                hackathon_name = item.get("hackathon", "Unknown")
                source = item.get("source")
                if source == "announcement" and item.get("announcement_text"):
                    lines.append(f"🏁 **{hackathon_name}**\n{item['announcement_text']}\n")
                else:
                    lines.append(
                        f"🏁 **{hackathon_name}**\n"
                        f"• Team: {item.get('team', '—')}\n"
                        f"• Project: {item.get('project', '—')}\n"
                        f"• Prize: {item.get('prize', '—')}\n"
                    )
            await interaction.followup.send("\n".join(lines), ephemeral=True)
            return

    # --- 2) Event / hackathon questions (grounded in /hackathons) ---
    event_keywords = ["hackathon", "event", "competition", "game jam"]
    time_keywords = ["next", "upcoming", "coming", "what's on", "whats on", "when"]

    if any(k in lower_q for k in event_keywords) and any(t in lower_q for t in time_keywords):
        events = await fetch_hackathons_from_github()
        if not events:
            await interaction.followup.send(
                "⚠️ I tried to look up current hackathons, but couldn’t fetch any just now.\n"
                "You can still check:\n"
                "• https://devpost.com/hackathons\n"
                "• https://mlh.io/events\n"
                "• https://www.hackeroos.com.au/#whats-on",
                ephemeral=True
            )
            return

        lines = ["🌍 Here are some live / upcoming hackathons I know about:\n"]
        for e in events[:8]:
            title = e.get("title", "Untitled")
            url = e.get("url", "#")
            source = e.get("source", "Unknown")
            lines.append(f"• **{title}** — ({source}) → {url}")
        lines.append("\nYou can also run `/hackathons` for an embed version.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        return

    # --- 3) For everything else, fall back to Hugging Face chat model ---
    if not HF_TOKEN:
        await interaction.followup.send(
            "⚠️ No Hugging Face token configured.\n"
            "Ask an admin to set `HF_TOKEN` (or `HUGGINGFACE_TOKEN`) in `.env`.",
            ephemeral=True
        )
        return

    try:
        client = OpenAI(
            base_url="https://router.huggingface.co/v1",
            api_key=HF_TOKEN,
        )

        completion = client.chat.completions.create(
            model=HF_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are Pika-Bot, a friendly Australian hackathon assistant for the "
                        "Hackeroos Discord community. Be concise, encouraging, and clear. "
                        "If the user asks about specific Hackeroos winners or upcoming events, "
                        "you should say you don't have that internal data here and ask them "
                        "to use the bot's built-in commands instead: /winners and /hackathons."
                    ),
                },
                {"role": "user", "content": question},
            ],
        )

        reply = completion.choices[0].message.content
        await interaction.followup.send(f"🦘 **Pika-Bot AI:** {reply}", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(
            f"⚠️ I couldn’t talk to Hugging Face Inference Providers:\n```{e}```",
            ephemeral=True
        )

# -------------------------------------------------
# WINNERS COMMANDS (manual + display)
# -------------------------------------------------
@bot.tree.command(name="set-winner", description="Set the winner for a hackathon (admin only) 🏆")
async def set_winner(
    interaction: discord.Interaction,
    hackathon: str,
    team: str,
    project: str = "",
    prize: str = ""
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("⚠️ Only admins can use `/set-winner`.", ephemeral=True)
        return

    HACKATHON_WINNERS[hackathon] = {
        "hackathon": hackathon,
        "team": team,
        "project": project or "—",
        "prize": prize or "—",
        "source": "manual",
    }
    save_winners()

    await interaction.response.send_message(
        f"🏆 Winner saved for **{hackathon}**:\n"
        f"• Team: **{team}**\n"
        f"{'• Project: ' + project if project else ''}\n"
        f"{'• Prize: ' + prize if prize else ''}",
        ephemeral=True
    )


@bot.tree.command(name="winners", description="Show recent Hackeroos hackathon winners 🏆")
async def winners(interaction: discord.Interaction):
    if not HACKATHON_WINNERS:
        await interaction.response.send_message("🏆 No winners saved yet.", ephemeral=True)
        return

    # Filter out obviously bad entries like hackathon == "winner"
    valid_entries = [
        v for v in HACKATHON_WINNERS.values()
        if v.get("hackathon", "").strip().lower() not in ("winner", "winners")
    ]

    if not valid_entries:
        await interaction.response.send_message("🏆 No valid winners saved yet.", ephemeral=True)
        return

    # show last 3 entries
    entries = valid_entries[-3:]

    embed = discord.Embed(
        title="🏆 Hackeroos Hackathon Winners",
        color=0xfbbf24
    )

    for item in reversed(entries):
        hackathon_name = item.get("hackathon", "Unknown")
        source = item.get("source")

        # If this came from an announcement and we have the full text,
        # show the exact announcement content:
        if source == "announcement" and item.get("announcement_text"):
            embed.add_field(
                name=f"🏁 {hackathon_name}",
                value=item["announcement_text"],
                inline=False,
            )
        else:
            # Manual (or legacy) entry – show structured fields
            embed.add_field(
                name=f"🏁 {hackathon_name}",
                value=(
                    f"• **Team:** {item.get('team', '—')}\n"
                    f"• **Project:** {item.get('project', '—')}\n"
                    f"• **Prize:** {item.get('prize', '—')}"
                ),
                inline=False,
            )

    embed.set_footer(text="Configured via /set-winner or announcements • Pika-Bot 🦘")
    await interaction.response.send_message(embed=embed, ephemeral=False)

# -------------------------------------------------
# 8.3) !sync — manual sync for admins
# -------------------------------------------------
@bot.command(name="sync")
@commands.has_permissions(administrator=True)
async def sync_cmd(ctx: commands.Context):
    try:
        synced = await bot.tree.sync()
        await ctx.send(f"✅ Slash commands synced again ({len(synced)} commands).")
    except Exception as e:
        await ctx.send(f"⚠️ Could not sync slash commands:\n`{e}`")

# -------------------------------------------------
# 9) RUN
# -------------------------------------------------
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing in `.env` as DISCORD_TOKEN!")

bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)
