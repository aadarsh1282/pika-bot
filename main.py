"""
Pika-Bot — Hackeroos Discord Bot (Refactored v2.1)
Built by Pika-Bots (AIHE Group 19)

Production-ready:
- Async main + graceful shutdown (SIGTERM/SIGINT supported)
- Background tasks started once per process
- Async file I/O (aiofiles)
- Reusable HTTP client (httpx.AsyncClient)
- State management with asyncio.Lock
- Bot-only unpin (won't unpin human pins)
- Raid detection safe bot member lookup
- /poll channel guard (no None crash)
- Better moderation normalization for blocked words

Deploy notes:
- Works best with discord.py 2.x
- Set DISCORD_TOKEN, HF_TOKEN (optional for /ask), and HACKATHONS_API_BASE (optional)
"""

from __future__ import annotations

import os
import re
import json
import time
import asyncio
import signal
import logging
import unicodedata
from dataclasses import dataclass, field
from collections import defaultdict, deque
from typing import Dict, List, Tuple
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands
from dotenv import load_dotenv

import httpx
import aiofiles

from openai import OpenAI

# -------------------------------------------------
# 1) CONSTANTS
# -------------------------------------------------

# Display limits
MAX_EVENTS_IN_EMBED = 10
MAX_EVENTS_IN_HACKATHONS_CMD = 20
RECENT_WINNERS_DISPLAY_COUNT = 3
MAX_HACKATHON_NAME_LENGTH = 100
MAX_TEAM_NAME_LENGTH = 100
MAX_PROJECT_LENGTH = 200
MAX_PRIZE_LENGTH = 100

# Raid detection
RAID_JOIN_WINDOW_SECONDS = 30
RAID_JOIN_THRESHOLD = 5
NEW_ACCOUNT_MAX_AGE_SECONDS = 24 * 60 * 60
RAID_BACKOFF_SECONDS = 3600
MAX_CONSECUTIVE_FAILURES = 5
RAID_SLOWMODE_DELAY = 10

# Spam thresholds
MENTION_SPAM_THRESHOLD = 6
EMOJI_SPAM_THRESHOLD = 15
AUTO_BAN_STRIKE_THRESHOLD = 3

# Intervals (in hours)
AUTO_ALERT_INTERVAL_HOURS = 24 * 7

# -------------------------------------------------
# 2) ENV + CONFIG
# -------------------------------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Hugging Face token + model for /ask (via router.huggingface.co/v1)
HF_TOKEN = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN")
HF_MODEL = os.getenv("HUGGINGFACE_MODEL", "Qwen/Qwen2.5-72B-Instruct")

# Channel names used by the bot (must match server)
WELCOME_CHANNEL_NAME = "welcome-verify"
HACKATHON_CHANNEL_NAME = "all-hackathons"
ANNOUNCEMENTS_CHANNEL_NAME = "announcements"
MOD_LOG_CHANNEL_NAME = "mod-logs"

# Roles used by the bot
ROLE_VERIFY = "Verified Hackeroos"
ROLE_TECH = "Tech Hackeroos"
ROLE_COMMUNITY = "Community Hackeroos"
QUARANTINE_ROLE_NAME = "Quarantined"

# Data files stored next to the bot
WINNERS_FILE = "winners.json"
STRIKES_FILE = "strikes.json"
BLOCKED_WORDS_FILE = os.getenv("BLOCKED_WORDS_FILE", "blocked_words.json")

# Hackathons backend
HACKATHONS_API_BASE = os.getenv(
    "HACKATHONS_API_BASE",
    "https://hackeroos-insights-api-production.up.railway.app",
)
HACKATHONS_JSON_URL = os.getenv(
    "HACKATHONS_JSON_URL",
    "https://raw.githubusercontent.com/aadarsh1282/pika-bot/main/data/hackathons.json",
)

# -------------------------------------------------
# 3) DEFAULT BLOCKED WORDS (fallback if no JSON file)
# -------------------------------------------------
DEFAULT_BLOCKED_WORDS = [
    "shit", "fuck", "bitch", "bastard", "cunt", "slut", "whore",
    "dick", "pussy", "fag", "faggot", "nigga", "nigger",
    "bloody hell", "asshole", "retard", "moron", "idiot",
    "porn", "nsfw", "sex", "cum", "jerk off", "jerking", "rape",
]


def load_blocked_words() -> List[str]:
    """Load blocked words from JSON file or use defaults."""
    if os.path.exists(BLOCKED_WORDS_FILE):
        try:
            with open(BLOCKED_WORDS_FILE, "r", encoding="utf-8") as f:
                words = json.load(f)
                if isinstance(words, list):
                    return [w.lower() for w in words if isinstance(w, str)]
        except Exception as e:
            logging.warning("Could not load blocked words file: %s", e)
    return DEFAULT_BLOCKED_WORDS


BLOCKED_WORDS = load_blocked_words()

# -------------------------------------------------
# 4) MONTH MAP FOR DATE PARSING
# -------------------------------------------------
MONTH_MAP = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

# -------------------------------------------------
# 5) REGEX PATTERNS
# -------------------------------------------------
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

WINNER_PATTERN = re.compile(
    r"(?:.*?)(?:winner|winners)\s*[:\-–]?\s*(?P<hackathon>[^|\n]+)"
    r"(?:\|\s*team:\s*(?P<team>[^|]+))?"
    r"(?:\|\s*project:\s*(?P<project>[^|]+))?"
    r"(?:\|\s*prize:\s*(?P<prize>[^|]+))?",
    re.IGNORECASE,
)

# -------------------------------------------------
# 6) LOGGING SETUP
# -------------------------------------------------
handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("pika-bot")

# -------------------------------------------------
# 7) UTILITY FUNCTIONS
# -------------------------------------------------
def strip_emojis(text: str) -> str:
    """Remove emoji characters from text."""
    return EMOJI_PATTERN.sub("", text)


