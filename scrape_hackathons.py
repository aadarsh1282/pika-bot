# scrape_hackathons.py
# Scrapes multiple hackathon sources (Devpost, MLH, Lu.ma, Hack Club)
# and writes a merged JSON file to data/hackathons.json for Pika-Bot.


import os
import json
from datetime import datetime
from typing import List, Dict

from bs4 import BeautifulSoup
from seleniumbase import SB


OUTPUT_PATH = os.path.join("data", "hackathons.json")


def normalise_date(raw: str) -> str:
    """Light normaliser – just strips and returns string.
    If you want full ISO parsing later, you can improve this.
    """
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
# 1) DEVPOST
# -------------------------------------------------

def scrape_devpost() -> List[Dict]:
    """Scrape upcoming Devpost hackathons."""
    url = "https://devpost.com/hackathons?status=upcoming&challenge_type=all"
    events: List[Dict] = []

    with SB(uc=True, headless=True) as sb:
        sb.open(url)
        sb.sleep(4)  # let content load
        html = sb.get_page_source()

    soup = BeautifulSoup(html, "html.parser")

    # Devpost changes layout sometimes; this targets common card styles.
    # If it ever breaks, inspect HTML and adjust selectors here.
    cards = soup.select("ul.hackathons-list li, div.challenge-listing")

    for card in cards:
        link = card.find("a", href=True)
        if not link:
            continue

        title = link.get_text(strip=True)
        href = link["href"]
        if href.startswith("/"):
            href = f"https://devpost.com{href}"

        # try to find date & location
        meta = card.get_text(" ", strip=True)
        # this is intentionally relaxed; you can add regex if you want.
        start_date = ""
        location = ""

        events.append(
            make_event(
                title=title,
                url=href,
                start_date=start_date,
                location=location,
                source="Devpost",
            )
        )

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

    # MLH "Upcoming Events" section – each event is basically a link row.
    upcoming_header = soup.find(
        lambda tag: tag.name in ["h2", "h3"] and "Upcoming Events" in tag.get_text()
    )
    if not upcoming_header:
        return events

    # Grab links until we hit "Past Events"
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

            # Very loose parsing: "HackDavis Apr 19th - 20th Davis , CA In-Person Only"
            parts = text.split()
            title = text
            start_date = ""
            location = ""
            # You can refine this later with regex if you like.

            if not href.startswith("http"):
                href = href if href.startswith("http") else f"https://mlh.io{href}"

            events.append(
                make_event(
                    title=title,
                    url=href,
                    start_date=start_date,
                    location=location,
                    source="MLH",
                )
            )

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
        sb.sleep(5)  # Lu.ma can be a bit slower
        html = sb.get_page_source()

    soup = BeautifulSoup(html, "html.parser")

    # Layout changes sometimes; target generic event cards with links.
    # You may want to tweak this selector if Lu.ma redesigns.
    cards = soup.select("a[href*='/event/'], a[href*='lu.ma/']")

    for link in cards:
        href = link.get("href")
        if not href:
            continue

        # avoid obvious non-event links
        if "/tag/" in href:
            continue

        title = link.get_text(" ", strip=True)
        if not title:
            continue

        if href.startswith("/"):
            href = f"https://lu.ma{href}"

        # try to find date + location near the link
        parent = link.find_parent()
        text_block = parent.get_text(" ", strip=True) if parent else title
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

    # Hack Club events are usually cards linking out to event pages.
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

        parent = link.find_parent()
        text_block = parent.get_text(" ", strip=True) if parent else title

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
            url = item.get("url", "").strip()
            if not url:
                continue
            if url in seen:
                continue
            seen.add(url)
            merged.append(item)

    # optional: sort by title, or by source, etc.
    merged.sort(key=lambda x: (x.get("source", ""), x.get("title", "").lower()))
    return merged


def main():
    print("🔎 Scraping hackathons from multiple sources...")

    devpost_events = scrape_devpost()
    print(f"Devpost: {len(devpost_events)} events")

    mlh_events = scrape_mlh()
    print(f"MLH: {len(mlh_events)} events")

    luma_events = scrape_luma()
    print(f"Lu.ma: {len(luma_events)} events")

    hackclub_events = scrape_hackclub()
    print(f"Hack Club: {len(hackclub_events)} events")

    merged = merge_and_dedupe([devpost_events, mlh_events, luma_events, hackclub_events])
    print(f"Total after merge/dedupe: {len(merged)} events")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

    print(f"✅ Saved to {OUTPUT_PATH} at {datetime.utcnow().isoformat()}Z")


if __name__ == "__main__":
    main()
