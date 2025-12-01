import os
import re
import logging
import json
import asyncio
import time
from collections import defaultdict, deque
from typing import Dict, List, Any
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands
from dotenv import load_dotenv

import httpx  # for Insights API + GitHub JSON

from openai import OpenAI  # HF router client

# -------------------------------------------------
# 1) ENV + CONFIG
# -------------------------------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Hugging Face token + model for /ask (via router.huggingface.co/v1)
HF_TOKEN = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN")
HF_MODEL = os.getenv(
    "HUGGINGFACE_MODEL",
    "Qwen/Qwen2.5-72B-Instruct"
)

# channel names used by the bot (must match server)
WELCOME_CHANNEL_NAME = "welcome-verify"
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

# simple thresholds – can tune later
RAID_JOIN_WINDOW_SECONDS = 30               # look at joins in this window
RAID_JOIN_THRESHOLD = 5                     # joins in that window to flag raid
NEW_ACCOUNT_MAX_AGE_SECONDS = 24 * 60 * 60  # treat <24h old as “very new”

MENTION_SPAM_THRESHOLD = 6                  # count mentions in one message
EMOJI_SPAM_THRESHOLD = 15                   # count emoji in one message

# Hackathons backend
# 1) Primary: Insights API on Railway
HACKATHONS_API_BASE = os.getenv(
    "HACKATHONS_API_BASE",
    "https://hackeroos-insights-api-production.up.railway.app",
)

# 2) Fallback: GitHub JSON with merged hackathons (updated by GH Actions)
HACKATHONS_JSON_URL = os.getenv(
    "HACKATHONS_JSON_URL",
    "https://raw.githubusercontent.com/aadarsh1282/pika-bot/main/data/hackathons.json",
)

# Hackeroos reminders
HACKEROOS_REMINDER_INTERVAL_HOURS = 12

# NEW: auto alert interval (global hackathons)
AUTO_ALERT_INTERVAL_HOURS = 24

# month map for Devpost-style + MLH-style strings
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


# -------------------------------------------------
# 2) HACKATHONS FETCH HELPERS (Insights API + fallback)
# -------------------------------------------------

async def fetch_hackathons() -> List[dict]:
    """
    Fetch merged hackathons list.

    Priority:
      1) Hackeroos Insights API (/hackathons/upcoming)
      2) Fallback to GitHub JSON (data/hackathons.json)

    Expected event shape: title, url, start_date, location, source.
    """
    # 1) Try Insights API first
    base = (HACKATHONS_API_BASE or "").strip()
    if base:
        url = base.rstrip("/") + "/hackathons/upcoming"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
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

                if isinstance(events, list):
                    log.info("Fetched %d hackathons from Insights API", len(events))
                    return events
        except Exception as e:
            log.warning("Could not fetch hackathons from Insights API: %s", e)

    # 2) Fallback: GitHub JSON
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(HACKATHONS_JSON_URL)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                log.info("Fetched %d hackathons from GitHub JSON fallback", len(data))
                return data
            log.warning("Hackathons JSON fallback is not a list, got: %s", type(data))
            return []
    except Exception as e:
        log.warning("Could not fetch hackathons fallback JSON: %s", e)
        return []


# -------------------------------------------------
# 3) PIN/UNPIN HELPER
# -------------------------------------------------

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


# -------------------------------------------------
# 4) AUTO ALERTS LOOP (ALL SOURCES, ONLINE-ONLY, 24H)
# -------------------------------------------------