def normalize_text(text: str) -> str:
    """
    Normalize text for word filtering:
    - Remove accents
    - Handle l33t speak
    - Remove spacing tricks
    """
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")

    # Remove spaces between single characters (l e e t)
    cleaned = re.sub(r"(?<=\b\w)\s+(?=\w\b)", "", ascii_text)

    substitutions = {
        "0": "o", "1": "i", "3": "e", "4": "a",
        "5": "s", "@": "a", "$": "s", "!": "i"
    }
    for old, new in substitutions.items():
        cleaned = cleaned.replace(old, new)

    return cleaned.lower()


def contains_blocked_word(text: str) -> bool:
    normalized = normalize_text(text)
    return any(bad in normalized for bad in BLOCKED_WORDS)


def is_online_event(event: dict) -> bool:
    loc = (event.get("location") or "").lower()
    mode = (event.get("mode") or "").lower()
    keywords = ("online", "virtual", "remote", "digital")
    return any(kw in loc or kw in mode for kw in keywords)


def has_valid_date(event: dict) -> bool:
    raw = (event.get("start_date") or "").strip()
    return bool(raw)


def sanitize_input(text: str, max_length: int = 100) -> str:
    if not text:
        return ""
    cleaned = text.strip()[:max_length]
    return discord.utils.escape_markdown(cleaned)


def parse_iso_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None

    s = date_str.strip()

    # ISO with Z
    try:
        if s.endswith("Z"):
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        pass

    # ISO with tz or plain date
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            continue

    # Devpost-style
    matches = re.findall(r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})", s)
    if matches:
        month_name, day_str, year_str = matches[0]
        month = MONTH_MAP.get(month_name.lower()[:3]) or MONTH_MAP.get(month_name.lower())
        try:
            if month:
                return datetime(int(year_str), month, int(day_str), tzinfo=timezone.utc)
        except Exception:
            pass

    # MLH-style
    m = re.search(
        r"([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?"
        r"(?:\s*-\s*\d{1,2}(?:st|nd|rd|th)?)?"
        r"(?:,\s*(\d{4}))?",
        s,
    )
    if m:
        month_name = m.group(1)
        day_str = m.group(2)
        year_str = m.group(3)

        month = MONTH_MAP.get(month_name.lower()[:3]) or MONTH_MAP.get(month_name.lower())
        if month:
            try:
                day = int(day_str)
                if year_str:
                    year = int(year_str)
                else:
                    now = datetime.now(timezone.utc)
                    year = now.year
                    try_date = datetime(year, month, day, tzinfo=timezone.utc)
                    if try_date < now:
                        year += 1
                return datetime(year, month, day, tzinfo=timezone.utc)
            except Exception:
                pass

    # Last resort YYYY-MM-DD
    m = re.search(r"\d{4}-\d{2}-\d{2}", s)
    if m:
        try:
            return datetime.strptime(m.group(0), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            pass

    return None


def infer_time_window(question: str) -> Tuple[int | None, str]:
    q = question.lower()
    mappings = [
        (["next week", "coming week", "upcoming week"], 7, "the next 7 days"),
        (["this weekend", "on the weekend"], 4, "this weekend"),
        (["next weekend"], 7, "next weekend"),
        (["today", "tonight"], 1, "today"),
        (["tomorrow"], 2, "tomorrow (and the following day)"),
        (["next month"], 31, "the next month"),
        (["this month"], 31, "this month"),
        (["soon", "coming up", "upcoming"], 14, "the next couple of weeks"),
    ]
    for keywords, days, label in mappings:
        if any(kw in q for kw in keywords):
            return days, label
    return None, "upcoming"

# -------------------------------------------------
# 7.5) ANNOUNCEMENT BROADCAST 
# -------------------------------------------------
BROADCAST_CHANNELS = [
    "all-hackathons",
    "find-a-team",
    "pika-bots",
    "coworking-chat",
]


async def broadcast_announcement(message: discord.Message) -> None:
    guild = message.guild
    if not guild:
        return
    if not isinstance(message.channel, discord.TextChannel):
        return

    # Only mirror announcements channel
    if message.channel.name != ANNOUNCEMENTS_CHANNEL_NAME:
        return

    # Prevent loops / spam
    if message.author.bot:
        return

    content = (message.content or "").strip()

    embed = discord.Embed(
        title="📢 Announcement",
        description=content[:4000] if content else "(no text)",
        color=0xfbbf24,
        timestamp=message.created_at,
    )
    embed.set_author(
        name=message.author.display_name,
        icon_url=message.author.display_avatar.url if message.author.display_avatar else None
    )
    embed.set_footer(text=f"From #{message.channel.name}")

    if message.attachments:
        attachment_links = "\n".join([a.url for a in message.attachments[:5]])
        embed.add_field(name="Attachments", value=attachment_links, inline=False)

    original_embeds = message.embeds[:3] if message.embeds else []

    sent_count = 0
    for channel_name in BROADCAST_CHANNELS:
        channel = discord.utils.get(guild.text_channels, name=channel_name)
        if channel and channel.id != message.channel.id:
            try:
                await channel.send(embed=embed)
                for orig_embed in original_embeds:
                    await channel.send(embed=orig_embed)
                sent_count += 1
            except Exception as e:
                log.warning("Error broadcasting to %s: %s", channel_name, e)

    try:
        await message.add_reaction("📡")
    except Exception:
        pass

    log.info("Broadcasted announcement to %d channels", sent_count)

# -------------------------------------------------
# 8) BOT STATE MANAGER
# -------------------------------------------------
@dataclass
class BotState:
    winners: Dict[str, dict] = field(default_factory=dict)
    strikes: Dict[str, int] = field(default_factory=dict)
    last_hackathons: List[dict] = field(default_factory=list)
    recent_joins: Dict[int, deque] = field(default_factory=lambda: defaultdict(
        lambda: deque(maxlen=RAID_JOIN_THRESHOLD * 3)
    ))
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def load_winners(self) -> None:
        if not os.path.exists(WINNERS_FILE):
            return
        try:
            async with aiofiles.open(WINNERS_FILE, "r", encoding="utf-8") as f:
                self.winners = json.loads(await f.read())
            log.info("Loaded %d winners from %s", len(self.winners), WINNERS_FILE)
        except Exception as e:
            log.warning("Could not load winners: %s", e)
            self.winners = {}

    async def save_winners(self) -> None:
        async with self._lock:
            snapshot = dict(self.winners)
        try:
            async with aiofiles.open(WINNERS_FILE, "w", encoding="utf-8") as f:
                await f.write(json.dumps(snapshot, indent=2, ensure_ascii=False))
            log.info("Saved %d winners to %s", len(snapshot), WINNERS_FILE)
        except Exception as e:
            log.warning("Could not save winners: %s", e)

    async def set_winner(self, hackathon: str, data: dict) -> None:
        async with self._lock:
            self.winners[hackathon] = data
        await self.save_winners()

    def get_winner(self, hackathon: str) -> dict | None:
        return self.winners.get(hackathon)

    def get_valid_winners(self) -> List[dict]:
        return [
            v for v in self.winners.values()
            if v.get("hackathon", "").strip().lower() not in ("winner", "winners")
        ]

    async def load_strikes(self) -> None:
        if not os.path.exists(STRIKES_FILE):
            return
        try:
            async with aiofiles.open(STRIKES_FILE, "r", encoding="utf-8") as f:
                self.strikes = json.loads(await f.read())
            log.info("Loaded %d strikes from %s", len(self.strikes), STRIKES_FILE)
        except Exception as e:
            log.warning("Could not load strikes: %s", e)
            self.strikes = {}

    async def save_strikes(self) -> None:
        async with self._lock:
            snapshot = dict(self.strikes)
        try:
            async with aiofiles.open(STRIKES_FILE, "w", encoding="utf-8") as f:
                await f.write(json.dumps(snapshot, indent=2, ensure_ascii=False))
            log.info("Saved %d strikes to %s", len(snapshot), STRIKES_FILE)
        except Exception as e:
            log.warning("Could not save strikes: %s", e)

    async def add_strike(self, guild_id: int, user_id: int, reason: str) -> int:
        key = f"{guild_id}:{user_id}"
        async with self._lock:
            self.strikes[key] = self.strikes.get(key, 0) + 1
            total = self.strikes[key]
        log.info("Strike added: user %d in guild %d (total %d) — reason: %s",
                 user_id, guild_id, total, reason)
        await self.save_strikes()
        return total

    def get_strikes(self, guild_id: int, user_id: int) -> int:
        return self.strikes.get(f"{guild_id}:{user_id}", 0)

    def update_hackathons(self, hackathons: List[dict]) -> None:
        self.last_hackathons = hackathons[:100]

    def record_join(self, guild_id: int) -> int:
        now = time.time()
        dq = self.recent_joins[guild_id]
        dq.append(now)
        while dq and now - dq[0] > RAID_JOIN_WINDOW_SECONDS:
            dq.popleft()
        return len(dq)


state = BotState()

# -------------------------------------------------
# 9) HTTP CLIENT MANAGER
# -------------------------------------------------
class HTTPClientManager:
    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def get_client(self) -> httpx.AsyncClient:
        async with self._lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    timeout=httpx.Timeout(10.0, connect=5.0),
                    limits=httpx.Limits(max_connections=20),
                    follow_redirects=True,
                )
        return self._client

    async def close(self) -> None:
        async with self._lock:
            if self._client and not self._client.is_closed:
                await self._client.aclose()
                self._client = None


