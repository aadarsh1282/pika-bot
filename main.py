import os
import re
import logging
import json
import asyncio
import time
from collections import defaultdict, deque
from typing import Dict, List
from datetime import datetime, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv

import httpx  # for GitHub JSON

from openai import OpenAI  # HF router client

# basic env + config
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Hugging Face token + model for /ask (via router.huggingface.co/v1)
HF_TOKEN = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN")
HF_MODEL = os.getenv(
    "HUGGINGFACE_MODEL",
    "Qwen/Qwen2.5-72B-Instruct"
)

# channel names used by the bot (must match server)
WELCOME_CHANNEL_NAME = "welcome"
HACKATHON_CHANNEL_NAME = "all-hackathons"
ANNOUNCEMENTS_CHANNEL_NAME = "announcements"
MOD_LOG_CHANNEL_NAME = "mod-logs"

# roles used by the bot
ROLE_VERIFY = "Verified Hackeroos"
ROLE_TECH = "Tech Hackeroos"
ROLE_COMMUNITY = "Community Hackeroos"
QUARANTINE_ROLE_NAME = "Quarantined"

# basic word filter for a PG server
BLOCKED_WORDS = [
    "shit", "fuck", "bitch", "bastard", "cunt", "slut", "whore",
    "dick", "pussy", "fag", "faggot", "nigga", "nigger",
    "bloody hell", "asshole", "retard", "moron", "idiot",
    "porn", "nsfw", "sex", "cum", "jerk off", "jerking", "rape",
]

# data files stored next to the bot
WINNERS_FILE = "winners.json"
STRIKES_FILE = "strikes.json"

HACKATHON_WINNERS: Dict[str, dict] = {}
USER_STRIKES: Dict[str, int] = {}  # key = f"{guild_id}:{user_id}"

# cache last hackathon list for auto alerts
LAST_HACKATHONS: List[dict] = []

# recent joins per guild for raid detection
RECENT_JOINS: Dict[int, deque] = defaultdict(deque)

# GitHub JSON with merged hackathons (updated by GH Actions)
HACKATHONS_JSON_URL = os.getenv(
    "HACKATHONS_JSON_URL",
    "https://raw.githubusercontent.com/aadarsh1282/pika-bot/main/data/hackathons.json",
)

# simple thresholds – can tune later
RAID_JOIN_WINDOW_SECONDS = 30               # look at joins in this window
RAID_JOIN_THRESHOLD = 5                     # joins in that window to flag raid
NEW_ACCOUNT_MAX_AGE_SECONDS = 24 * 60 * 60  # treat <24h old as “very new”

MENTION_SPAM_THRESHOLD = 6                  # count mentions in one message
EMOJI_SPAM_THRESHOLD = 15                   # count emoji in one message

# strip most emoji so we can clean titles like "🎃 WINNERS 🎃"
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "]+",
    flags=re.UNICODE,
)


def strip_emojis(text: str) -> str:
    return EMOJI_PATTERN.sub("", text)


# very basic logging to file
handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("pika-bot")

# intents + bot
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# -------- hackathon fetch / auto alerts --------


