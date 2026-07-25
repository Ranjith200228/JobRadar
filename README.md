<div align="center">

<img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white"/>
<img src="https://img.shields.io/badge/Flask-3.0-000000?style=for-the-badge&logo=flask&logoColor=white"/>
<img src="https://img.shields.io/badge/Claude_AI-Anthropic-D97706?style=for-the-badge&logo=anthropic&logoColor=white"/>
<img src="https://img.shields.io/badge/Apify-Job_Scraping-00C4B4?style=for-the-badge&logo=apify&logoColor=white"/>
<img src="https://img.shields.io/badge/React-18-61DAFB?style=for-the-badge&logo=react&logoColor=black"/>

# 🎯 JobRadar

### AI-Powered Job Search & Resume Tailoring Platform

*Search jobs across every major board, get your resume tailored to each JD in seconds, and track your entire pipeline — all in one dark-mode app.*

</div>

---

## What It Does

JobRadar is a personal job search command center built with Flask + Claude AI. Paste a job description and it instantly generates a tailored ATS-optimized resume, professional cover letter, cold outreach email, and two LinkedIn messages — using only your real experience, never fabricated facts.

---

## Features

**Job Feed**
- Scrapes LinkedIn, Indeed, Glassdoor, Google Jobs, ZipRecruiter, Wellfound, and Dice simultaneously via Apify
- Smart 72-hour → 7-day auto-fallback so you always get results
- Auto-scores every fetched job against your CV using Claude AI

**Resume Builder**
- Paste any JD → get a pixel-perfect ATS-optimized resume PDF in your exact layout
- Professional cover letter PDF
- Cold outreach email written in your own voice — no AI-sounding phrases
- LinkedIn connection request note (300-char strict limit)
- LinkedIn outreach message (InMail-ready, 200+ words)
- Zero fabrication — Claude only reframes your real experience to match the role

**Job Tracker**
- Kanban board: Applied → Phone Screen → Interview → Offer → Rejected
- Drag cards across columns to track your pipeline
- Every job stores the match score, date posted, and source platform

**Dashboard**
- Live funnel chart showing your application pipeline
- Syncs in real time as you move cards

**Settings**
- Paste your Anthropic and Apify API keys directly in the UI — no terminal config needed
- Keys saved to a local `.env` file

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.10+, Flask 3.0, Flask-SQLAlchemy |
| AI | Anthropic Claude (claude-sonnet-4-6) |
| Job Scraping | Apify — openclawai/job-board-scraper, orgupdate/wellfound-jobs-scraper, shahidirfan/Dice-Job-Scraper |
| PDF Generation | ReportLab 4.0 — pixel-perfect resume & cover letter layout |
| PDF Parsing | PyMuPDF (fitz) |
| Frontend | React 18 (CDN), Tailwind CSS, Chart.js — single-file, no build step |
| Database | SQLite via SQLAlchemy |
| Auth / Config | python-dotenv |

---

## Getting Started

### Prerequisites

- Python 3.10+
- An [Anthropic API key](https://console.anthropic.com/)
- An [Apify API key](https://console.apify.com/account/integrations)

### Installation

```bash
# 1. Clone the repo
git clone https://github.com/Ranjith200228/JobRadar.git
cd JobRadar

# 2. Install dependencies
pip install -r requirements.txt

# 3. Add your API keys (two options)

# Option A — via the app UI (easiest)
#   Run the app, click the ⚙ icon top-right, paste your keys. Done.

# Option B — manual
cp .env.example .env
# Edit .env and fill in:
#   ANTHROPIC_API_KEY=sk-ant-...
#   APIFY_API_KEY=apify_api_...

# 4. Run
python app.py
```

Then open **http://localhost:5000** in your browser.

> **Windows shortcut:** double-click `START_JOBRADAR.bat`

---

## Usage Flow

```
1. Upload CV     →  drop your resume PDF — Claude parses it instantly
2. Job Feed      →  enter role + location, select platforms, hit Fetch
                    Claude auto-scores each result against your CV
3. Resume Builder → paste any JD → get resume PDF + cover letter + emails
4. Tracker       →  drag cards through your pipeline stages
5. Dashboard     →  watch your funnel stats update live
```

---

## Project Structure

```
JobRadar/
├── app.py              # Flask backend — all routes, AI calls, PDF generation
├── templates/
│   └── index.html      # React 18 single-page frontend
├── requirements.txt
├── .env.example        # API key template
├── HOW_TO_RUN.txt      # Quick-start guide
├── START_JOBRADAR.bat  # Windows one-click launcher
└── .gitignore          # Excludes db, keys, cache
```

---

## API Keys

| Key | Where to get it |
|-----|----------------|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com/) |
| `APIFY_API_KEY` | [console.apify.com → Integrations](https://console.apify.com/account/integrations) |

Both keys can be entered through the app's settings panel (⚙ icon) without touching any config files.

---

## Notes

- The SQLite database (`jobradar.db`) is created automatically on first run and is excluded from git
- To start fresh, delete `jobradar.db` and restart the server
- Job scraping calls are synchronous — large multi-platform searches can take 30–60 seconds
- All resume/cover letter PDFs are generated server-side and download directly from the browser

---

<div align="center">

Built by [Ranjith Kumar Maddirala](https://github.com/Ranjith200228)

*AI Engineer · curious to build things that matter*

</div>