http_manager = HTTPClientManager()

# -------------------------------------------------
# 10) HACKATHONS FETCH + FILTERS
# -------------------------------------------------
async def fetch_hackathons() -> List[dict]:
    client = await http_manager.get_client()

    base = (HACKATHONS_API_BASE or "").strip()
    if base:
        url = base.rstrip("/") + "/hackathons/upcoming"
        try:
            r = await client.get(url, params={"days": 365, "limit": 300})
            r.raise_for_status()
            data = r.json()

            if isinstance(data, dict) and "events" in data:
                events = data["events"]
            elif isinstance(data, list):
                events = data
            else:
                log.warning("Insights API: unexpected JSON shape: %s", type(data))
                events = []

            if isinstance(events, list) and events:
                log.info("Fetched %d hackathons from Insights API", len(events))
                return events
        except Exception as e:
            log.warning("Could not fetch hackathons from Insights API: %s", e)

    try:
        r = await client.get(HACKATHONS_JSON_URL)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            log.info("Fetched %d hackathons from GitHub JSON fallback", len(data))
            return data
        log.warning("Hackathons JSON fallback is not a list, got: %s", type(data))
    except Exception as e:
        log.warning("Could not fetch hackathons fallback JSON: %s", e)

    return []


def filter_online_events(events: List[dict]) -> List[dict]:
    return [e for e in events if is_online_event(e)]


def filter_events_with_dates(events: List[dict]) -> List[dict]:
    return [e for e in events if has_valid_date(e)]


def sort_events_by_date(events: List[dict]) -> List[Tuple[dict, datetime | None]]:
    events_with_dates: List[Tuple[dict, datetime | None]] = []
    for e in events:
        dt = parse_iso_date(e.get("start_date") or "")
        events_with_dates.append((e, dt))

    events_with_dates.sort(
        key=lambda pair: (
            0 if pair[1] is not None else 1,
            pair[1].timestamp() if pair[1] is not None else float("inf"),
        )
    )
    return events_with_dates


def filter_events_for_question(
    events: List[dict],
    question: str,
) -> Tuple[List[Tuple[dict, datetime | None]], str]:
    lower_q = question.lower()
    window_days, window_label = infer_time_window(lower_q)

    only_hackeroos = "hackeroos" in lower_q
    online_only = (
        "online only" in lower_q
        or "only online" in lower_q
        or (
            "online" in lower_q
            and "in person" not in lower_q
            and "in-person" not in lower_q
            and "offline" not in lower_q
        )
        or "remote" in lower_q
        or "virtual" in lower_q
    )

    now = datetime.now(timezone.utc)
    filtered: List[Tuple[dict, datetime | None]] = []

    for e in events:
        source = (e.get("source") or "").strip().lower()
        if only_hackeroos and source != "hackeroos":
            continue
        if online_only and not is_online_event(e):
            continue

        dt = parse_iso_date(e.get("start_date"))
        if window_days is not None:
            if dt is None:
                continue
            end = now + timedelta(days=window_days)
            if not (now <= dt <= end):
                continue

        filtered.append((e, dt))

    filtered.sort(
        key=lambda pair: (
            0 if pair[1] is not None else 1,
            pair[1].timestamp() if pair[1] is not None else float("inf"),
            (pair[0].get("title") or "").lower(),
        )
    )

    return filtered, window_label