async def fetch_hackathons_from_github() -> List[dict]:
    """
    Fetch merged hackathon list from GitHub.
    Expected: list of dicts with title, url, start_date, location, source.
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
        log.warning("Could not fetch hackathons JSON: %s", e)
        return []


async def pin_and_unpin(message: discord.Message):
    """Pin new hackathon embed and unpin old one in the same channel."""
    channel = message.channel
    try:
        pinned = await channel.pins()
        if pinned:
            try:
                await pinned[0].unpin()
            except Exception:
                pass

        await message.pin(reason="New Hackathon Announcement")
    except Exception as e:
        log.warning("Could not pin/unpin: %s", e)


async def auto_alerts_loop():
    """Background task: poll GitHub JSON and announce new hackathons."""
    global LAST_HACKATHONS
    await bot.wait_until_ready()
    log.info("Auto-alerts loop started (every 3 hours)")

    while not bot.is_closed():
        try:
            events = await fetch_hackathons_from_github()
            if not events:
                log.warning("Hackathons JSON empty or unreachable")
                await asyncio.sleep(3 * 60 * 60)
                continue

            # first run – just cache
            if not LAST_HACKATHONS:
                LAST_HACKATHONS = events
                log.info("First run: cached %d hackathons", len(events))
                await asyncio.sleep(3 * 60 * 60)
                continue

            # detect new events by URL
            old_urls = {e.get("url") for e in LAST_HACKATHONS if e.get("url")}
            new_events = [
                e for e in events
                if e.get("url") and e["url"] not in old_urls
            ]

            if new_events:
                log.info("New hackathons detected: %d", len(new_events))

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

            LAST_HACKATHONS = events[:100]

        except Exception as e:
            log.exception("auto_alerts_loop crashed: %s", e)

        await asyncio.sleep(3 * 60 * 60)


# -------- winners load/save --------


def load_winners():
    global HACKATHON_WINNERS
    if os.path.exists(WINNERS_FILE):
        try:
            with open(WINNERS_FILE, "r", encoding="utf-8") as f:
                HACKATHON_WINNERS = json.load(f)
            log.info("Loaded %d winners from %s", len(HACKATHON_WINNERS), WINNERS_FILE)
        except Exception as e:
            log.warning("Could not load winners: %s", e)
            HACKATHON_WINNERS = {}
    else:
        HACKATHON_WINNERS = {}


def save_winners():
    try:
        with open(WINNERS_FILE, "w", encoding="utf-8") as f:
            json.dump(HACKATHON_WINNERS, f, indent=2, ensure_ascii=False)
        log.info("Saved %d winners to %s", len(HACKATHON_WINNERS), WINNERS_FILE)
    except Exception as e:
        log.warning("Could not save winners: %s", e)


# -------- strikes storage (for moderation) --------


def load_strikes():
    global USER_STRIKES
    if os.path.exists(STRIKES_FILE):
        try:
            with open(STRIKES_FILE, "r", encoding="utf-8") as f:
                USER_STRIKES = json.load(f)
            log.info("Loaded %d strikes from %s", len(USER_STRIKES), STRIKES_FILE)
        except Exception as e:
            log.warning("Could not load strikes: %s", e)
            USER_STRIKES = {}
    else:
        USER_STRIKES = {}


def save_strikes():
    try:
        with open(STRIKES_FILE, "w", encoding="utf-8") as f:
            json.dump(USER_STRIKES, f, indent=2, ensure_ascii=False)
        log.info("Saved %d strikes to %s", len(USER_STRIKES), STRIKES_FILE)
    except Exception as e:
        log.warning("Could not save strikes: %s", e)


def _strike_key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}:{user_id}"


def add_strike(guild: discord.Guild, user: discord.abc.User, reason: str) -> int:
    """Increment strike counter for a user and return total."""
    key = _strike_key(guild.id, user.id)
    USER_STRIKES[key] = USER_STRIKES.get(key, 0) + 1
    save_strikes()
    log.info(
        "Strike added: %s in %s (total %d) — reason: %s",
        user, guild.name, USER_STRIKES[key], reason
    )
    return USER_STRIKES[key]


def get_strikes(guild: discord.Guild, user: discord.abc.User) -> int:
    return USER_STRIKES.get(_strike_key(guild.id, user.id), 0)


# -------- mod log helpers --------


async def get_mod_log_channel(guild: discord.Guild) -> discord.TextChannel | None:
    if guild is None:
        return None
    chan = discord.utils.get(guild.text_channels, name=MOD_LOG_CHANNEL_NAME)
    return chan


async def send_mod_log(
    guild: discord.Guild,
    title: str,
    description: str = "",
    *,
    user: discord.abc.User | None = None,
    channel: discord.abc.GuildChannel | None = None,
    extra: dict | None = None,
):
    """Send a simple embed to #mod-logs if it exists."""
    chan = await get_mod_log_channel(guild)
    if not chan:
        return

    embed = discord.Embed(
        title=title,
        description=description or "",
        color=0xff5555,
        timestamp=datetime.now(timezone.utc),
    )

    if user:
        embed.add_field(name="User", value=f"{user} (`{user.id}`)", inline=False)
    if channel:
        embed.add_field(name="Channel", value=f"{channel.mention} (`{channel.id}`)", inline=False)
    if extra:
        for k, v in extra.items():
            embed.add_field(name=k, value=str(v), inline=False)

    embed.set_footer(text="Pika-Bot • Moderation Log")
    try:
        await chan.send(embed=embed)
    except discord.Forbidden:
        pass
    except Exception as e:
        log.warning("Could not send mod log: %s", e)


# -------- raid detection --------


