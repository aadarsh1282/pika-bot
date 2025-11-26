# scrape_hackathons.py
# Scrapes multiple hackathon sources (Devpost, MLH, Lu.ma, Hack Club)
# and writes a merged JSON file to data/hackathons.json for Pika-Bot.

import os
import json
from datetime import datetime
from typing import List, Dict

import httpx
from bs4 import BeautifulSoup
from seleniumbase import SB

OUTPUT_PATH = os.path.join("data", "hackathons.json")


def normalise_date(raw: str) -> str:
    """Light normaliser – just strips and returns a single-spaced string."""
    if not raw:
        return ""
    return " ".join(raw.split())


def make_event(
    *,
    title: str,
    url: str,
    start_date: str = "",
    location: str = "",
    source: str,
) -> Dict:
    return {
        "title": title.strip() if title else "",
        "url": url.strip() if url else "",
        "start_date": normalise_date(start_date),
        "location": location.strip() if location else "",
        "source": source,
    }


# -------------------------------------------------
# 1) DEVPOST — use JSON API instead of HTML scraping
# -------------------------------------------------

def scrape_devpost() -> List[Dict]:
    """
    Fetch upcoming Devpost hackathons via their public JSON API.
    This is much more stable than scraping HTML.
    """
    base_url = "https://devpost.com/api/hackathons"
    events: List[Dict] = []

    # Safety: don't hammer them, just grab a few pages max.
    max_pages = 3
    params = {
        "status": "upcoming",
        "challenge_type": "all",
        "per_page": 50,
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; PikaBotHackeroos/1.0; "
            "+https://github.com/aadarsh1282/pika-bot)"
        )
    }

    for page in range(1, max_pages + 1):
        qp = dict(params)
        qp["page"] = page

        try:
            resp = httpx.get(base_url, params=qp, headers=headers, timeout=15.0)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[Devpost] Error on page {page}: {e}")
            break

        hacks = data.get("hackathons") or []
        if not hacks:
            break

        for h in hacks:
            title = h.get("title") or ""
            url = h.get("url") or ""
            loc_obj = h.get("displayed_location") or {}
            location = loc_obj.get("location") or "Online / TBA"
            # e.g. "Oct 31 - Dec 05, 2025"
            start_str = h.get("submission_period_dates") or ""

            events.append(
                make_event(
                    title=title,
                    url=url,
                    start_date=start_str,
                    location=location,
                    source="Devpost",
                )
            )

    print(f"[Devpost] Collected {len(events)} events from API")
    return events


# -------------------------------------------------
# 2) MLH
# -------------------------------------------------

def scrape_mlh() -> List[Dict]:
    """Scrape upcoming MLH hackathons from the events page."""
    url = "https://mlh.io/events"
    events: List[Dict] = []

    with SB(uc=True, headless=True) as sb:
        sb.open(url)
        sb.sleep(4)
        html = sb.get_page_source()

    soup = BeautifulSoup(html, "html.parser")

    # "Upcoming Events" header
    upcoming_header = soup.find(
        lambda tag: tag.name in ["h2", "h3"] and "Upcoming Events" in tag.get_text()
    )
    if not upcoming_header:
        print("[MLH] Could not find 'Upcoming Events' header")
        return events

    # Walk links until "Past Events"
    current = upcoming_header
    while current:
        current = current.find_next(["a", "h2", "h3"])
        if not current:
            break

        if current.name in ["h2", "h3"] and "Past Events" in current.get_text():
            break

        if current.name == "a" and current.has_attr("href"):
            text = current.get_text(" ", strip=True)
            href = current["href"]

            if not text or "Upcoming Events" in text:
                continue

            title = text
            start_date = ""  # you can parse text to extract date later if you want
            location = ""

            if not href.startswith("http"):
                href = f"https://mlh.io{href}"

            events.append(
                make_event(
                    title=title,
                    url=href,
                    start_date=start_date,
                    location=location,
                    source="MLH",
                )
            )

    print(f"[MLH] Collected {len(events)} events")
    return events


# -------------------------------------------------
# 3) Lu.ma (hackathon tag)
# -------------------------------------------------

def scrape_luma() -> List[Dict]:
    """Scrape Lu.ma hackathons via the 'hackathon' tag page."""
    url = "https://lu.ma/tag/hackathon"
    events: List[Dict] = []

    with SB(uc=True, headless=True) as sb:
        sb.open(url)
        sb.sleep(5)  # Lu.ma is more JS-heavy
        html = sb.get_page_source()

    soup = BeautifulSoup(html, "html.parser")

    cards = soup.select("a[href*='/event/'], a[href*='lu.ma/']")

    for link in cards:
        href = link.get("href")
        if not href:
            continue

        # skip tag links
        if "/tag/" in href:
            continue

        title = link.get_text(" ", strip=True)
        if not title:
            continue

        if href.startswith("/"):
            href = f"https://lu.ma{href}"

        start_date = ""
        location = ""

        events.append(
            make_event(
                title=title,
                url=href,
                start_date=start_date,
                location=location,
                source="Lu.ma",
            )
        )

    print(f"[Lu.ma] Collected {len(events)} events")
    return events


# -------------------------------------------------
# 4) Hack Club Events
# -------------------------------------------------

def scrape_hackclub() -> List[Dict]:
    """Scrape Hack Club events page."""
    url = "https://events.hackclub.com"
    events: List[Dict] = []

    with SB(uc=True, headless=True) as sb:
        sb.open(url)
        sb.sleep(4)
        html = sb.get_page_source()

    soup = BeautifulSoup(html, "html.parser")

    cards = soup.select("a[href*='events.hackclub.com/event'], a[href*='/event/']")

    for link in cards:
        href = link.get("href")
        if not href:
            continue

        title = link.get_text(" ", strip=True)
        if not title:
            continue

        if href.startswith("/"):
            href = f"https://events.hackclub.com{href}"

        start_date = ""
        location = ""

        events.append(
            make_event(
                title=title,
                url=href,
                start_date=start_date,
                location=location,
                source="Hack Club",
            )
        )

    print(f"[Hack Club] Collected {len(events)} events")
    return events


# -------------------------------------------------
# MERGE + SAVE
# -------------------------------------------------

def merge_and_dedupe(all_lists: List[List[Dict]]) -> List[Dict]:
    """Merge lists and dedupe by URL."""
    seen = set()
    merged: List[Dict] = []

    for lst in all_lists:
        for item in lst:
            url = (item.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(item)

    # sort by (source, title) just to keep it tidy; /hackathons will re-sort by date
    merged.sort(key=lambda x: (x.get("source", ""), x.get("title", "").lower()))
    return merged


def main():
    print("🔎 Scraping hackathons from multiple sources...")

    devpost_events = scrape_devpost()
    mlh_events = scrape_mlh()
    luma_events = scrape_luma()
    hackclub_events = scrape_hackclub()

    print(f"Devpost: {len(devpost_events)} events")
    print(f"MLH: {len(mlh_events)} events")
    print(f"Lu.ma: {len(luma_events)} events")
    print(f"Hack Club: {len(hackclub_events)} events")

    merged = merge_and_dedupe([devpost_events, mlh_events, luma_events, hackclub_events])
    print(f"Total after merge/dedupe: {len(merged)} events")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

    print(f"✅ Saved to {OUTPUT_PATH} at {datetime.utcnow().isoformat()}Z")


if __name__ == "__main__":
    main()
