# Baltimore Eviction Canvasser Tool

A civic tech tool that fetches upcoming Baltimore City landlord-tenant court cases,
enriches them via the Maryland Judiciary case search API, and generates printable
canvasser walk sheets using Claude AI.

**~$0.003 per walk sheet · 20-case run ≈ $0.06 · hard cap at 60 cases / $0.18**

---

## Repository structure

```
├── app.py                  ← Flask backend
├── frontend/
│   └── index.html          ← Single-file frontend (open directly in browser)
├── tests/
│   └── test_app.py         ← Test suite (runs in CI)
├── .github/
│   └── workflows/
│       ├── ci.yml          ← Runs tests on every push/PR
│       └── deploy.yml      ← Deploys to Railway on merge to main
├── .env.example            ← Template — copy to .env locally
├── .gitignore              ← Keeps secrets + venvs out of git
├── requirements.txt
└── Procfile                ← For Railway / Heroku deployment
```

---

## Local setup (5 minutes)

### 1. Clone and install

```bash
git clone https://github.com/YOUR-ORG/eviction-canvasser
cd eviction-canvasser
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 2. Add your API key

```bash
cp .env.example .env
# Now edit .env and paste your key:
# ANTHROPIC_API_KEY=sk-ant-...
```

> **Your `.env` file is gitignored.** It will never be committed.

### 3. Run the server

```bash
python app.py
# → http://localhost:5050
```

### 4. Open the frontend

Open `frontend/index.html` in your browser. In the **Setup** tab, confirm the
API URL is `http://localhost:5050`, then go to **Fetch docket** and run.

---

## GitHub secrets setup

For CI tests and production deployment, add your key as a **GitHub repository secret** — it's encrypted and never visible in logs.

### Step-by-step

1. Go to your repository on GitHub
2. Click **Settings** → **Secrets and variables** → **Actions**
3. Click **New repository secret**
4. Name: `ANTHROPIC_API_KEY`
5. Value: your key (`sk-ant-...`)
6. Click **Add secret**

That's it. The CI workflow (`ci.yml`) will now run live API tests on push.

```
GitHub repo
└── Settings
    └── Secrets and variables
        └── Actions
            └── ANTHROPIC_API_KEY  ← add here
```

> **Never put your key in `app.py`, `index.html`, or any tracked file.**
> If you accidentally commit a key, rotate it immediately at
> https://console.anthropic.com and then remove it from git history with
> `git filter-repo` or BFG Cleaner.

---

## Production deployment (Railway — free tier available)

Railway is the easiest zero-config host for this Flask app.

### One-time Railway setup

1. Create a free account at [railway.app](https://railway.app)
2. New Project → **Deploy from GitHub repo** → select this repo
3. In the Railway dashboard, go to your service → **Variables**
4. Add variable: `ANTHROPIC_API_KEY` = your key
5. Also add: `PORT` = `5050`  (Railway injects `$PORT` automatically, but this makes it explicit)

### Wire up auto-deploy via GitHub Actions

1. In Railway: **Account Settings** → **Tokens** → **New Token** → copy it
2. In GitHub: **Settings** → **Secrets** → **New secret**
   - Name: `RAILWAY_TOKEN`
   - Value: the token you copied
3. Now every push to `main` triggers `.github/workflows/deploy.yml` and auto-deploys

### Point the frontend at your live backend

In `frontend/index.html`, update the default API URL in the Setup tab, or
just change it at runtime in the UI. You can also host the frontend for free
on **GitHub Pages** (see below).

---

## Hosting the frontend on GitHub Pages

The frontend is a single HTML file with no build step.

1. Go to repo **Settings** → **Pages**
2. Source: **Deploy from a branch** → branch: `main`, folder: `/frontend`
3. Click **Save** — your site will be at `https://YOUR-ORG.github.io/eviction-canvasser/`

Then update the default API URL in `index.html` to your Railway backend URL before committing.

---

## Environment variables reference

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | ✅ Yes | Your Anthropic API key |
| `PORT` | No | Server port (default `5050`) |
| `ALLOWED_ORIGINS` | No | Comma-separated CORS origins (default `*`). Set to your frontend URL in production, e.g. `https://your-org.github.io` |

---

## Running tests

```bash
# All tests (live API tests skipped without a real key)
pytest tests/ -v

# With a real key — runs everything including the Claude call
ANTHROPIC_API_KEY=sk-ant-... pytest tests/ -v
```

---

## Cost breakdown

| Item | Cost |
|------|------|
| PDF download | Free |
| MJCS API lookup | Free |
| Claude Sonnet per walk sheet | ~$0.003 |
| 20-case run | ~$0.06 |
| 60-case run (max per session) | ~$0.18 |

