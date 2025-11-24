# scripts/update_hackathons.py
import json
import httpx
from datetime import datetime
from pathlib import Path

SOURCES = [
    {"url": "https://mlh.io/seasons/2025/events.json", "source": "MLH"},
    {"url": "https://lu.ma/api/events?pagination_limit=100&upcoming=true", "source": "Lu.ma"},
    {"url": "https://www.hackathon.com/api/events/upcoming", "source": "Hackathon.com"},
    {"url": "https://events.hackclub.com/api/events/upcoming", "source": "Hack Club"},
]

def fetch_json(url):
    try:
        r = httpx.get(url, timeout=15.0)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Failed {url}: {e}")
        return []

def normalize_event(event, source):
    if source == "MLH":
        return {
            "title": event.get("name"),
            "url": event.get("url") or f"https://mlh.io/events/{event.get('slug')}",
            "start_date": event.get("startDate"),
            "location": event.get("location", "Online"),
            "source": "MLH"
        }
    elif source == "Lu.ma":
        return {
            "title": event.get("title"),
            "url": event.get("url"),
            "start_date": event.get("start_date"),
            "location": "Online" if event.get("is_online") else "In-Person",
            "source": "Lu.ma"
        }
    elif source == "Hackathon.com":
        return {
            "title": event.get("title"),
            "url": event.get("event_url"),
            "start_date": event.get("start_date"),
            "location": event.get("location", "Online"),
            "source": "Hackathon.com"
        }
    elif source == "Hack Club":
        return {
            "title": event.get("title"),
            "url": event.get("url"),
            "start_date": event.get("start"),
            "location": "Various",
            "source": "Hack Club"
        }
    return None

def main():
    all_events = []
    for src in SOURCES:
        print(f"Fetching {src['source']}...")
        data = fetch_json(src["url"])
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("events", []) or data.get("data", []) or []
        else:
            items = []

        for item in items:
            norm = normalize_event(item, src["source"])
            if norm and norm["title"]:
                all_events.append(norm)

    # Dedupe by URL
    seen = set()
    unique = []
    for e in all_events:
        key = e["url"]
        if key not in seen:
            seen.add(key)
            unique.append(e)

    # Sort by start date
    def safe_date(e):
        try:
            return datetime.fromisoformat(e["start_date"].replace("Z", "+00:00"))
        except Exception:
            return datetime(2099, 1, 1)

    unique.sort(key=safe_date)

    Path("data").mkdir(exist_ok=True)
    with open("data/hackathons.json", "w", encoding="utf-8") as f:
        json.dump(unique[:50], f, indent=2, ensure_ascii=False)

    print(f"Updated! {len(unique)} upcoming hackathons saved.")

if __name__ == "__main__":
    main()
