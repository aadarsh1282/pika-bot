# scripts/update_hackathons.py
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

SOURCES = [
    {"url": "https://mlh.io/seasons/2025/events.json", "source": "MLH"},
    {"url": "https://lu.ma/api/events?pagination_limit=100&upcoming=true", "source": "Lu.ma"},
    {"url": "https://www.hackathon.com/api/events/upcoming", "source": "Hackathon.com"},
    {"url": "https://events.hackclub.com/api/events/upcoming", "source": "Hack Club"},
]


def fetch_json(url: str) -> Any:
    """Fetch JSON from a URL, return parsed JSON or [] on error."""
    try:
        resp = httpx.get(url, timeout=20.0)
        resp.raise_for_status()
        print(f"✅ Fetched {url} (status {resp.status_code})")
        return resp.json()
    except Exception as e:
        print(f"⚠️ Failed {url}: {e}")
        return []


def parse_date(value: Optional[str]) -> Optional[str]:
    """
    Try to normalise different date formats to ISO-8601 (YYYY-MM-DD or full datetime).
    If parsing fails, return None so we can push it to the end of the list.
    """
    if not value:
        return None

    value = value.strip()

    # Common patterns these APIs might use
    candidates = [
        value,
        value.replace("Z", "+00:00"),  # ISO with Z → offset
    ]

    for cand in candidates:
        for fmt in (
            "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                dt = datetime.strptime(cand, fmt)
                # store as ISO date if no time, else full ISO
                if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
                    return dt.date().isoformat()
                return dt.isoformat()
            except Exception:
                continue

    # last resort: try fromisoformat
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
            return dt.date().isoformat()
        return dt.isoformat()
    except Exception:
        print(f"⚠️ Could not parse date: {value}")
        return None


def normalize_event(event: Dict[str, Any], source: str) -> Optional[Dict[str, Any]]:
    """
    Convert different provider schemas into one unified structure:
    {
        "title": str,
        "url": str,
        "start_date": str | None (ISO),
        "location": str,
        "source": str
    }
    """
    if source == "MLH":
        title = event.get("name")
        url = event.get("url") or (f"https://mlh.io/events/{event.get('slug')}" if event.get("slug") else None)
        start_date = parse_date(event.get("startDate") or event.get("start_date"))
        location = event.get("location", "Online")

        return {
            "title": title,
            "url": url,
            "start_date": start_date,
            "location": location or "Online",
            "source": "MLH",
        }

    elif source == "Lu.ma":
        title = event.get("title")
        url = event.get("url")
        # Lu.ma often uses start_at / start_date; handle both.
        start_date = parse_date(event.get("start_date") or event.get("start_at"))
        is_online = event.get("is_online") or event.get("online", False)
        location = "Online" if is_online else "In-Person"

        return {
            "title": title,
            "url": url,
            "start_date": start_date,
            "location": location,
            "source": "Lu.ma",
        }

    elif source == "Hackathon.com":
        title = event.get("title")
        url = event.get("event_url") or event.get("url")
        start_date = parse_date(event.get("start_date") or event.get("startDate"))
        location = event.get("location", "Online")

        return {
            "title": title,
            "url": url,
            "start_date": start_date,
            "location": location or "Online",
            "source": "Hackathon.com",
        }

    elif source == "Hack Club":
        title = event.get("title")
        url = event.get("url")
        start_date = parse_date(event.get("start") or event.get("start_date"))
        location = event.get("location") or "Various"

        return {
            "title": title,
            "url": url,
            "start_date": start_date,
            "location": location,
            "source": "Hack Club",
        }

    return None


def main() -> None:
    all_events: List[Dict[str, Any]] = []

    for src in SOURCES:
        print(f"\n🔎 Fetching {src['source']}...")
        data = fetch_json(src["url"])

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            # different APIs use different top-level keys
            items = data.get("events") or data.get("data") or data.get("results") or []
        else:
            items = []

        print(f"  • Raw items from {src['source']}: {len(items)}")

        for item in items:
            norm = normalize_event(item, src["source"])
            if not norm:
                continue

            if not norm.get("title") or not norm.get("url"):
                continue  # require basic info

            all_events.append(norm)

    print(f"\n📦 Total normalised events before dedupe: {len(all_events)}")

    # Dedupe by URL
    seen: set[str] = set()
    unique: List[Dict[str, Any]] = []
    for e in all_events:
        key = e.get("url")
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)

    print(f"🚿 After dedupe: {len(unique)}")

    # Sort by start date, unknown dates go to the end
    def sort_key(e: Dict[str, Any]):
        raw = e.get("start_date")
        if not raw:
            return datetime(2099, 1, 1)
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return datetime(2099, 1, 1)

    unique.sort(key=sort_key)

    # Optionally: filter out events clearly in the past (keep future + near-past)
    today = datetime.utcnow().date()
    filtered: List[Dict[str, Any]] = []
    for e in unique:
        sd = e.get("start_date")
        try:
            d = datetime.fromisoformat(sd.replace("Z", "+00:00")).date() if sd else None
        except Exception:
            d = None

        if d is None or d >= today:
            filtered.append(e)

    print(f"⏭ After filtering past events: {len(filtered)}")

    # Save top 50 to data/hackathons.json
    Path("data").mkdir(exist_ok=True)
    out_path = Path("data/hackathons.json")
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(filtered[:50], f, indent=2, ensure_ascii=False)

    print(f"\n✅ Updated {out_path} with {len(filtered[:50])} upcoming hackathons.")


if __name__ == "__main__":
    main()