async def auto_alerts_loop():
    """
    Background task: every 24 hours:
      - Poll hackathons feed
      - Filter to ONLINE-ONLY events (location/mode contains online/virtual/remote/digital)
      - Announce NEW ones compared to last run
      - Skip events with no date to avoid TBA spam

    NOTE: This loop no longer pins messages.
          Only the Hackeroos reminder loop controls the pinned countdown
          (Option A: Hackeroos-only pinned reminder).
    """
    global LAST_HACKATHONS
    await bot.wait_until_ready()
    log.info("Auto-alerts loop started (every %d hours)", AUTO_ALERT_INTERVAL_HOURS)

    while not bot.is_closed():
        try:
            events = await fetch_hackathons()
            if not events:
                log.warning("Hackathons feed empty or unreachable")
                await asyncio.sleep(AUTO_ALERT_INTERVAL_HOURS * 60 * 60)
                continue

            # filter to online-only events (location or mode)
            online_events: List[dict] = []
            for e in events:
                loc_lower = (e.get("location") or "").strip().lower()
                mode_lower = (e.get("mode") or "").strip().lower()

                is_online_location = any(
                    kw in loc_lower
                    for kw in ("online", "virtual", "remote", "digital")
                )
                is_online_mode = any(
                    kw in mode_lower
                    for kw in ("online", "digital", "remote", "virtual")
                )

                if is_online_location or is_online_mode:
                    online_events.append(e)

            if not online_events:
                log.info("No online-only hackathons found this cycle.")
                await asyncio.sleep(AUTO_ALERT_INTERVAL_HOURS * 60 * 60)
                continue

            events = online_events

            # first run – just cache
            if not LAST_HACKATHONS:
                LAST_HACKATHONS = events
                log.info("First run: cached %d online hackathons", len(events))
                await asyncio.sleep(AUTO_ALERT_INTERVAL_HOURS * 60 * 60)
                continue

            # detect new events by URL
            old_urls = {e.get("url") for e in LAST_HACKATHONS if e.get("url")}
            new_events = [
                e for e in events
                if e.get("url") and e["url"] not in old_urls
            ]

            # skip events that don't have any date at all (avoid TBA spam)
            def has_any_date(ev: dict) -> bool:
                raw = (ev.get("start_date") or "").strip()
                return bool(raw)

            new_events = [e for e in new_events if has_any_date(e)]

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
                            "For official Hackeroos things, check the pinned "
                            "countdown in #announcements. 🦘"
                        ),
                        color=0x00ff88,
                        timestamp=datetime.now(timezone.utc),
                    )
                    for e in new_events[:10]:
                        title = (e.get("title") or "Untitled")[:80]
                        source = e.get("source", "Unknown")
                        loc = e.get("location") or "Online"
                        raw_date = e.get("start_date") or ""
                        dt = parse_iso_date(raw_date)
                        if dt:
                            start = dt.strftime("%Y-%m-%d")
                        elif raw_date:
                            start = raw_date
                        else:
                            start = "Date coming soon"
                        url = e.get("url", "#")
                        embed.add_field(
                            name=f"{source} · {title}",
                            value=f"{loc} • {start} • [Register]({url})",
                            inline=False,
                        )
                    embed.set_footer(text="Pika-Bot • Auto-updated (online-only) from Insights/GitHub")

                    # IMPORTANT: no pin, no @here — just a normal message
                    await channel.send(embed=embed)
            else:
                log.info("No new online hackathons this cycle")

            LAST_HACKATHONS = events[:100]

        except Exception as e:
            log.exception("auto_alerts_loop crashed: %s", e)

        await asyncio.sleep(AUTO_ALERT_INTERVAL_HOURS * 60 * 60)


# -------------------------------------------------
# 5) DATE + MINI AGENT HELPERS
# -------------------------------------------------