async def handle_possible_raid(member: discord.Member):
    """
    Called on each join.
    Tracks join timestamps per guild and applies slowmode / kicks new accounts
    if a bunch of people join quickly.
    """
    guild = member.guild
    now = time.time()
    dq = RECENT_JOINS[guild.id]

    dq.append(now)

    # drop old joins
    while dq and now - dq[0] > RAID_JOIN_WINDOW_SECONDS:
        dq.popleft()

    join_count = len(dq)

    if join_count >= RAID_JOIN_THRESHOLD:
        # looks like a raid pattern
        log.warning(
            "Potential raid in %s (%d joins in %ds)",
            guild.name, join_count, RAID_JOIN_WINDOW_SECONDS
        )
        await send_mod_log(
            guild,
            "Potential Raid Detected",
            f"{join_count} new accounts joined within {RAID_JOIN_WINDOW_SECONDS} seconds.",
            user=member,
            extra={"Action": "Slowmode + kick very new accounts"},
        )

        # enable slowmode on most text channels
        for ch in guild.text_channels:
            try:
                perms = ch.permissions_for(guild.me)
                if perms.manage_channels and ch.name != MOD_LOG_CHANNEL_NAME:
                    await ch.edit(slowmode_delay=10)
            except Exception:
                continue

        # optionally kick accounts that are very new
        try:
            account_age = (datetime.now(timezone.utc) - member.created_at).total_seconds()
            if account_age <= NEW_ACCOUNT_MAX_AGE_SECONDS:
                reason = f"Auto-kick during suspected raid (account age {int(account_age)}s)"
                await member.kick(reason=reason)
                await send_mod_log(
                    guild,
                    "Auto-kick (Raid Protection)",
                    description=reason,
                    user=member,
                    extra={"Account Age (seconds)": int(account_age)},
                )
        except discord.Forbidden:
            log.warning("Could not auto-kick suspicious user %s", member)
        except Exception as e:
            log.warning("Error while auto-kicking in raid mode: %s", e)


# -------- lifecycle events --------


@bot.event
async def on_ready():
    load_winners()
    load_strikes()

    bot.loop.create_task(auto_alerts_loop())

    try:
        await bot.tree.sync()
        log.info("Slash commands synced globally")
        print("✅ Slash commands synced globally.")
    except Exception as e:
        log.warning("Error syncing slash commands: %s", e)
        print("Error syncing slash commands:", e)

    log.info(
        "Pika-Bot online | Guilds: %d | Hackathons cached: %d",
        len(bot.guilds), len(LAST_HACKATHONS)
    )

    print("🦘------------------------------------------------------------")
    print(f"⚡ {bot.user.name} is online")
    print(f"🏠 Connected servers: {len(bot.guilds)}")
    print("🦘------------------------------------------------------------")

    await bot.change_presence(
        activity=discord.Game(name="Helping Hackeroos innovate ⚡🦘"),
        status=discord.Status.online
    )

    # one-time hello in hackathon channel if it exists
    for guild in bot.guilds:
        hack_channel = discord.utils.get(guild.text_channels, name=HACKATHON_CHANNEL_NAME)
        if hack_channel:
            try:
                await hack_channel.send(
                    "🌍 **Pika-Bot** is live! Use `/hackathons` to see global hackathons ⚡"
                )
            except discord.Forbidden:
                pass


@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild

    # basic raid check
    await handle_possible_raid(member)

    # DM welcome
    try:
        await member.send(
            f"Welcome to Hackeroos, {member.name}! 🦘💛\n"
            f"I’m **Pika-Bot**. Use `/pika-help` in the server to see what I can do.\n"
            f"To unlock channels, run `/verify` in #{WELCOME_CHANNEL_NAME}."
        )
    except discord.Forbidden:
        log.warning("Could not DM %s", member.name)

    # public welcome
    channel = discord.utils.get(guild.text_channels, name=WELCOME_CHANNEL_NAME)
    if channel:
        await channel.send(
            f"⚡ G’day {member.mention}! Welcome to **{guild.name}** — run `/verify` to get access!"
        )

    # log join
    await send_mod_log(
        guild,
        "Member Joined",
        user=member,
        extra={"Account created": member.created_at.strftime("%Y-%m-%d %H:%M UTC")},
    )


