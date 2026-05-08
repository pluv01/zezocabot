# 🐳 Dormant Whale Monitor Bot

A lightweight Telegram bot that watches whale wallets across **Solana, ETH, and BSC** and fires an alert when a previously dormant wallet (30+ days inactive) makes a large memecoin move.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Railway.app                       │
│                                                     │
│  ┌─────────────────┐     ┌───────────────────────┐  │
│  │  Telegram Bot   │     │  Flask (port 8080)    │  │
│  │  (polling)      │     │  /helius-webhook      │  │
│  │                 │     │  /health              │  │
│  └────────┬────────┘     └──────────┬────────────┘  │
│           │                         │ Helius push   │
│  ┌────────▼─────────────────────────▼────────────┐  │
│  │              Core Logic                       │  │
│  │  whale_store.py   alert_builder.py            │  │
│  │  moralis_monitor.py (APScheduler poll)        │  │
│  └───────────────────────────────────────────────┘  │
│                      │                              │
│              whales.json (persistent volume)        │
└─────────────────────────────────────────────────────┘
         ▲                          ▲
   Moralis API              Helius Webhooks
   (ETH / BSC)                (Solana)
```

---

## Setup: API Keys

### 1. Telegram Bot Token
1. Message [@BotFather](https://t.me/BotFather) on Telegram.
2. Send `/newbot` and follow the prompts.
3. Copy the token (looks like `7123456789:AAxxxxxx`).
4. Get your chat ID by messaging [@userinfobot](https://t.me/userinfobot).

### 2. Helius API Key (Solana)
1. Sign up at [dashboard.helius.dev](https://dashboard.helius.dev/).
2. Create a new project → copy the **API Key**.
3. You don't need to manually create a webhook — `/addwhale` does it automatically.

### 3. Moralis API Key (ETH + BSC)
1. Sign up at [admin.moralis.io](https://admin.moralis.io/).
2. Go to **Web3 APIs** → copy your **API Key**.
3. The free tier allows ~40,000 CU/month — enough for polling a handful of wallets.

---

## Deploy on Railway

### Step 1 — Push code to GitHub
```bash
git init
git add .
git commit -m "initial whale bot"
git remote add origin https://github.com/YOUR_USERNAME/whale-bot.git
git push -u origin main
```

### Step 2 — Create Railway project
1. Go to [railway.app](https://railway.app/) → **New Project** → **Deploy from GitHub repo**.
2. Select your repository.
3. Railway auto-detects `Procfile` and starts the build.

### Step 3 — Add a Persistent Volume (important!)
`whales.json` must survive redeploys.
1. In your Railway service → **Volumes** tab → **Add Volume**.
2. Mount path: `/app`
3. This ensures `whales.json` is preserved across deployments.

### Step 4 — Set Environment Variables
In Railway: **Variables** tab → add each of the following:

| Variable | Value | Required |
|---|---|---|
| `TELEGRAM_TOKEN` | Your BotFather token | ✅ |
| `TELEGRAM_CHAT_ID` | Your Telegram user/chat ID | ✅ |
| `HELIUS_API_KEY` | From Helius dashboard | ✅ |
| `HELIUS_WEBHOOK_URL` | `https://YOUR-APP.up.railway.app/helius-webhook` | ✅ |
| `MORALIS_API_KEY` | From Moralis dashboard | ✅ |
| `HELIUS_WEBHOOK_SECRET` | Optional HMAC secret | ⬜ |
| `MIN_USD_VALUE` | Min $ to alert (default: `10000`) | ⬜ |
| `DORMANT_DAYS_MIN` | Min days dormant (default: `30`) | ⬜ |
| `MORALIS_POLL_MINS` | EVM poll interval in min (default: `5`) | ⬜ |

> ⚠️ Set `HELIUS_WEBHOOK_URL` **after** Railway gives you a domain (Settings → Networking → Generate Domain), then redeploy once.

### Step 5 — Verify deployment
- Check **Logs** tab in Railway — you should see:
  ```
  Flask webhook server starting on port 8080
  EVM polling scheduler started (every 5 min)
  Telegram bot polling started…
  ```
- Hit `https://YOUR-APP.up.railway.app/health` — should return `{"status": "ok", "whales": 0}`.

---

## Bot Commands

| Command | Description |
|---|---|
| `/addwhale <address> <chain>` | Start tracking a wallet. Chain: `sol`, `eth`, `bsc` |
| `/listwhales` | Show all tracked wallets with dormancy info |
| `/removewhale <address>` | Stop tracking a wallet |
| `/status` | Show bot config and wallet count |

### Example
```
/addwhale 7xKpABCdef1234567890abcdefGHIJKLMN sol
/addwhale 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045 eth
/addwhale 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045 bsc
```

---

## Alert Format

```
━━━━━━━━━━━━━━━━━━━━
🟣 SOLANA WHALE WOKE UP
━━━━━━━━━━━━━━━━━━━━
💤 Dormant for: 47 days
🐳 Wallet: 7xKpAB…6045  [solscan link]
🪙 Token: Bonk (BONK)
📦 Amount sold: 14,200,000 BONK
💵 ~USD value: $18,400
🔗 TX: abc123…  [solscan link]
```

---

## Local Development

```bash
# 1. Clone and install deps
git clone https://github.com/YOUR_USERNAME/whale-bot.git
cd whale-bot
pip install -r requirements.txt

# 2. Copy and fill out env
cp .env.example .env
# edit .env with your keys

# 3. Load env and run
export $(cat .env | xargs)
python bot.py
```

For Helius webhooks locally, use [ngrok](https://ngrok.com/):
```bash
ngrok http 8080
# Set HELIUS_WEBHOOK_URL=https://your-ngrok-url.ngrok.app/helius-webhook
```

---

## Notes & Limitations

- **whales.json** is a flat file — fine for up to ~100 wallets. For larger lists, swap in SQLite.
- **Moralis free tier** has CU limits. Polling every 5 min with 10 wallets uses ~288 requests/day — well within limits.
- **Helius free tier** supports up to 1 webhook with up to 100 addresses.
- **USD values**: Helius sometimes omits USD pricing for very new or illiquid tokens. The fallback estimation uses a hardcoded SOL price in `alert_builder.py` — update `SOL_PRICE_APPROX` or integrate a price oracle (CoinGecko free API) for accuracy.
- **No false-positive protection** for EVM: if you add the same address to ETH and BSC separately, you may get duplicate alerts if that address is active on both chains.