def parse_iso_date(date_str: str | None) -> datetime | None:
    """
    Try to parse:
      - ISO formats: 2025-12-03, 2025-12-03T10:00:00Z, 2025-12-03T10:00:00+00:00
      - Devpost-style: "Dec 17, 2025", "Dec 01 - 21, 2025",
                       "Dec 31, 2025 - Feb 07, 2026"
      - MLH-style: "Feb 14th - 15th, 2026" or "Feb 14th - 15th"
    We always take the *first* date as "start".
    """
    if not date_str:
        return None

    s = date_str.strip()

    # 1) Strict ISO with Z
    try:
        if s.endswith("Z"):
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return dt
    except Exception:
        pass

    # 2) ISO with timezone or plain date
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            continue

    # 3) Devpost-style: pick first "MonthName DD, YYYY"
    matches = re.findall(r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})", s)
    if matches:
        month_name, day_str, year_str = matches[0]
        month = MONTH_MAP.get(month_name.lower()[:3]) or MONTH_MAP.get(month_name.lower())
        try:
            if month:
                day = int(day_str)
                year = int(year_str)
                return datetime(year, month, day, tzinfo=timezone.utc)
        except Exception:
            pass

    # 3b) MLH-style: "Feb 14th - 15th, 2026" or "Feb 14th - 15th"
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
                    # If no year, guess: use current year or next year if already passed
                    now = datetime.now(timezone.utc)
                    year = now.year
                    try_date = datetime(year, month, day, tzinfo=timezone.utc)
                    if try_date < now:
                        year += 1
                return datetime(year, month, day, tzinfo=timezone.utc)
            except Exception:
                pass

    # 4) Last resort: find "YYYY-MM-DD" anywhere
    m = re.search(r"\d{4}-\d{2}-\d{2}", s)
    if m:
        try:
            dt = datetime.strptime(m.group(0), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass

    return None


def infer_time_window(question: str) -> tuple[int | None, str]:
    """
    Very simple NLP: map phrases like 'next week', 'this weekend'
    into a (days, friendly_label).
    days=None means 'no date filter, just general upcoming'.
    """
    q = question.lower()

    if "next week" in q or "coming week" in q or "upcoming week" in q:
        return 7, "the next 7 days"

    if "this weekend" in q or "on the weekend" in q:
        return 4, "this weekend"

    if "next weekend" in q:
        return 7, "next weekend"

    if "today" in q or "tonight" in q:
        return 1, "today"

    if "tomorrow" in q:
        # today + tomorrow
        return 2, "tomorrow (and the following day)"

    if "next month" in q:
        return 31, "the next month"

    if "this month" in q:
        return 31, "this month"

    if "soon" in q or "coming up" in q or "upcoming" in q:
        return 14, "the next couple of weeks"

    # default: no strict filter
    return None, "upcoming"


def filter_events_for_question(
    events: list[dict],
    *,
    question: str,
) -> tuple[list[tuple[dict, datetime | None]], str]:
    """
    Apply mini-agent logic:
      - detect time window
      - detect Hackeroos-only
      - detect online-only
      - properly filter out events with unparseable dates when user asks
        for a specific time window
    Return (filtered_events_with_dt, label_for_answer)
    """
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
    filtered: list[tuple[dict, datetime | None]] = []

    for e in events:
        source = (e.get("source") or "").strip().lower()
        location = (e.get("location") or "").strip().lower()
        mode = (e.get("mode") or "").strip().lower()
        raw_date = e.get("start_date")

        # 1) filter by source
        if only_hackeroos and source != "hackeroos":
            continue

        # 2) filter by online-only (location or mode)
        if online_only:
            if (
                "online" not in location
                and "virtual" not in location
                and "remote" not in location
                and "digital" not in location
                and "online" not in mode
                and "virtual" not in mode
                and "remote" not in mode
                and "digital" not in mode
            ):
                continue

        # 3) date parsing + time window
        dt = parse_iso_date(raw_date)

        # If user asked for a time window and we can't parse date → skip
        if window_days is not None:
            if dt is None:
                continue
            end = now + timedelta(days=window_days)
            if not (now <= dt <= end):
                continue

        filtered.append((e, dt))

    # sort: first by date (if known) then by title
    filtered.sort(
        key=lambda pair: (
            0 if pair[1] is not None else 1,
            pair[1].timestamp() if pair[1] is not None else float("inf"),
            (pair[0].get("title") or "").lower(),
        )
    )

    return filtered, window_label


# -------------------------------------------------
# 6) HACKEROOS REMINDER LOOP (Hackeroos-only)
# -------------------------------------------------

async def hackeroos_reminder_loop():
    """
    Every 12 hours:
      - Fetch hackathons
      - Filter to source == "Hackeroos"
      - Use end_date/deadline if available to show
        'X days left before applications close'
      - Only show active/upcoming events (no 'already finished')
      - Pin a single countdown embed in announcements / all-hackathons
    """
    await bot.wait_until_ready()
    log.info("Hackeroos reminder loop started (every %d hours)", HACKEROOS_REMINDER_INTERVAL_HOURS)

    while not bot.is_closed():
        try:
            events = await fetch_hackathons()
            if not events:
                log.warning("[Hackeroos reminder] No events from feed.")
                await asyncio.sleep(HACKEROOS_REMINDER_INTERVAL_HOURS * 60 * 60)
                continue

            # Only Hackeroos-run events
            hackeroos_events = [
                e for e in events
                if (e.get("source") or "").strip().lower() == "hackeroos"
            ]

            if not hackeroos_events:
                log.info("[Hackeroos reminder] No Hackeroos events in feed this cycle.")
                await asyncio.sleep(HACKEROOS_REMINDER_INTERVAL_HOURS * 60 * 60)
                continue

            now_utc = datetime.now(timezone.utc).date()

            # Build list: (event, deadline_dt|None, start_dt|None)
            processed: list[tuple[dict, datetime | None, datetime | None]] = []
            for e in hackeroos_events:
                raw_start = e.get("start_date") or ""
                raw_deadline = (
                    e.get("end_date")
                    or e.get("deadline")
                    or e.get("apply_by")
                    or ""
                )

                dt_start = parse_iso_date(raw_start) if raw_start else None
                dt_deadline = parse_iso_date(raw_deadline) if raw_deadline else None

                processed.append((e, dt_deadline, dt_start))

            # Only show events that are upcoming / active:
            #   - if we have a deadline: keep if deadline >= today
            #   - else if we only have start_date: keep if start >= today
            #   - else: keep (dates coming soon)
            upcoming: list[tuple[dict, datetime | None, datetime | None]] = []
            for e, dt_deadline, dt_start in processed:
                if dt_deadline is not None:
                    if dt_deadline.date() < now_utc:
                        continue  # applications already closed
                    upcoming.append((e, dt_deadline, dt_start))
                elif dt_start is not None:
                    if dt_start.date() < now_utc:
                        continue  # event already started and no deadline info
                    upcoming.append((e, dt_deadline, dt_start))
                else:
                    # no dates at all → still show as "dates coming soon"
                    upcoming.append((e, dt_deadline, dt_start))

            if not upcoming:
                log.info("[Hackeroos reminder] No upcoming Hackeroos events with open applications.")
                await asyncio.sleep(HACKEROOS_REMINDER_INTERVAL_HOURS * 60 * 60)
                continue

            # Sort by deadline first, then start date, then title
            def sort_key(item: tuple[dict, datetime | None, datetime | None]):
                e, dt_deadline, dt_start = item

                if dt_deadline is not None:
                    return (0, dt_deadline.timestamp(),
                            dt_start.timestamp() if dt_start else float("inf"),
                            (e.get("title") or "").lower())
                if dt_start is not None:
                    return (1, dt_start.timestamp(),
                            float("inf"),
                            (e.get("title") or "").lower())
                return (2, float("inf"), float("inf"),
                        (e.get("title") or "").lower())

            upcoming.sort(key=sort_key)

            # Build embed
            for guild in bot.guilds:
                # Prefer announcements; fallback to all-hackathons
                channel = discord.utils.get(guild.text_channels, name=ANNOUNCEMENTS_CHANNEL_NAME)
                if not channel:
                    channel = discord.utils.get(guild.text_channels, name=HACKATHON_CHANNEL_NAME)
                if not channel:
                    continue

                embed = discord.Embed(
                    title="Hackeroos Events & Reminders 🦘",
                    description=(
                        "Here are upcoming **Hackeroos-run** events.\n"
                        "Follow updates on X: https://x.com/hackeroos_au"
                    ),
                    color=0xfbbf24,
                    timestamp=datetime.now(timezone.utc),
                )

                for e, dt_deadline, dt_start in upcoming[:5]:
                    title = e.get("title") or "Hackeroos Event"
                    url = e.get("url", "#")
                    loc = e.get("location") or "Australia / Online"

                    # Choose main date for countdown: deadline > start_date > “coming soon”
                    if dt_deadline is not None:
                        d = dt_deadline.date()
                        days_left = (d - now_utc).days
                        date_str = d.strftime("%Y-%m-%d")
                        if days_left == 0:
                            status = "Applications close **today**"
                        elif days_left == 1:
                            status = "Applications close **tomorrow**"
                        else:
                            status = f"Applications close in **{days_left} days**"
                    elif dt_start is not None:
                        d = dt_start.date()
                        days_left = (d - now_utc).days
                        date_str = d.strftime("%Y-%m-%d")
                        if days_left == 0:
                            status = "Event **starts today**"
                        elif days_left == 1:
                            status = "Event starts in **1 day**"
                        else:
                            status = f"Event starts in **{days_left} days**"
                    else:
                        date_str = "Dates coming soon"
                        status = "Keep an eye out for announcements"

                    embed.add_field(
                        name=title,
                        value=(
                            f"{loc}\n"
                            f"{date_str} — {status}\n"
                            f"[Details]({url})"
                        ),
                        inline=False,
                    )

                embed.set_footer(text="Pika-Bot • Hackeroos-first reminders from Insights/GitHub")

                msg = await channel.send(
                    content="@here Hackeroos events update 🦘",
                    embed=embed,
                )
                await pin_and_unpin(msg)

        except Exception as e:
            log.exception("hackeroos_reminder_loop crashed: %s", e)

        await asyncio.sleep(HACKEROOS_REMINDER_INTERVAL_HOURS * 60 * 60)


# -------------------------------------------------
# 7) WINNERS LOAD/SAVE
# -------------------------------------------------

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


# -------------------------------------------------
# 8) STRIKES STORAGE
# -------------------------------------------------

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
        log.info("Saved %d strikes to %s", STRIKES_FILE)
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


# -------------------------------------------------
# 9) MOD LOG HELPERS
# -------------------------------------------------

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


# -------------------------------------------------
# 10) RAID DETECTION
# -------------------------------------------------

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


# -------------------------------------------------
# 11) LIFECYCLE EVENTS
# -------------------------------------------------

@bot.event
async def on_ready():
    load_winners()
    load_strikes()

    bot.loop.create_task(auto_alerts_loop())
    bot.loop.create_task(hackeroos_reminder_loop())

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
                    "🌍 **Pika-Bot** is live! Use `/hackathons` to see online global hackathons ⚡"
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
async def on_member_unban(guild: discord.Guild, user:discord.abc.User):
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


# -------------------------------------------------
# 12) SLASH COMMANDS
# -------------------------------------------------

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
            "• Welcome members (DM + public)\n"
            "• Verify users with `/verify`\n"
            "• Create polls with `/poll`\n"
            "• Show online global hackathons with `/hackathons`\n"
            "• AI Q&A with `/ask`\n"
            "• Track Hackeroos winners with `/set-winner` + `/winners`\n"
            "• Hackeroos-first reminders every 12 hours\n"
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


@bot.tree.command(name="hackathons", description="Show upcoming ONLINE global hackathons 🌍")
async def hackathons(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    events = await fetch_hackathons()

    if not events:
        # Hard fallback – feed unreachable or empty
        embed = discord.Embed(
            title="No Live Hackathons Found (Right Now)",
            description=(
                "I couldn’t read any upcoming hackathons from the feed.\n\n"
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
            name="Lu.ma",
            value="[lu.ma/tag/hackathon](https://lu.ma/tag/hackathon)",
            inline=False
        )
        embed.add_field(
            name="Hack Club",
            value="[events.hackclub.com](https://events.hackclub.com/)",
            inline=False
        )
        embed.add_field(
            name="Hackeroos What's On",
            value="[hackeroos.com.au/#whats-on](https://www.hackeroos.com.au/#whats-on)",
            inline=False
        )
        embed.set_footer(text="Pika-Bot • /hackathons uses a feed built from these sites.")
        await interaction.followup.send(embed=embed)
        return

    # Filter to ONLINE-ONLY events (location or mode says online/digital)
    online_events: List[dict] = []
    for e in events:
        loc_lower = (e.get("location") or "").strip().lower()
        mode_lower = (e.get("mode") or "").strip().lower()

        is_online_location = any(
            kw in loc_lower
            for kw in ("online", "virtual", "remote", "digital")
        )
        is_online_mode = any(
            kw in mode_lower
            for kw in ("online", "digital", "remote", "virtual")
        )

        if is_online_location or is_online_mode:
            online_events.append(e)

    events = online_events

    if not events:
        embed = discord.Embed(
            title="No Online Hackathons Found (Right Now)",
            description=(
                "I couldn’t find online-only hackathons in the merged feed.\n\n"
                "Try browsing manually on Devpost / MLH / Lu.ma / Hack Club / Hackeroos."
            ),
            color=0xffc300
        )
        await interaction.followup.send(embed=embed)
        return

       # For display: skip events with no date at all (avoid TBA spam)
    cleaned_events: List[dict] = []
    for e in events:
        raw_date = (e.get("start_date") or "").strip()
        if not raw_date:
            continue  # no date → don't show
        cleaned_events.append(e)

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

    # 🔽 NEW: sort by parsed start date and limit to next ~20 events
    events_with_dates: List[tuple[dict, datetime | None]] = []
    for e in cleaned_events:
        dt = parse_iso_date(e.get("start_date") or "")
        events_with_dates.append((e, dt))

    # Events with valid dates first (soonest → latest), then anything unparseable at the end
    events_with_dates.sort(
        key=lambda pair: (
            0 if pair[1] is not None else 1,
            pair[1].timestamp() if pair[1] is not None else float("inf"),
        )
    )

    MAX_EVENTS = 20  
    top_events = [e for (e, _) in events_with_dates[:MAX_EVENTS]]

    embed = discord.Embed(
        title="Live Online Global Hackathons",
        description=(
            f"Here are the next ~{MAX_EVENTS} upcoming **online** hackathons from the merged feed.\n"
            "Sources include Devpost, MLH, Lu.ma, Hack Club, and Hackeroos."
        ),
        color=0x00bcd4,
        timestamp=datetime.now(timezone.utc),
    )
    for e in top_events:
        title = (e.get("title") or "Untitled")[:100]
        source = e.get("source", "Unknown")
        location = e.get("location") or "Online"
        raw_date = e.get("start_date") or ""
        dt = parse_iso_date(raw_date)
        if dt:
            start = dt.strftime("%Y-%m-%d")
        elif raw_date:
            start = raw_date
        else:
            # should not happen because we filtered earlier, but just in case
            start = "Date coming soon"
        url = e.get("url", "#")
        label = f"[{source}]"
        if (source or "").strip().lower() == "hackeroos":
            label = "🦘 Hackeroos"
        embed.add_field(
            name=title,
            value=f"{label} • {location} • {start} • [Details]({url})",
            inline=False
        )

    # Soft fallback: manual browsing links even when feed works
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
        value="Use `/hackathons` (online-only).",
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
    embed.add_field(
        name="6. Where can I follow Hackeroos?",
        value="X: https://x.com/hackeroos_au\nWeb: https://www.hackeroos.com.au/",
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

    # -------------------------------------------------
    # 1) Winner questions → answer from winners.json
    # -------------------------------------------------
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
                    "🏆 I don’t have any valid winners saved yet.",
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

    # -------------------------------------------------
    # 2) Event / hackathon questions → mini MCP-style agent
    # -------------------------------------------------
    event_keywords = ["hackathon", "event", "competition", "game jam", "buildathon", "challenge"]
    if any(k in lower_q for k in event_keywords):
        events = await fetch_hackathons()
        if not events:
            # Same fallback as /hackathons
            embed = discord.Embed(
                title="No Live Hackathons Found (Right Now)",
                description=(
                    "I tried to look up current hackathons from the feed and got nothing.\n\n"
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
                name="Lu.ma",
                value="[lu.ma/tag/hackathon](https://lu.ma/tag/hackathon)",
                inline=False
            )
            embed.add_field(
                name="Hack Club",
                value="[events.hackclub.com](https://events.hackclub.com/)",
                inline=False
            )
            embed.add_field(
                name="Hackeroos What's On",
                value="[hackeroos.com.au/#whats-on](https://www.hackeroos.com.au/#whats-on)",
                inline=False
            )
            embed.set_footer(text="Pika-Bot • /hackathons uses the same feed.")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        filtered, window_label = filter_events_for_question(events, question=question)

        if not filtered:
            # No exact matches in the time window → show a friendly fallback
            lines = [
                f"🤔 I couldn’t find hackathons that strictly match **{window_label}** for that query.",
                "",
                "Here are some upcoming ones anyway:\n",
            ]
            for e in events[:8]:
                title = e.get("title", "Untitled")
                url = e.get("url", "#")
                source = e.get("source", "Unknown")
                label = source
                if (source or "").strip().lower() == "hackeroos":
                    label = "Hackeroos 🦘"
                lines.append(f"• **{title}** — ({label}) → {url}")

            lines.append(
                "\nYou can also run `/hackathons` for an embed version, or browse manually:\n"
                "Devpost / MLH / Lu.ma / Hack Club / Hackeroos."
            )
            await interaction.followup.send("\n".join(lines), ephemeral=True)
            return

        # We have filtered results: answer like an intelligent agent
        lines = [f"🌍 Here are hackathons I found for **{window_label}**:\n"]
        count = 0
        for e, dt in filtered:
            if count >= 10:
                break
            count += 1

            title = e.get("title", "Untitled")
            url = e.get("url", "#")
            source = e.get("source", "Unknown")
            location = e.get("location") or "Location TBA / Online"
            label = source
            if (source or "").strip().lower() == "hackeroos":
                label = "Hackeroos 🦘"

            raw_date = e.get("start_date") or ""
            if dt:
                date_str = dt.strftime("%Y-%m-%d")
            elif raw_date:
                date_str = raw_date
            else:
                date_str = "Date coming soon"

            lines.append(f"• **{title}** — ({label}) • {location} • {date_str} → {url}")

        lines.append(
            "\nYou can also run `/hackathons` for an embed view, or ask more specific things like:\n"
            "• *\"Show Hackeroos events next month\"*\n"
            "• *\"Only online hackathons this weekend\"*"
        )

        await interaction.followup.send("\n".join(lines), ephemeral=True)
        return

    # -------------------------------------------------
    # 3) Everything else → send to Hugging Face LLM
    # -------------------------------------------------
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


@bot.tree.command(name="update-hackathons", description="Manually refresh hackathons feed (admin only)")
async def update_hackathons(interaction: discord.Interaction):
    # admin check
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "⚠️ Only admins can update hackathons.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    events = await fetch_hackathons()
    if not events:
        await interaction.followup.send("⚠️ Could not fetch any hackathons from API or fallback.", ephemeral=True)
        return

    # Save to data/hackathons.json
    os.makedirs("data", exist_ok=True)
    try:
        with open("data/hackathons.json", "w", encoding="utf-8") as f:
            json.dump(events, f, indent=2, ensure_ascii=False)
        await interaction.followup.send(
            f"✅ Hackathons updated successfully.\nTotal events saved: **{len(events)}**.",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(
            f"⚠️ Failed to save hackathons.json:\n```{e}```",
            ephemeral=True
        )


# -------------------------------------------------
# 13) RUN THE BOT
# -------------------------------------------------

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing in `.env` as DISCORD_TOKEN!")

bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)