# -------------------------------------------------
# 11) PIN/UNPIN HELPER (Bot-only)
# -------------------------------------------------
async def pin_and_unpin(message: discord.Message, bot_user: discord.User) -> None:
    channel = message.channel
    try:
        pinned = await channel.pins()
        for p in pinned:
            if p.author.id == bot_user.id:
                try:
                    await p.unpin()
                except Exception:
                    pass
        await message.pin(reason="New Hackathon Announcement")
    except Exception as e:
        log.warning("Could not pin/unpin: %s", e)

# -------------------------------------------------
# 12) MOD LOG HELPERS
# -------------------------------------------------
async def get_mod_log_channel(guild: discord.Guild) -> discord.TextChannel | None:
    if guild is None:
        return None
    return discord.utils.get(guild.text_channels, name=MOD_LOG_CHANNEL_NAME)


async def send_mod_log(
    guild: discord.Guild,
    title: str,
    description: str = "",
    *,
    user: discord.abc.User | None = None,
    channel: discord.abc.GuildChannel | None = None,
    extra: dict | None = None,
) -> None:
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
            embed.add_field(name=k, value=str(v)[:1024], inline=False)

    embed.set_footer(text="Pika-Bot • Moderation Log")

    try:
        await chan.send(embed=embed)
    except discord.Forbidden:
        pass
    except Exception as e:
        log.warning("Could not send mod log: %s", e)

# -------------------------------------------------
# 13) RAID DETECTION (safe bot member lookup)
# -------------------------------------------------
def get_bot_member(guild: discord.Guild, bot_user: discord.User | None) -> discord.Member | None:
    me = guild.me
    if me is not None:
        return me
    if bot_user is not None:
        return guild.get_member(bot_user.id)
    return None


async def handle_possible_raid(member: discord.Member, bot_user: discord.User | None) -> None:
    guild = member.guild
    join_count = state.record_join(guild.id)

    if join_count >= RAID_JOIN_THRESHOLD:
        log.warning("Potential raid in %s (%d joins in %ds)",
                    guild.name, join_count, RAID_JOIN_WINDOW_SECONDS)

        await send_mod_log(
            guild,
            "Potential Raid Detected",
            f"{join_count} new accounts joined within {RAID_JOIN_WINDOW_SECONDS} seconds.",
            user=member,
            extra={"Action": "Slowmode + kick very new accounts"},
        )

        me = get_bot_member(guild, bot_user)
        if me:
            for ch in guild.text_channels:
                try:
                    perms = ch.permissions_for(me)
                    if (perms.manage_channels
                            and ch.name != MOD_LOG_CHANNEL_NAME
                            and ch.slowmode_delay != RAID_SLOWMODE_DELAY):
                        await ch.edit(slowmode_delay=RAID_SLOWMODE_DELAY)
                except discord.Forbidden:
                    continue
                except Exception as e:
                    log.warning("Could not set slowmode on %s: %s", ch.name, e)

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

# -------------------------------------------------
# 14) DISCORD BOT SETUP
# -------------------------------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

_bg_lock = asyncio.Lock()
_background_tasks: List[asyncio.Task] = []

# -------------------------------------------------
# 15) SHUTDOWN HANDLER
# -------------------------------------------------
async def shutdown() -> None:
    log.info("Shutting down… cancelling tasks + closing HTTP client")

    for task in _background_tasks:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    await http_manager.close()
    log.info("Cleanup complete.")

# -------------------------------------------------
# 16) BACKGROUND LOOPS
# -------------------------------------------------
async def auto_alerts_loop() -> None:
    await bot.wait_until_ready()
    log.info("Auto-alerts loop started (every %d hours)", AUTO_ALERT_INTERVAL_HOURS)

    failure_count = 0

    while not bot.is_closed():
        try:
            events = await fetch_hackathons()
            if not events:
                log.warning("Hackathons feed empty or unreachable")
                await asyncio.sleep(AUTO_ALERT_INTERVAL_HOURS * 60 * 60)
                continue

            online_events = filter_online_events(events)
            if not online_events:
                log.info("No online-only hackathons found this cycle.")
                await asyncio.sleep(AUTO_ALERT_INTERVAL_HOURS * 60 * 60)
                continue

            if not state.last_hackathons:
                state.update_hackathons(online_events)
                log.info("First run: cached %d online hackathons", len(online_events))
                await asyncio.sleep(AUTO_ALERT_INTERVAL_HOURS * 60 * 60)
                continue

            old_urls = {e.get("url") for e in state.last_hackathons if e.get("url")}
            new_events = [
                e for e in online_events
                if e.get("url") and e["url"] not in old_urls and has_valid_date(e)
            ]

            if new_events:
                log.info("New ONLINE hackathons detected: %d", len(new_events))

                for guild in bot.guilds:
                    channel = discord.utils.get(guild.text_channels, name=HACKATHON_CHANNEL_NAME)
                    if not channel:
                        continue

                    embed = discord.Embed(
                        title="New Online Global Hackathons 🌍",
                        description=(
                            f"{len(new_events)} new **online** global event(s) just dropped!\n\n"
                            "These are *not* Hackeroos-run events.\n"
                            "For official Hackeroos things, check #announcements. 🦘"
                        ),
                        color=0x00ff88,
                        timestamp=datetime.now(timezone.utc),
                    )

                    for e in new_events[:MAX_EVENTS_IN_EMBED]:
                        title = (e.get("title") or "Untitled")[:80]
                        source = e.get("source", "Unknown")
                        loc = e.get("location") or "Online"
                        dt = parse_iso_date(e.get("start_date") or "")
                        start = dt.strftime("%Y-%m-%d") if dt else "Date coming soon"
                        url = e.get("url", "#")
                        embed.add_field(
                            name=f"{source} · {title}",
                            value=f"{loc} • {start} • [Register]({url})",
                            inline=False,
                        )

                    embed.set_footer(text="Pika-Bot • Auto-updated (online-only) from Insights/GitHub")
                    await channel.send(embed=embed)
            else:
                log.info("No new online hackathons this cycle")

            state.update_hackathons(online_events)
            failure_count = 0

        except asyncio.CancelledError:
            log.info("auto_alerts_loop cancelled gracefully")
            raise
        except httpx.RequestError as e:
            failure_count += 1
            log.warning("Network error in auto_alerts_loop (%d/%d): %s",
                        failure_count, MAX_CONSECUTIVE_FAILURES, e)
            if failure_count >= MAX_CONSECUTIVE_FAILURES:
                log.error("Too many consecutive failures, pausing loop")
                await asyncio.sleep(RAID_BACKOFF_SECONDS)
                failure_count = 0
        except Exception as e:
            log.exception("auto_alerts_loop crashed: %s", e)

        await asyncio.sleep(AUTO_ALERT_INTERVAL_HOURS * 60 * 60)

