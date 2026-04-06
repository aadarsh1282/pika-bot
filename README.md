<div align="center">

# ⚡ PikaBot

**AI-Powered Hackathon Intelligence & Community Automation Platform**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![discord.py](https://img.shields.io/badge/discord.py-2.3.2-5865F2?style=flat-square&logo=discord&logoColor=white)](https://discordpy.readthedocs.io)
[![GitHub Actions](https://img.shields.io/badge/CI%2FCD-GitHub_Actions-2088FF?style=flat-square&logo=github-actions&logoColor=white)](https://github.com/features/actions)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Production-brightgreen?style=flat-square)]()
[![Discord](https://img.shields.io/badge/Try%20it%20Live-Join%20Hackeroos-5865F2?style=flat-square&logo=discord&logoColor=white)](https://discord.gg/fRbjEecC)

*Aggregates 250+ hackathons across 5 platforms · AI-driven Q&A · Real-time community moderation · Fully automated*

> **⚡ PikaBot is live.** [Join the Hackeroos Discord](https://discord.gg/fRbjEecC) to see it in action — run `/hackathons`, `/ask`, or `/poll` in the server.

</div>

---

## 📌 Overview

PikaBot is a production-grade Discord bot and hackathon intelligence system built for the **Hackeroos** community. It continuously scrapes, deduplicates, and serves hackathon data from multiple platforms — providing community members with real-time event discovery, AI-powered Q&A, and automated moderation through a unified Discord interface.

PikaBot solves three real problems:
- **Discovery fragmentation** — hackathon events are scattered across Devpost, MLH, Lu.ma, Hack Club, and more
- **Community noise** — unmoderated servers suffer from spam, raids, and content violations
- **Insight gap** — participants and organizers lack structured, queryable data on trends and events

---

## 🚀 Try it Live

PikaBot is running 24/7 in the **Hackeroos** Discord server. Join and interact with it directly:

<div align="center">

[![Join Hackeroos Discord](https://img.shields.io/badge/Join%20Hackeroos-Try%20PikaBot%20Live-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/fRbjEecC)

</div>

Once inside, try these commands in `#pika-bots`:

| Command | What to expect |
|---------|---------------|
| `/hackathons` | Live feed of 250+ upcoming hackathons |
| `/ask upcoming` | AI-curated list with links and dates |
| `/poll <question>` | Create a poll instantly |
| `/winners` | See the community leaderboard |
| `/pika-help` | Full command reference |

---

## 🚀 Key Features

### 🔍 Hackathon Intelligence
- Aggregates events from **Devpost, MLH, Lu.ma, Hack Club, and Hackeroos** into a single unified feed
- Auto-deduplicates cross-platform listings
- Posts new hackathons in real-time to the `#all-hackathons` Discord channel
- Standardises all event data: title, dates, location, mode (Online / In-Person / Hybrid), source

### 🤖 AI-Powered Q&A
- `/ask` command powered by **Hugging Face** (Qwen2.5 via `gradio_client`)
- Users can query hackathon advice, ask community questions, or get project feedback
- Infrastructure in place to evolve into a full RAG-based analytics assistant

### 📊 Community & Event Management
- `/hackathons` — browse upcoming events
- `/poll` — create instant community polls
- `/winners` & `/set-winner` — track and celebrate hackathon winners
- `/verify` — member onboarding and role assignment
- Countdown announcements for curated Hackeroos-specific events

### 🛡️ Advanced Moderation
- Spam detection with configurable rate-limit thresholds
- **Persistent strike system** — violation tracking per user stored in `strikes.json`
- **Raid protection** — detects and mitigates mass-join attacks
- Word/content filtering with admin control
- Thread-safe async state management via `asyncio.Lock`

### ⚙️ Automated Data Pipeline
- Daily scraper runs at **05:00 UTC** via GitHub Actions
- Smart commit logic — only pushes when new data is detected (`[skip ci]` tagged)
- Outputs a clean `data/hackathons.json` consumable by external frontends or APIs

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                        PikaBot System                         │
├─────────────────────────┬────────────────────────────────────┤
│      Data Layer         │          Bot Layer                  │
│                         │                                     │
│  scrape_hackathons.py   │           main.py                   │
│                         │                                     │
│  Devpost  ──┐           │   ┌─────────────┐  ┌────────────┐  │
│  MLH  ──────┤           │   │  Slash Cmds │  │ Moderation │  │
│  Lu.ma  ────┼──► merge  │   │ /hackathons │  │ Spam guard │  │
│  Hack Club ─┤   dedupe  │   │ /ask        │  │ Raid guard │  │
│  Hackeroos ─┘     │     │   │ /poll       │  │ Strikes    │  │
│                   │     │   │ /winners    │  └────────────┘  │
│  data/            ▼     │   └──────┬──────┘                  │
│  hackathons.json ◄──────┼──────────┘                         │
│                         │   AI Q&A (Hugging Face / Qwen2.5)  │
├─────────────────────────┴────────────────────────────────────┤
│                     CI/CD Layer                               │
│              GitHub Actions — Daily @ 05:00 UTC               │
└──────────────────────────────────────────────────────────────┘
```

---

## 📁 Folder Structure

```
pika-bot/
├── .github/
│   └── workflows/
│       └── hackathons-scraper.yml   # Automated daily scraper (GitHub Actions)
├── data/
│   ├── hackathons.json              # Aggregated + deduplicated events (250+)
│   └── hackeroos_events.json        # Curated Hackeroos community events
├── docs/
│   └── assets/                      # Screenshots, banners (add yours here)
├── main.py                          # Discord bot — commands, moderation, AI Q&A
├── scrape_hackathons.py             # Multi-source hackathon scraper
├── requirements.txt                 # Python dependencies
├── runtime.txt                      # Python version pin (3.11.8)
└── README.md
```

---

## ⚙️ Setup & Installation

### Prerequisites
- Python 3.11+
- A Discord bot token ([create one here](https://discord.com/developers/applications))
- Hugging Face token *(optional — enables `/ask` AI feature)*

### 1. Clone the repository

```bash
git clone https://github.com/aadarsh1282/pika-bot.git
cd pika-bot
```

### 2. Create a virtual environment

```bash
python -m venv venv
source venv/bin/activate        # macOS / Linux
venv\Scripts\activate           # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

Create a `.env` file in the project root:

```env
# Required
DISCORD_TOKEN=your_discord_bot_token_here

# Optional — enables /ask AI feature
HF_TOKEN=your_huggingface_token_here

# Optional — custom hackathon feed endpoint
HACKATHONS_API_BASE=https://your-api-endpoint.com
```

### 5. Run the bot

```bash
python main.py
```

### 6. Run the scraper manually *(optional)*

```bash
python scrape_hackathons.py
```

> The scraper runs automatically every day at 05:00 UTC via GitHub Actions. Running it manually refreshes `data/hackathons.json` immediately.

---

## 🔌 Live API

PikaBot is backed by the **Hackeroos Insights API** — a production FastAPI service deployed on Railway that powers the bot's hackathon intelligence.

**Base URL:** `https://hackeroos-insights-api-production.up.railway.app`

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/hackathons/upcoming` | GET | Upcoming hackathons (filter by `days`, `tag`, `mode`, `location`) |
| `/hackathons/stats` | GET | Aggregate stats across the hackathon dataset |

**Interactive docs:** `https://hackeroos-insights-api-production.up.railway.app/redoc`

### Event Schema

```json
{
  "title": "HackSydney 2026",
  "url": "https://devpost.com/hacksydney2026",
  "start_date": "Apr 28 - 29, 2026",
  "end_date": "2026-04-29T23:59:59Z",
  "location": "Sydney, Australia",
  "mode": "in-person",
  "source": "Devpost",
  "tags": ["ai", "fintech", "sustainability"],
  "description": "48-hour hackathon at the University of Sydney."
}
```

### Supported Sources

| Source | Method | Approx. Events |
|--------|--------|----------------|
| Devpost | REST API | ~100+ |
| MLH | Selenium (JS-rendered) | ~50+ |
| Lu.ma | BeautifulSoup | ~40+ |
| Hack Club | HTML scraper | ~30+ |
| Hackeroos | Curated JSON | ~20+ |

### Consuming the feed

```python
import json

with open("data/hackathons.json") as f:
    hackathons = json.load(f)

online = [h for h in hackathons if h.get("mode") == "online"]
print(f"{len(online)} online hackathons found")
```

---

## 🤖 Discord Commands

| Command | Description | Access |
|---------|-------------|--------|
| `/hackathons` | Browse upcoming hackathon events | Everyone |
| `/ask <question>` | AI-powered Q&A via Hugging Face | Everyone |
| `/poll <question>` | Create a community poll | Everyone |
| `/winners` | View hackathon winners leaderboard | Everyone |
| `/set-winner` | Record a hackathon winner | Admin |
| `/verify` | Member onboarding & role assignment | Everyone |
| `/pika-help` | Show all available commands | Everyone |
| `/about` | About PikaBot | Everyone |
| `/faq` | Frequently asked questions | Everyone |

---

## 🔄 CI/CD Pipeline

```
Trigger: Daily cron at 05:00 UTC  (or manual via workflow_dispatch)
│
├── Checkout repo
├── Setup Python 3.11
├── pip install -r requirements.txt
├── python scrape_hackathons.py
└── data/hackathons.json changed?
    ├── Yes → git commit "Update hackathons JSON [skip ci]" + push
    └── No  → skip (no unnecessary commits)
```

---

## 🛣️ Roadmap

- [x] Multi-source hackathon aggregation (5 platforms)
- [x] Discord slash commands
- [x] AI Q&A via Hugging Face
- [x] Moderation system (spam, raids, strikes)
- [x] Automated daily scraper via GitHub Actions
- [ ] RAG-based AI using the hackathon dataset as a knowledge base
- [ ] Web dashboard for browsing hackathon analytics
- [ ] Trend detection and winning pattern analysis
- [ ] REST API for external integrations
- [ ] User authentication & personalised event recommendations
- [ ] MLOps pipeline for production model deployment
- [ ] Real-time WebSocket feed for live event updates

---

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Commit your changes: `git commit -m 'Add your feature'`
4. Push: `git push origin feature/your-feature`
5. Open a Pull Request

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

<div align="center">

Built with ⚡ by **Aadarsh Karki**

[GitHub](https://github.com/aadarsh1282) · [Email](mailto:aadarshk56@gmail.com)

</div>
