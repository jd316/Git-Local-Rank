<div align="center">
  <img src="https://raw.githubusercontent.com/lucide-icons/lucide/main/icons/map-pin.svg" width="60" alt="Logo"/>
  <h1>Git Local Rank</h1>
  <p><strong>Find out how you stack up against other developers in your city.</strong></p>
  <br>
</div>

Git Local Rank is a Python/Flask web application that securely authenticates users via GitHub OAuth, converts any global PIN/ZIP code into a geographical region (using OpenStreetMap Nominatim), and scrubs GitHub to find and rank developers in that exact area.

It uses a balanced composite scoring algorithm factoring in followers, repositories, gists, and account age to rank developers fairly.

---

## 🔥 Features

* **Flexible Auth**: OAuth login for production, or `GITHUB_TOKEN` for local dev—both give 5,000 req/hr and a seamless experience.
* **Smart Global Geocoding**: Enter any PIN or ZIP code in the world. The backend will seamlessly map it to cities and regions (e.g. `743165` -> `Barrackpore, West Bengal`).
* **Deep Location Search**: The engine automatically constructs variations of the user's location to capture developers who format their GitHub bios differently.
* **Beautiful Dashboard**:
  * Sleek dark mode design.
  * Real-time loading states and smooth transitions.
  * Distribution charts powered by `Chart.js`.
  * Visual leaderboard of top local talent.

## 📊 How The Score Is Calculated

The ranking score is a logarithmic composite designed to balance highly-followed influencers against long-time active open-source contributors:

```math
Score = 40\% \log_{10}(\text{Followers}) + 30\% \log_{10}(\text{Repos}) + 5\% \log_{10}(\text{Gists}) + 25\% \log_{10}(\text{Account Age})
```

## 🛠️ Setup & Installation

### 1. Clone the repository

```bash
git clone https://github.com/yourusername/github-local-rank.git
cd github-local-rank
```

### 2. Set up the virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install requirements

```bash
pip install -r requirements.txt
```

### 4. Configure environment

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

**Option A — Local dev (simplest):** Set `GITHUB_TOKEN` to a [GitHub Personal Access Token](https://github.com/settings/tokens) (read:user scope). No OAuth setup needed; the web app works without login on localhost.

**Option B — OAuth (for production or shared use):** Create a GitHub OAuth App at **GitHub Settings > Developer Settings > OAuth Apps** (Homepage: `http://localhost:5000`, Callback: `http://localhost:5000/callback`), then set `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` in `.env`.

Generate a random `FLASK_SECRET_KEY` (e.g. `python -c "import secrets; print(secrets.token_hex(32))"`).

### 5. Run the Application

**For Development:**

```bash
python app.py
```

**For Production:**

```bash
gunicorn -w 4 -b 127.0.0.1:5000 app:app
```

Open [http://localhost:5000](http://localhost:5000) in your browser.

### 6. (Optional) Prevent Vercel cold starts

On the free tier, serverless functions scale down when idle. To avoid slow first loads after inactivity, set up a free external cron to ping the app every 10–15 minutes:

1. **[cron-job.org](https://cron-job.org)** – Create a job, set URL to `https://git-local-rank.vercel.app/ping`, schedule every 10 minutes.
2. **[UptimeRobot](https://uptimerobot.com)** – Add a monitor with the same URL; default 5-min check keeps the app warm.

Both services are free and keep the function warm with minimal traffic.

### 7. (Optional) Shareable links

Connect **Upstash Redis** via Vercel Storage (Storage → Create → Upstash for Redis). Once connected, the Share button on results creates links that persist for 7 days. No manual env setup needed—Vercel adds them automatically.

---

## 🏗️ Architecture

* **Backend:** Python + Flask

* **Frontend:** Vanilla HTML/CSS/JS (No heavy frameworks for blazing fast performance)
* **Geocoding API:** OpenStreetMap (Nominatim), with India Post & Zippopotam fallbacks.
* **Data Source:** GitHub REST API v3

## 📝 License

MIT License.