# -------------------------------------------------
# 17) LIFECYCLE EVENTS
# -------------------------------------------------
@bot.event
async def on_ready():
    await state.load_winners()
    await state.load_strikes()

    async with _bg_lock:
        if not getattr(bot, "_bg_tasks_started", False):
            bot._bg_tasks_started = True
            task1 = asyncio.create_task(auto_alerts_loop())
            _background_tasks.extend([task1])
            log.info("✅ Background loops started once.")
        else:
            log.info("ℹ️ on_ready fired again — background loops already running.")

    try:
        await bot.tree.sync()
        log.info("Slash commands synced globally")
    except Exception as e:
        log.warning("Error syncing slash commands: %s", e)

    log.info("Pika-Bot online | Guilds: %d | Hackathons cached: %d",
             len(bot.guilds), len(state.last_hackathons))

    await bot.change_presence(
        activity=discord.Game(name="Helping Hackeroos innovate ⚡🦘"),
        status=discord.Status.online,
    )

    # Removed “startup announcement” to avoid deploy spam.
    # If needed, we can persist it to a JSON file per guild.


@bot.event
async def on_member_join(member: discord.Member):
    await handle_possible_raid(member, bot.user)

    try:
        await member.send(
            f"Welcome to Hackeroos, {member.name}! 🦘💛\n"
            f"I'm **Pika-Bot**. Use `/pika-help` in the server to see what I can do.\n"
            f"To unlock channels, run `/verify` in #{WELCOME_CHANNEL_NAME}."
        )
    except discord.Forbidden:
        log.warning("Could not DM %s", member.name)

    channel = discord.utils.get(member.guild.text_channels, name=WELCOME_CHANNEL_NAME)
    if channel:
        await channel.send(
            f"⚡ G'day {member.mention}! Welcome to **{member.guild.name}** — run `/verify` to get access!"
        )

    await send_mod_log(
        member.guild,
        "Member Joined",
        user=member,
        extra={"Account created": member.created_at.strftime("%Y-%m-%d %H:%M UTC")},
    )


@bot.event
async def on_member_remove(member: discord.Member):
    await send_mod_log(member.guild, "Member Left", user=member)


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.abc.User):
    await send_mod_log(guild, "Member Banned", user=user)


@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.abc.User):
    await send_mod_log(guild, "Member Unbanned", user=user)


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
        extra={"Content": (message.content or "(no content / embed only)")[:1024]},
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
            "Before": (before.content or "(empty)")[:512],
            "After": (after.content or "(empty)")[:512],
        },
    )


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    if not isinstance(message.channel, discord.TextChannel):
        await bot.process_commands(message)
        return

    # Mirror any #announcements post as an embed into broadcast channels
    await broadcast_announcement(message)

    guild = message.guild
    author = message.author
    is_admin = author.guild_permissions.administrator or author.guild_permissions.manage_guild

    if contains_blocked_word(message.content or ""):
        try:
            await message.delete()
        except discord.Forbidden:
            pass

        await message.channel.send(f"{author.mention}, let's keep it clean, mate! 🧹")
        await send_mod_log(
            guild,
            "Message Deleted (Bad Word Filter)",
            user=author,
            channel=message.channel,
            extra={"Content": (message.content or "")[:512]},
        )
        return

    if not is_admin:
        mention_count = len(message.mentions)
        if message.mention_everyone:
            mention_count += 5
        if message.role_mentions:
            mention_count += len(message.role_mentions) * 2

        if mention_count >= MENTION_SPAM_THRESHOLD:
            try:
                await message.delete()
            except discord.Forbidden:
                pass

            strikes = await state.add_strike(guild.id, author.id, reason=f"Mention spam ({mention_count} mentions)")
            await send_mod_log(
                guild,
                "Mention Spam Detected",
                user=author,
                channel=message.channel,
                extra={
                    "Mentions": mention_count,
                    "Message": (message.content or "")[:512],
                    "Strikes (after)": strikes,
                },
            )

            if strikes >= AUTO_BAN_STRIKE_THRESHOLD:
                try:
                    await guild.ban(author, reason="Auto-ban: 3 strikes (mention spam)")
                    await send_mod_log(guild, "Auto-ban (3 Strikes)", user=author,
                                       extra={"Reason": "Mention spam / 3 strikes"})
                except discord.Forbidden:
                    log.warning("Could not auto-ban %s", author)
            else:
                try:
                    await message.channel.send(
                        f"{author.mention}, please don't spam mentions. "
                        f"You now have **{strikes} strike(s)** (auto-ban at 3)."
                    )
                except discord.Forbidden:
                    pass
            return

    if not is_admin and message.content:
        emoji_count = len(EMOJI_PATTERN.findall(message.content))
        if emoji_count >= EMOJI_SPAM_THRESHOLD:
            try:
                await message.delete()
            except discord.Forbidden:
                pass

            strikes = await state.add_strike(guild.id, author.id, reason=f"Emoji spam ({emoji_count} emojis)")
            await send_mod_log(
                guild,
                "Emoji Spam Detected",
                user=author,
                channel=message.channel,
                extra={
                    "Emoji count": emoji_count,
                    "Message": (message.content or "")[:512],
                    "Strikes (after)": strikes,
                },
            )

            if strikes >= AUTO_BAN_STRIKE_THRESHOLD:
                try:
                    await guild.ban(author, reason="Auto-ban: 3 strikes (emoji spam)")
                    await send_mod_log(guild, "Auto-ban (3 Strikes)", user=author,
                                       extra={"Reason": "Emoji spam / 3 strikes"})
                except discord.Forbidden:
                    log.warning("Could not auto-ban %s", author)
            else:
                try:
                    await message.channel.send(
                        f"{author.mention}, please don't spam emojis. "
                        f"You now have **{strikes} strike(s)** (auto-ban at 3)."
                    )
                except discord.Forbidden:
                    pass
            return

    lowered = (message.content or "").lower()
    if (
        message.channel.name == ANNOUNCEMENTS_CHANNEL_NAME
        and message.author.guild_permissions.administrator
        and "winner" in lowered
    ):
        await handle_winner_announcement(message)
        return

    await bot.process_commands(message)


