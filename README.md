# Pika-Bot – Hackeroos Discord Assistant

Pika-Bot is a Discord bot built for the **Hackeroos** community to help members discover hackathons, get updates, and stay engaged. It combines:

- A **Discord bot** (`main.py`) for commands, onboarding, moderation and announcements  
- A **hackathon scraper** (`scrape_hackathons.py`) that merges events from multiple sources  
- A **JSON feed** (`data/hackathons.json`) that can also be used by the Hackeroos frontend or other tools  

This repository is designed to be easy to hand over and maintain for future development (e.g. Pika-Bot Version 2).

---

## 🌟 Key Features

- **Hackathon auto-alerts**
  - Fetches hackathons from the **Hackeroos Insights API** and/or `data/hackathons.json`
  - Detects new online/global events and posts them in the `#all-hackathons` channel
- **Hackeroos-specific reminders**
  - Uses curated Hackeroos events (via `data/hackeroos_events.json` if present)  
  - Supports pinned countdown/reminder style messaging in announcement channels
- **Moderation helpers**
  - Simple PG-friendly word filter
  - Strike tracking via `strikes.json`
  - Basic raid / spam detection (joins, emoji spam, mention spam)
- **AI-ready integration**
  - Configured to call a Hugging Face router model (`Qwen2.5-72B-Instruct`) via the `openai` client (future/optional `/ask` and insights features)

---

## 🧱 Tech Stack

- **Language:** Python 3.10+
- **Discord:** `discord.py`
- **Config:** `python-dotenv` (`.env` file)
- **HTTP / APIs:** `httpx`
- **Scraping:** `httpx`, `beautifulsoup4`, `seleniumbase`, `undetected-chromedriver`
- **AI:** `openai` client for Hugging Face router (configured via env vars)
- **Automation:** GitHub Actions (`.github/workflows`)

---

## 📂 Project Structure

```text
.
├── .github/
│   └── workflows/          # GitHub Actions (e.g. scrape + update hackathons.json)
├── data/
│   ├── hackathons.json     # merged events feed (Devpost + others)
│   └── hackeroos_events.json (optional, curated Hackeroos events)
├── main.py                 # Discord bot (commands, alerts, moderation, auto-loops)
├── scrape_hackathons.py    # Script to build/refresh data/hackathons.json
├── requirements.txt        # Python dependencies
├── runtime.txt             # Runtime hint (e.g. for Railway/Heroku-style platforms)
└── README.md               # This file