@bot.event
async def on_member_remove(member: discord.Member):
    guild = member.guild
    await send_mod_log(
        guild,
        "Member Left",
        user=member,
    )


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.abc.User):
    await send_mod_log(
        guild,
        "Member Banned",
        user=user,
    )


@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.abc.User):
    await send_mod_log(
        guild,
        "Member Unbanned",
        user=user,
    )


@bot.event
async def on_message_delete(message: discord.Message):
    if message.author == bot.user:
        return
    if not message.guild or not isinstance(message.channel, discord.TextChannel):
        return

    await send_mod_log(
        message.guild,
        "Message Deleted",
        user=message.author,
        channel=message.channel,
        extra={"Content": message.content or "(no content / embed only)"},
    )


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author == bot.user:
        return
    if not before.guild or not isinstance(before.channel, discord.TextChannel):
        return
    if before.content == after.content:
        return

    await send_mod_log(
        before.guild,
        "Message Edited",
        user=before.author,
        channel=before.channel,
        extra={
            "Before": before.content or "(empty)",
            "After": after.content or "(empty)",
        },
    )


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    # for DMs just run commands, no moderation
    if not isinstance(message.channel, discord.TextChannel):
        await bot.process_commands(message)
        return

    guild = message.guild
    author = message.author
    is_admin = author.guild_permissions.administrator or author.guild_permissions.manage_guild

    lowered = (message.content or "").lower()

    # simple bad-word filter
    if any(bad in lowered for bad in BLOCKED_WORDS):
        try:
            await message.delete()
        except discord.Forbidden:
            pass

        await message.channel.send(f"{author.mention}, let’s keep it clean, mate! 🧹")
        await send_mod_log(
            guild,
            "Message Deleted (Bad Word Filter)",
            user=author,
            channel=message.channel,
            extra={"Content": message.content},
        )
        return

    # mention spam check
    if not is_admin:
        mention_count = len(message.mentions)
        if message.mention_everyone:
            mention_count += 5
        if message.mention_roles:
            mention_count += len(message.role_mentions) * 2

        if mention_count >= MENTION_SPAM_THRESHOLD:
            try:
                await message.delete()
            except discord.Forbidden:
                pass

            strikes = add_strike(guild, author, reason=f"Mention spam ({mention_count} mentions)")
            await send_mod_log(
                guild,
                "Mention Spam Detected",
                user=author,
                channel=message.channel,
                extra={
                    "Mentions": mention_count,
                    "Message": message.content,
                    "Strikes (after)": strikes,
                },
            )

            if strikes >= 3:
                try:
                    await guild.ban(author, reason="Auto-ban: 3 strikes (mention spam)")
                    await send_mod_log(
                        guild,
                        "Auto-ban (3 Strikes)",
                        user=author,
                        extra={"Reason": "Mention spam / 3 strikes"},
                    )
                except discord.Forbidden:
                    log.warning("Could not auto-ban %s", author)
            else:
                try:
                    await message.channel.send(
                        f"{author.mention}, please don’t spam mentions. "
                        f"You now have **{strikes} strike(s)** (auto-ban at 3)."
                    )
                except discord.Forbidden:
                    pass

            return

    # emoji spam check
    if not is_admin and message.content:
        emojis_found = EMOJI_PATTERN.findall(message.content)
        emoji_count = len(emojis_found)

        if emoji_count >= EMOJI_SPAM_THRESHOLD:
            try:
                await message.delete()
            except discord.Forbidden:
                pass

            strikes = add_strike(guild, author, reason=f"Emoji spam ({emoji_count} emojis)")
            await send_mod_log(
                guild,
                "Emoji Spam Detected",
                user=author,
                channel=message.channel,
                extra={
                    "Emoji count": emoji_count,
                    "Message": message.content,
                    "Strikes (after)": strikes,
                },
            )

            if strikes >= 3:
                try:
                    await guild.ban(author, reason="Auto-ban: 3 strikes (emoji spam)")
                    await send_mod_log(
                        guild,
                        "Auto-ban (3 Strikes)",
                        user=author,
                        extra={"Reason": "Emoji spam / 3 strikes"},
                    )
                except discord.Forbidden:
                    log.warning("Could not auto-ban %s", author)
            else:
                try:
                    await message.channel.send(
                        f"{author.mention}, please don’t spam emojis. "
                        f"You now have **{strikes} strike(s)** (auto-ban at 3)."
                    )
                except discord.Forbidden:
                    pass

            return

    # winner auto-capture from #announcements
    if (
        isinstance(message.channel, discord.TextChannel)
        and message.channel.name == ANNOUNCEMENTS_CHANNEL_NAME
        and message.author.guild_permissions.administrator
        and "winner" in lowered
    ):
        hackathon = team = project = prize = None

        # structured: "Winner: Hackathon | Team: ... | Project: ... | Prize: ..."
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
                match = None

        # fallback style (multi-line announcements)
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