async def handle_winner_announcement(message: discord.Message) -> None:
    hackathon = team = project = prize = None
    match = WINNER_PATTERN.search(message.content)

    if match:
        raw_hackathon = (match.group("hackathon") or "")
        cleaned_hackathon = strip_emojis(raw_hackathon)
        cleaned_hackathon = cleaned_hackathon.replace("-", "").replace("–", "").replace(":", "").strip()

        if cleaned_hackathon and len(cleaned_hackathon) >= 3:
            hackathon = cleaned_hackathon
            team = (match.group("team") or "").strip() or "—"
            project = (match.group("project") or "").strip() or "—"
            prize = (match.group("prize") or "").strip() or "—"
        else:
            match = None

    if not match or not hackathon:
        lines = [ln for ln in message.content.splitlines() if ln.strip()]
        if lines:
            first_clean = strip_emojis(lines[0])
            first_clean = re.sub(r"(?i)\b(winner|winners)\b", "", first_clean)
            first_clean = first_clean.replace("-", "").replace("–", "").replace(":", "").strip()

            if not first_clean and len(lines) >= 2:
                candidate = strip_emojis(lines[1]).strip()
                if candidate:
                    hackathon = candidate
                    team = project = prize = "—"
            elif first_clean and len(first_clean) >= 3:
                hackathon = first_clean
                team = project = prize = "—"

    if hackathon:
        existing = state.get_winner(hackathon) or {}
        await state.set_winner(hackathon, {
            "hackathon": hackathon,
            "team": team or existing.get("team", "—"),
            "project": project or existing.get("project", "—"),
            "prize": prize or existing.get("prize", "—"),
            "source": "announcement",
            "announcement_text": message.content,
        })

        try:
            await message.add_reaction("🏆")
        except discord.Forbidden:
            pass

        await message.channel.send(
            f"🏆 Winner saved for **{hackathon}** (via announcement).",
            reference=message,
        )

