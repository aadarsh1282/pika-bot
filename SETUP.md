```markdown
# PikaBot – Setup Guide

This guide explains how to install and run PikaBot locally for development or testing.  
Follow these steps if you are maintaining or extending the bot.

---

## 1. Prerequisites

Make sure you have the following installed:

- **Python 3.10+**
- **Git**
- **A Discord Bot Token** (from the Discord Developer Portal)
- **A Discord server** where you have permission to add bots

Optional (for AI features):

- A **Hugging Face token** (or other LLM provider)
- API access to the **Hackeroos Insights API**

---

## 2. Clone the Repository

```bash
git clone https://github.com/aadarsh1282/pika-bot.git
cd pika-bot
```

---

## 3. Create a Virtual Environment

```bash
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
```

---

## 4. Install Dependencies

```bash
pip install -r requirements.txt
```

---

## 5. Configure Environment Variables

Create a `.env` file in the project root:

```bash
DISCORD_TOKEN=your_discord_bot_token_here

# Optional (AI)
HF_TOKEN=your_huggingface_or_router_key_here
HUGGINGFACE_MODEL=Qwen/Qwen2.5-72B-Instruct

# Optional (Hackeroos API)
HACKATHONS_API_BASE=https://hackeroos-insights-api-production.up.railway.app
HACKATHONS_JSON_URL=https://raw.githubusercontent.com/aadarsh1282/pika-bot/main/data/hackathons.json
```

> ⚠️ **Do NOT commit `.env`** — it contains secrets.  
> Check `.env.example` for a full variable reference.

---

## 6. Running the Discord Bot

```bash
python main.py
```

If everything is configured correctly, PikaBot will appear **online** in your Discord server.

---

## 7. Testing the Hackathon Scraper (Optional)

```bash
python scrape_hackathons.py
```

This script will:

- Fetch hackathons from **Devpost / MLH**
- Normalise them into a unified JSON format
- Save them to `data/hackathons.json`

---

## 8. Troubleshooting

### ❌ Bot doesn’t come online
- Check `DISCORD_TOKEN` is correct  
- Ensure the token hasn’t been reset in the Developer Portal  
- Make sure the bot has been invited with the correct permissions

### ❌ Slash commands not appearing
- Ensure the bot has **applications.commands** enabled  
- Restart the bot after adding commands  
- Allow 1–2 minutes for Discord syncing

### ❌ Scraper errors
- Devpost / MLH may throttle or block requests  
- Check network permissions if using Railway or similar  
- Run the script locally first to verify config

---
```