# -------- slash commands --------


@bot.tree.command(name="pika-help", description="Show all Pika-Bot slash commands 🦘")
async def pika_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Pika-Bot — Hackeroos Helper",
        description="Slash commands currently available:",
        color=0xffc300
    )

    for cmd in bot.tree.get_commands():
        desc = cmd.description or "No description provided"
        embed.add_field(name=f"/{cmd.name}", value=desc, inline=False)

    embed.set_footer(text="Built by Pika-Bots (AIHE Group 19)")
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
            "• verify users with `/verify`\n"
            "• polls\n"
            "• global hackathon feed\n"
            "• `/ask` with Hugging Face model\n"
            "• winners tracking via `/set-winner` + `/winners`"
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
        title="Hackeroos Poll",
        description=question,
        color=0xffc300
    )
    msg = await interaction.channel.send(embed=embed)
    await msg.add_reaction("👍")
    await msg.add_reaction("👎")
    await interaction.response.send_message("Poll created ✅", ephemeral=True)


@bot.tree.command(name="hackathons", description="Show upcoming global hackathons 🌍")
async def hackathons(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    events = await fetch_hackathons_from_github()

    if not events:
        embed = discord.Embed(
            title="No Live Hackathons Found (Right Now)",
            description=(
                "Couldn’t find any future events in the JSON feed.\n\n"
                "You can still browse manually here:"
            ),
            color=0xffc300
        )
        embed.add_field(
            name="Devpost",
            value="[devpost.com/hackathons](https://devpost.com/hackathons)",
            inline=False
        )
        embed.add_field(
            name="MLH",
            value="[mlh.io/events](https://mlh.io/events)",
            inline=False
        )
        embed.add_field(
            name="Hack Club",
            value="[events.hackclub.com](https://events.hackclub.com/)",
            inline=False
        )
        embed.add_field(
            name="Hackathon.com",
            value="[hackathon.com](https://www.hackathon.com/city/global)",
            inline=False
        )
        embed.add_field(
            name="Hackeroos What's On",
            value="[hackeroos.com.au/#whats-on](https://www.hackeroos.com.au/#whats-on)",
            inline=False
        )
        embed.set_footer(text="Pika-Bot • /hackathons uses the same sources.")
        await interaction.followup.send(embed=embed)
        return

    embed = discord.Embed(
        title="Live Global Hackathons",
        description="Some current / upcoming hackathons from the GitHub feed:",
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
    embed.set_footer(text="Sources: MLH, Lu.ma, Hack Club, etc. • Pika-Bot ⚡")

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
            title="New Global Hackathons!",
            description="Fresh hackathons from the JSON feed:",
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

    await interaction.followup.send(
        "✅ Posted latest hackathons to #all-hackathons (where available).",
        ephemeral=True
    )


@bot.tree.command(name="refresh-hackathons", description="Force refresh hackathons feed (admin only)")
async def refresh_hackathons(interaction: discord.Interaction):
    global LAST_HACKATHONS
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("⚠️ Only admins can use `/refresh-hackathons`.", ephemeral=True)
        return

    events = await fetch_hackathons_from_github()
    LAST_HACKATHONS = events
    await interaction.followup.send(
        f"✅ Hackathons cache refreshed ({len(events)} events cached).",
        ephemeral=True
    )


@bot.tree.command(name="faq", description="Common questions about Hackeroos / Pika-Bot")
async def faq(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Hackeroos FAQ",
        description="Quick answers for new members:",
        color=0x3b82f6
    )
    embed.add_field(
        name="1. I just joined, what now?",
        value=f"Go to **#{WELCOME_CHANNEL_NAME}** and run `/verify` to unlock channels.",
        inline=False
    )
    embed.add_field(
        name="2. How do I see global hackathons?",
        value="Use `/hackathons`.",
        inline=False
    )
    embed.add_field(
        name="3. Can I ask AI-style questions?",
        value="Yes, use `/ask <your question>`.",
        inline=False
    )
    embed.add_field(
        name="4. How do I see past winners?",
        value="Use `/winners`.",
        inline=False
    )
    embed.add_field(
        name="5. Who built this?",
        value="Pika-Bots — AIHE Group 19.",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="status", description="Bot health check")
async def status(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"✅ Online | Servers: {len(bot.guilds)} | Winners stored: {len(HACKATHON_WINNERS)}",
        ephemeral=True
    )


@bot.tree.command(name="ask", description="Ask Pika-Bot in natural language 🤖")
async def ask(interaction: discord.Interaction, question: str):
    await interaction.response.defer(ephemeral=True)

    lower_q = question.lower().strip()

    # winner questions → answer from winners.json
    winner_keywords = ["winner", "winners", "who won", "who is the winner", "who are the winners"]
    if any(k in lower_q for k in winner_keywords) and HACKATHON_WINNERS:
        matched = None
        for name, data in HACKATHON_WINNERS.items():
            name_lower = name.lower()
            if name_lower in lower_q or lower_q in name_lower:
                matched = data
                break

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

    # event questions → use same source as /hackathons
    event_keywords = ["hackathon", "event", "competition", "game jam"]
    time_keywords = ["next", "upcoming", "coming", "what's on", "whats on", "when"]

    if any(k in lower_q for k in event_keywords) and any(t in lower_q for t in time_keywords):
        events = await fetch_hackathons_from_github()
        if not events:
            embed = discord.Embed(
                title="No Live Hackathons Found (Right Now)",
                description=(
                    "I tried to look up current hackathons from the JSON feed and got nothing.\n\n"
                    "You can manually browse here:"
                ),
                color=0xffc300
            )
            embed.add_field(
                name="Devpost",
                value="[devpost.com/hackathons](https://devpost.com/hackathons)",
                inline=False
            )
            embed.add_field(
                name="MLH",
                value="[mlh.io/events](https://mlh.io/events)",
                inline=False
            )
            embed.add_field(
                name="Hack Club",
                value="[events.hackclub.com](https://events.hackclub.com/)",
                inline=False
            )
            embed.add_field(
                name="Hackathon.com",
                value="[hackathon.com](https://www.hackathon.com/city/global)",
                inline=False
            )
            embed.add_field(
                name="Hackeroos What's On",
                value="[hackeroos.com.au/#whats-on](https://www.hackeroos.com.au/#whats-on)",
                inline=False
            )
            embed.set_footer(text="Pika-Bot • /hackathons uses the same sources.")
            await interaction.followup.send(embed=embed, ephemeral=True)
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

    # everything else → send to HF router
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
                        "ask them to use the bot's commands instead: /winners and /hackathons."
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

    valid_entries = [
        v for v in HACKATHON_WINNERS.values()
        if v.get("hackathon", "").strip().lower() not in ("winner", "winners")
    ]

    if not valid_entries:
        await interaction.response.send_message("🏆 No valid winners saved yet.", ephemeral=True)
        return

    entries = valid_entries[-3:]

    embed = discord.Embed(
        title="Hackeroos Hackathon Winners",
        color=0xfbbf24
    )

    for item in reversed(entries):
        hackathon_name = item.get("hackathon", "Unknown")
        source = item.get("source")

        if source == "announcement" and item.get("announcement_text"):
            embed.add_field(
                name=f"🏁 {hackathon_name}",
                value=item["announcement_text"],
                inline=False,
            )
        else:
            embed.add_field(
                name=f"🏁 {hackathon_name}",
                value=(
                    f"• **Team:** {item.get('team', '—')}\n"
                    f"• **Project:** {item.get('project', '—')}\n"
                    f"• **Prize:** {item.get('prize', '—')}"
                ),
                inline=False,
            )

    embed.set_footer(text="Configured via /set-winner or announcements • Pika-Bot")
    await interaction.response.send_message(embed=embed, ephemeral=False)


@bot.command(name="sync")
@commands.has_permissions(administrator=True)
async def sync_cmd(ctx: commands.Context):
    try:
        synced = await bot.tree.sync()
        await ctx.send(f"✅ Slash commands synced again ({len(synced)} commands).")
    except Exception as e:
        await ctx.send(f"⚠️ Could not sync slash commands:\n`{e}`")


# run the bot
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing in `.env` as DISCORD_TOKEN!")

bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)