# -------------------------------------------------
# 18) SLASH COMMANDS
# -------------------------------------------------
@bot.tree.command(name="pika-help", description="Show all Pika-Bot slash commands 🦘")
async def pika_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Pika-Bot — Hackeroos Helper",
        description="Slash commands currently available:",
        color=0xffc300
    )
    for cmd in bot.tree.get_commands():
        embed.add_field(name=f"/{cmd.name}", value=(cmd.description or "No description"), inline=False)
    embed.set_footer(text="Built by Pika-Bots (AIHE Group 19)")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="hello", description="Say g'day to Pika-Bot")
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"G'day {interaction.user.mention}! Pika-Bot here — ready to hack and hop! ⚡🦘"
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
            "• Welcome members (DM + public)\n"
            "• Verify users with `/verify`\n"
            "• Create polls with `/poll`\n"
            "• Show online global hackathons with `/hackathons`\n"
            "• AI Q&A with `/ask`\n"
            "• Track Hackeroos winners with `/set-winner` + `/winners`\n"
        ),
        inline=False
    )
    embed.add_field(
        name="Follow Hackeroos",
        value="X: https://x.com/hackeroos_au\nWeb: https://www.hackeroos.com.au/",
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
            f"⚠️ I couldn't find a role called `{ROLE_VERIFY}`. Ask an admin to create it.",
            ephemeral=True
        )
        return

    member = interaction.user
    if isinstance(member, discord.Member) and role in member.roles:
        await interaction.response.send_message("✅ You're already verified!", ephemeral=True)
        return

    try:
        if isinstance(member, discord.Member):
            await member.add_roles(role, reason="Self-verify via /verify")
        await interaction.response.send_message("✅ You've been verified. Welcome in! 🦘", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(
            "⚠️ I don't have permission to give you that role. Tell an admin to move my role higher.",
            ephemeral=True
        )


@bot.tree.command(name="poll", description="Create a yes/no poll")
async def poll(interaction: discord.Interaction, question: str):
    if not interaction.channel or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("⚠️ Run this command in a server text channel.", ephemeral=True)
        return

    question = sanitize_input(question, max_length=500)
    if not question:
        await interaction.response.send_message("⚠️ Please provide a question for the poll.", ephemeral=True)
        return

    embed = discord.Embed(title="Hackeroos Poll", description=question, color=0xffc300)
    msg = await interaction.channel.send(embed=embed)
    await msg.add_reaction("👍")
    await msg.add_reaction("👎")
    await interaction.response.send_message("Poll created ✅", ephemeral=True)


def create_fallback_hackathons_embed(title: str, description: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=f"{description}\n\nYou can still browse manually here:",
        color=0xffc300
    )
    embed.add_field(name="Devpost", value="[devpost.com/hackathons](https://devpost.com/hackathons)", inline=False)
    embed.add_field(name="MLH", value="[mlh.io/events](https://mlh.io/events)", inline=False)
    embed.add_field(name="Lu.ma", value="[lu.ma/tag/hackathon](https://lu.ma/tag/hackathon)", inline=False)
    embed.add_field(name="Hack Club", value="[events.hackclub.com](https://events.hackclub.com/)", inline=False)
    embed.add_field(
        name="Hackeroos What's On",
        value="[hackeroos.com.au/#whats-on](https://www.hackeroos.com.au/#whats-on)",
        inline=False
    )
    embed.set_footer(text="Pika-Bot • /hackathons uses a feed built from these sites.")
    return embed


@bot.tree.command(name="hackathons", description="Show upcoming ONLINE global hackathons 🌍")
async def hackathons_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    events = await fetch_hackathons()
    if not events:
        await interaction.followup.send(embed=create_fallback_hackathons_embed(
            "No Live Hackathons Found (Right Now)",
            "I couldn't read any upcoming hackathons from the feed."
        ))
        return

    online_events = filter_online_events(events)
    if not online_events:
        await interaction.followup.send(embed=create_fallback_hackathons_embed(
            "No Online Hackathons Found (Right Now)",
            "I couldn't find online-only hackathons in the merged feed."
        ))
        return

    cleaned_events = filter_events_with_dates(online_events)
    if not cleaned_events:
        embed = discord.Embed(
            title="Online Hackathons (Dates Coming Soon)",
            description=(
                "The feed has online events but without clear dates.\n"
                "Please check Devpost / MLH / Lu.ma / Hack Club / Hackeroos directly."
            ),
            color=0xffc300
        )
        await interaction.followup.send(embed=embed)
        return

    sorted_events = sort_events_by_date(cleaned_events)
    top_events = [e for (e, _) in sorted_events[:MAX_EVENTS_IN_HACKATHONS_CMD]]

    embed = discord.Embed(
        title="Live Online Global Hackathons",
        description=(
            f"Here are the next ~{MAX_EVENTS_IN_HACKATHONS_CMD} upcoming **online** hackathons "
            "from the merged feed.\n"
            "Sources include Devpost, MLH, Lu.ma, Hack Club, and Hackeroos."
        ),
        color=0x00bcd4,
        timestamp=datetime.now(timezone.utc),
    )

    for e in top_events:
        title = (e.get("title") or "Untitled")[:100]
        source = e.get("source", "Unknown")
        location = e.get("location") or "Online"
        dt = parse_iso_date(e.get("start_date") or "")
        start = dt.strftime("%Y-%m-%d") if dt else "Date coming soon"
        url = e.get("url", "#")

        label = f"[{source}]"
        if (source or "").strip().lower() == "hackeroos":
            label = "🦘 Hackeroos"

        embed.add_field(
            name=title,
            value=f"{label} • {location} • {start} • [Details]({url})",
            inline=False
        )

    embed.add_field(
        name="Prefer browsing manually?",
        value=(
            "• Devpost – https://devpost.com/hackathons\n"
            "• MLH – https://mlh.io/events\n"
            "• Lu.ma – https://lu.ma/tag/hackathon\n"
            "• Hack Club – https://events.hackclub.com/\n"
            "• Hackeroos – https://www.hackeroos.com.au/#whats-on"
        ),
        inline=False
    )
    embed.set_footer(text="Pika-Bot • Online-only feed from GitHub Actions + Insights API.")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="faq", description="Common questions about Hackeroos / Pika-Bot")
async def faq(interaction: discord.Interaction):
    embed = discord.Embed(title="Hackeroos FAQ", description="Quick answers for new members:", color=0x3b82f6)
    embed.add_field(name="1. I just joined, what now?",
                    value=f"Go to **#{WELCOME_CHANNEL_NAME}** and run `/verify` to unlock channels.",
                    inline=False)
    embed.add_field(name="2. How do I see global hackathons?", value="Use `/hackathons` (online-only).", inline=False)
    embed.add_field(name="3. Can I ask AI-style questions?", value="Yes, use `/ask <your question>`.", inline=False)
    embed.add_field(name="4. How do I see past winners?", value="Use `/winners`.", inline=False)
    embed.add_field(name="5. Who built this?", value="Pika-Bots — AIHE Group 19.", inline=False)
    embed.add_field(name="6. Where can I follow Hackeroos?",
                    value="X: https://x.com/hackeroos_au\nWeb: https://www.hackeroos.com.au/",
                    inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="status", description="Bot health check")
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"✅ Online | Servers: {len(bot.guilds)} | Winners stored: {len(state.winners)}",
        ephemeral=True
    )


@bot.tree.command(name="ask", description="Ask Pika-Bot in natural language 🤖")
async def ask(interaction: discord.Interaction, question: str):
    await interaction.response.defer(ephemeral=True)

    question = (question or "").strip()
    if not question:
        await interaction.followup.send("⚠️ Please provide a question.", ephemeral=True)
        return

    if await handle_winner_question(interaction, question):
        return
    if await handle_event_question(interaction, question):
        return
    await handle_llm_question(interaction, question)


async def handle_winner_question(interaction: discord.Interaction, question: str) -> bool:
    lower_q = question.lower()
    winner_keywords = ["winner", "winners", "who won", "who is the winner", "who are the winners"]

    if not any(k in lower_q for k in winner_keywords):
        return False
    if not state.winners:
        return False

    matched = None
    for name, data in state.winners.items():
        name_lower = name.lower()
        if name_lower in lower_q or lower_q in name_lower:
            matched = data
            break

    if matched:
        hackathon_name = matched.get("hackathon", "Unknown hackathon")
        source = matched.get("source")
        if source == "announcement" and matched.get("announcement_text"):
            msg = (
                f"🏆 Here's the saved winner announcement for **{hackathon_name}**:\n\n"
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
        return True

    valid_entries = state.get_valid_winners()
    if not valid_entries:
        await interaction.followup.send("🏆 I don't have any valid winners saved yet.", ephemeral=True)
        return True

    entries = valid_entries[-RECENT_WINNERS_DISPLAY_COUNT:]
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
    return True


async def handle_event_question(interaction: discord.Interaction, question: str) -> bool:
    lower_q = question.lower()

    event_keywords = ["hackathon", "hackathons", "event", "events", "competition", "game jam", "buildathon", "challenge"]
    if not any(k in lower_q for k in event_keywords):
        return False

    intent_keywords = [
        "show", "list", "find", "search", "browse", "recommend",
        "upcoming", "next", "soon", "today", "tomorrow",
        "this week", "next week", "this weekend", "next weekend",
        "online", "remote", "virtual",
    ]

    if not any(k in lower_q for k in intent_keywords):
        return False

    events = await fetch_hackathons()
    if not events:
        await interaction.followup.send(embed=create_fallback_hackathons_embed(
            "No Live Hackathons Found (Right Now)",
            "I tried to look up current hackathons from the feed and got nothing."
        ), ephemeral=True)
        return True

    filtered, window_label = filter_events_for_question(events, question)
    if not filtered:
        lines = [
            f"🤔 I couldn't find hackathons that strictly match **{window_label}** for that query.",
            "",
            "Here are some upcoming ones anyway:\n",
        ]
        for e in events[:8]:
            title = e.get("title", "Untitled")
            url = e.get("url", "#")
            source = e.get("source", "Unknown")
            label = "Hackeroos 🦘" if (source or "").strip().lower() == "hackeroos" else source
            lines.append(f"• **{title}** — ({label}) → {url}")

        lines.append("\nYou can also run `/hackathons` for an embed version.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        return True

    lines = [f"🌍 Here are hackathons I found for **{window_label}**:\n"]
    for i, (e, dt) in enumerate(filtered[:MAX_EVENTS_IN_EMBED], start=1):
        title = e.get("title", "Untitled")
        url = e.get("url", "#")
        source = e.get("source", "Unknown")
        location = e.get("location") or "Location TBA / Online"
        label = "Hackeroos 🦘" if (source or "").strip().lower() == "hackeroos" else source
        date_str = dt.strftime("%Y-%m-%d") if dt else "Date coming soon"
        lines.append(f"{i}. **{title}** — ({label}) • {location} • {date_str} → {url}")

    await interaction.followup.send("\n".join(lines), ephemeral=True)
    return True


async def handle_llm_question(interaction: discord.Interaction, question: str) -> None:
    if not HF_TOKEN:
        await interaction.followup.send(
            "⚠️ No Hugging Face token configured.\n"
            "Ask an admin to set `HF_TOKEN` (or `HUGGINGFACE_TOKEN`) in `.env`.",
            ephemeral=True
        )
        return

    try:
        client = OpenAI(base_url="https://router.huggingface.co/v1", api_key=HF_TOKEN)
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
            f"⚠️ I couldn't talk to Hugging Face Inference Providers:\n```{e}```",
            ephemeral=True
        )


@bot.tree.command(name="set-winner", description="Set the winner for a hackathon (admin only) 🏆")
async def set_winner(interaction: discord.Interaction, hackathon: str, team: str, project: str = "", prize: str = ""):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("⚠️ Only admins can use `/set-winner`.", ephemeral=True)
        return

    hackathon = sanitize_input(hackathon, MAX_HACKATHON_NAME_LENGTH)
    team = sanitize_input(team, MAX_TEAM_NAME_LENGTH)
    project = sanitize_input(project, MAX_PROJECT_LENGTH) or "—"
    prize = sanitize_input(prize, MAX_PRIZE_LENGTH) or "—"

    if not hackathon or not team:
        await interaction.response.send_message("⚠️ Hackathon name and team are required.", ephemeral=True)
        return

    await state.set_winner(hackathon, {
        "hackathon": hackathon,
        "team": team,
        "project": project,
        "prize": prize,
        "source": "manual",
    })

    await interaction.response.send_message(
        f"🏆 Winner saved for **{hackathon}**:\n"
        f"• Team: **{team}**\n"
        f"{'• Project: ' + project if project != '—' else ''}\n"
        f"{'• Prize: ' + prize if prize != '—' else ''}",
        ephemeral=True
    )


@bot.tree.command(name="winners", description="Show recent Hackeroos hackathon winners 🏆")
async def winners_cmd(interaction: discord.Interaction):
    valid_entries = state.get_valid_winners()
    if not valid_entries:
        await interaction.response.send_message("🏆 No winners saved yet.", ephemeral=True)
        return

    entries = valid_entries[-RECENT_WINNERS_DISPLAY_COUNT:]
    embed = discord.Embed(title="Hackeroos Hackathon Winners", color=0xfbbf24)

    for item in reversed(entries):
        hackathon_name = item.get("hackathon", "Unknown")
        source = item.get("source")

        if source == "announcement" and item.get("announcement_text"):
            embed.add_field(name=f"🏁 {hackathon_name}", value=item["announcement_text"][:1024], inline=False)
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


@bot.tree.command(name="update-hackathons", description="Manually refresh hackathons feed (admin only)")
async def update_hackathons(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("⚠️ Only admins can update hackathons.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    events = await fetch_hackathons()
    if not events:
        await interaction.followup.send("⚠️ Could not fetch any hackathons from API or fallback.", ephemeral=True)
        return

    os.makedirs("data", exist_ok=True)
    try:
        async with aiofiles.open("data/hackathons.json", "w", encoding="utf-8") as f:
            await f.write(json.dumps(events, indent=2, ensure_ascii=False))
        await interaction.followup.send(
            f"✅ Hackathons updated successfully.\nTotal events saved: **{len(events)}**.",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"⚠️ Failed to save hackathons.json:\n```{e}```", ephemeral=True)

# -------------------------------------------------
# 19) MAIN ENTRY POINT (Graceful shutdown + SIGTERM)
# -------------------------------------------------
_stop_event = asyncio.Event()


def _request_stop() -> None:
    _stop_event.set()


async def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN missing in `.env`!")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # Windows / limited environments
            pass

    try:
        async with bot:
            runner = asyncio.create_task(bot.start(TOKEN))
            stopper = asyncio.create_task(_stop_event.wait())

            done, pending = await asyncio.wait(
                {runner, stopper},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if stopper in done:
                log.info("Stop requested (SIGINT/SIGTERM). Closing bot...")
                await bot.close()

            for t in pending:
                t.cancel()
    finally:
        await shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped via keyboard interrupt")
