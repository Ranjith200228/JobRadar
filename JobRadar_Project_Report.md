# JobRadar — Complete End-to-End Technical Report
### Interview Preparation Document

---

## 1. What JobRadar Is (Your 30-Second Pitch)

JobRadar is a full-stack, AI-powered job search platform I designed and built from scratch. It solves the biggest frustration in job hunting: wasting hours scrolling irrelevant listings. A user uploads their resume once; JobRadar then scrapes live job postings from 8+ job boards across 20+ countries, uses a two-stage AI matching pipeline to score every listing against the user's actual resume, and shows only genuine matches — in under 60 seconds. Beyond search, it's a complete career toolkit: tailored resume generation, a drag-and-drop application tracker, skill-gap analysis, interview preparation, and an AI screening-call coach.

---

## 2. Technology Stack (and WHY each choice)

| Layer | Technology | Why I chose it |
|---|---|---|
| Backend | **Python + Flask** | Lightweight, fast to iterate, perfect for an API-first single-service app. No framework overhead like Django's ORM/admin when I only need REST endpoints. |
| Database | **SQLite via Flask-SQLAlchemy** | Zero-configuration, file-based, ideal for a single-server product. SQLAlchemy ORM gives me models, queries, and easy migration to Postgres later. |
| Frontend | **React 18 (CDN) + Babel Standalone** | Single-file architecture — no Node, no webpack, no build step. The entire UI is one `index.html` served by Flask. Deliberate tradeoff: instant deployment simplicity over build-time optimization. |
| Styling | **Custom CSS design system + Tailwind utility classes** | I built a "Spatial Depth" glassmorphism theme: deep navy canvas (#060c1a), frosted-glass panels (`backdrop-filter: blur(40px)`), layered box-shadows for 3D card depth, Space Grotesk typography. |
| AI | **Anthropic Claude API** — two models tiered by task | **Claude Haiku** for high-volume, low-cost scoring (each job = 1 tiny call, ~20 output tokens). **Claude Sonnet** for premium generation (resumes, cover letters, screening scripts). Tiering models by task is a key cost-engineering decision. |
| Job Scraping | **Apify platform actors** | Managed scraping infrastructure (proxies, anti-bot handling). Main actor: a multi-board scraper covering LinkedIn, Indeed, Glassdoor, Google Jobs, ZipRecruiter, Naukri, Bayt in ONE call. Dedicated actors for Wellfound and Dice. |
| PDF | **PyMuPDF (fitz)** for reading, **ReportLab** for generating | PyMuPDF extracts raw text from uploaded CVs; ReportLab renders the AI-generated tailored resumes/cover letters as downloadable PDFs. |
| Charts | **Chart.js** | Dashboard funnel + application-velocity charts. |
| Config | **python-dotenv** | API keys live in `.env`, editable from the in-app Settings panel which writes back to the file. |

---

## 3. Architecture Overview

```
Browser (React SPA, single index.html)
        │  fetch → JSON
        ▼
Flask REST API  (~32 endpoints, app.py)
        │
        ├── SQLite (jobradar.db) ── CVProfile, Job models
        ├── Anthropic API ── Haiku (scoring) / Sonnet (generation)
        └── Apify API ── job-board scraper actors
```

One Flask process serves both the static frontend (`/` returns index.html) and the JSON API (`/api/...`). The React app is a tab-based SPA: Upload CV, Job Feed, Tracker, Dashboard, Resume, Skill Gap, Interview, Screening. Each tab remounts on switch, refetching fresh data.

---

## 4. Data Layer

**Two core models:**

- **CVProfile** — full_name, current_job_title, years_of_experience, technical_skills (stored as JSON string), professional_summary, education/projects/certifications/experience (JSON strings), contact fields, LinkedIn/GitHub/portfolio URLs, `raw_text` (the entire original CV text — the source of truth for all AI features), `is_active` flag (multiple CV versions supported; exactly one active).

- **Job** — job_title, company_name, location, job_description, source_url, platform_name, date_posted, `match_score` (0-100, null = unscored), `status` (Saved → Applied → Screening → Technical Interview → Offer / Rejected), `status_updated_at`, notes.

**Schema evolution without a migration tool:** at boot, the app runs `PRAGMA table_info` and issues `ALTER TABLE ADD COLUMN` for any missing columns — so older databases upgrade themselves automatically. In an interview: "I implemented lightweight forward-only migrations at startup rather than pulling in Alembic for a single-file schema."

**Data freshness model:** every new browser session (detected via sessionStorage flag → `/api/session-start`) and every server boot wipes to a clean slate, so each visitor gets a brand-new experience. Reloading mid-session preserves data.

---

## 5. Resume Intake Pipeline

1. User drops a PDF (or pastes raw text).
2. **PyMuPDF** extracts text; page count is captured for adaptive resume generation later.
3. The raw text goes to **Claude** with a structured-extraction system prompt that returns strict JSON: name, title, years of experience, skills array, summary, education, projects, certifications, contact links.
4. The parsed profile is saved; re-uploading the same person+title **replaces** the old version (dedup), and the new profile becomes active.
5. The original `raw_text` is kept verbatim — every downstream AI feature (scoring, resume building, screening scripts) works from the real resume, not a lossy summary.

---

## 6. Job Scraping Engine (the hardest subsystem)

**Multi-board strategy.** Fifteen platform toggles in the UI map to three Apify actors:
- A multi-board actor (LinkedIn, Indeed, Glassdoor, Google Jobs, ZipRecruiter — plus Naukri and Bayt) — one API call scrapes many boards simultaneously, with both query phrasings OR-searched in a single run.
- Dedicated actors for Wellfound and Dice.

**International support.** A `COUNTRY_MAP` of 70+ city/country keywords detects the user's target country from the location string ("Hyderabad" → India) and passes the correct country parameter to Indeed/Glassdoor — I verified the exact accepted values against the actor's live input schema. Regional boards auto-attach: Naukri for India, Bayt for the UAE. Dice (US-only) is auto-skipped for international searches.

**Freshness filter.** The user picks a strict window — last 6, 24, or 48 hours — which is passed straight to the scraper's `hoursOld` parameter and never silently widened. Wellfound/Dice map to their closest *narrower* enum so the promise is kept.

**Query intelligence.** A rewrite table knows acronym ↔ full-form pairs (IAM ↔ Identity Access Management, M365 ↔ Microsoft 365, QA ↔ Quality Assurance…). Both phrasings are searched simultaneously because job boards match titles literally — the two forms return different listings.

**Performance.** Originally the fallback chain made up to 4 sequential scraper runs (5+ minutes). I collapsed it to one run (multi-term OR search), reduced per-board result counts, disabled LinkedIn's slow per-job description fetching, and capped every run with a 4-minute timeout. Live-measured result: **~17 seconds** for a 5-board scrape; full search-to-scored-results in 30-60 seconds.

**Resilience.**
- In-memory result cache (30-min TTL) keyed on query+location+platforms+window; thin result sets are never cached.
- Auto-rescue: if the user's selected boards return nothing relevant, the backend automatically re-searches ALL major boards and tells the user it did so.
- Scraper errors are collected and surfaced verbatim ("The job scraper hit a problem: …") instead of masquerading as "no jobs found."
- Billing/quota errors from Apify are detected by keyword and returned as actionable messages.

---

## 7. The AI Matching Pipeline (the core innovation)

**Stage 1 — Deterministic relevance gate (zero cost, runs first):**
- The search query is tokenized into **specific** words (windows, iam, python, 365) vs **generic** role words (engineer, administrator, manager) using a stop-word list and a generic-role dictionary.
- Specific tokens expand through a synonym/acronym map (365 → m365, o365, office 365…).
- Matching uses word-boundary regex with prefix semantics — "iam" matches "IAM Engineer" but never "William"; "admin" matches "Administrator."
- Rule: a job title must contain a specific token; a generic-only title match passes only if the description mentions a specific token (or has no description — benefit of the doubt, the LLM decides).
- This gate runs at **fetch time** (irrelevant jobs never even enter the feed) and again before scoring. It eliminates ~90% of off-target listings and saves ~40% of LLM calls.

**Stage 2 — LLM semantic scoring:**
- Each surviving job goes to **Claude Haiku** with a compact profile summary (~120 tokens: role, years, top skills — not the full resume, a deliberate token optimization) plus the job description and the user's search intent.
- The prompt enforces `{"score": <0-100>}` JSON only, temperature 0, ~20 max output tokens.
- Scoring rules baked into the system prompt: wrong role type → ≤30 regardless of skill overlap; matching role → 70-100 on skill fit; title-only jobs judged fairly without penalizing missing text.
- Calls run in a **ThreadPoolExecutor (4 workers)** with a lock-protected results dict — parallel scoring of dozens of jobs in seconds.

**Adaptive quality gate:** jobs ≥75% show by default; if nothing clears 75, the threshold degrades gracefully to 60, then 40 — the user always sees best matches, never a blank screen. The UI honestly labels the active threshold. If *every* scoring call fails (API key/credit problem), the app returns the real error instead of silently deleting everything.

---

## 8. AI Generation Features (Claude Sonnet)

- **Resume Builder** — takes the user's raw CV + a target job description and generates an ATS-optimized resume (adaptive to the original page count), cover letter, cold email, and LinkedIn messages; rendered to PDF with ReportLab.
- **Skill Gap Analyzer** — compares CV to JD: strong skills, missing skills, and a learning roadmap.
- **Interview Prep** — 10-12 likely questions (behavioral/technical/role-specific) with coaching hints, plus company research, salary estimates, follow-up email drafts, and rejection-pattern analysis.
- **Screening Call Coach** (flagship) — resume on the left, JD on the right → generates: a motivating confidence boost, a 60-second opening pitch, a requirement-by-requirement alignment map with verbatim "power lines," an 8-10 exchange mock screening call rendered as a chat (including a coached salary-question deflect-then-range strategy), questions to ask the recruiter, and reusable power phrases. The prompt forbids inventing experience — gaps are bridged honestly ("transferable strength + fast learner with proof").
- **Daily Challenge / Weekly Digest / AI Coach elements** — streaks, activity heatmap, weekly stats with dynamic tips.

**Structured output discipline:** every AI route uses a system prompt demanding raw JSON, extracts with regex, and parses through a **lenient JSON repairer** I wrote: it tracks bracket/string state character-by-character, closes unterminated strings, strips dangling fragments, and if needed chops back to the last complete element — so a truncated model response yields every salvageable section instead of an error. (Born from a real production bug: long screening scripts exceeded the max-token budget and arrived cut off.)

---

## 9. Frontend Engineering

- **Single-file React SPA** (~2,900 lines JSX) compiled in-browser by Babel Standalone — zero build tooling.
- **Design system:** "Spatial Depth" — glassmorphism panels, animated canvas particle background with connection lines, per-tab SVG hero scenes (procedurally seeded), gradient text, Space Grotesk font.
- **Key components:** JobCard with score ring + slide-over detail drawer (Esc/backdrop close, sticky Apply), drag-and-drop Kanban tracker with per-card notes, Dashboard with count-up animated stats + Chart.js funnel/velocity charts + streak/goal rings, Welcome Journey 3-step onboarding checklist, Settings modal with key management + live "Test Connections" validator + danger-zone reset, confetti on wins.
- **API client:** a tiny fetch wrapper with unified error handling — HTTP errors and network failures both surface as `{error}` objects; no silent failures.
- **UX safeguards:** live elapsed-seconds counter during scraping, platform-count warning ("select more boards"), location quick-chips, strict posted-within filter chips, honest result banners.

---

## 10. Reliability & Testing

- **Automated audit:** a 20-test end-to-end suite (run via Flask's test client with stubbed external APIs) covering the full user journey — session freshness, profile CRUD + dedup, fetch validation, gate behavior, scoring, quality gate, status transitions, tracker grouping, dashboard, digest fields, reset.
- **Relevance-gate eval suite:** 11+ ground-truth cases using real scraped job titles (verified against live API runs) — every filter change was regression-tested.
- **JSON repairer test matrix:** 6 corruption scenarios (mid-string, mid-key, 50%, 80%, markdown-wrapped).
- **Key validation endpoint:** `/api/test-keys` makes real micro-calls to both external APIs and reports per-key status in the UI.
- **JSX compile verification** via Babel on every change; Python AST parse checks.

---

## 11. Hard Problems I Solved (Your Interview War Stories)

1. **"Wrong jobs" bug** — scrapers returned loosely related jobs (a Python job for a "Windows Administrator" search) and pure CV-based scoring rated them highly. Root cause: scoring ignored search intent, and the keyword filter dropped short tokens like "IAM." Fix: the two-stage gate with acronym expansion + intent-weighted scoring prompt.
2. **International search returning nothing** — the scraper was hardcoded to `countryIndeed: "usa"`. I pulled the actor's live input schema, discovered the accepted country values AND that it supported Naukri/Bayt, and built country detection + regional board attachment. Verified with a live test: 20 real Hyderabad jobs.
3. **5-minute searches** — sequential fallback chains. Collapsed into one OR-query run; 17-second scrapes.
4. **Silent failures** — scraper errors, scoring API failures, and truncated LLM JSON all previously looked like "no jobs found." I built error collection + surfacing, total-failure detection (502 with the real reason), and the JSON repairer. Principle: *distinguish "no results" from "something broke," always.*
5. **Empty-screen problem** — a hard 75% cutoff sometimes deleted everything. Adaptive thresholds with honest UI labeling.

---

## 12. Likely Interview Questions & Strong Answers

**Q: Why Flask over Django/FastAPI?**
A: The app is a single service with ~32 JSON endpoints and one template — Flask's minimalism fit. FastAPI would've been equally valid; I'd choose it for async scraper calls if I scaled this. Django's batteries (admin, auth, ORM migrations) weren't needed for v1.

**Q: Why no frontend build system?**
A: Deliberate tradeoff. Babel-in-browser costs some initial parse time but gives one-file deployment, zero toolchain, and instant iteration. For production scale I'd precompile with Vite — the components are already structured for that migration.

**Q: How do you keep LLM costs down?**
A: Three ways: model tiering (Haiku for scoring at ~20 output tokens/call, Sonnet only for premium generation), a free deterministic pre-filter that eliminates ~40% of scoring calls, and compact context (a 120-token profile summary instead of the full resume for scoring). Plus response caching on searches.

**Q: How does matching work?**
A: Retrieval-then-rerank: cheap deterministic filtering (tokenization, synonym expansion, word-boundary matching, specific-vs-generic rules) followed by parallel LLM semantic scoring with intent weighting and an adaptive threshold. Same architecture pattern as production recommendation/RAG systems.

**Q: What would you do differently at scale?**
A: Postgres over SQLite; a job queue (Celery/RQ) for scrapes instead of request-blocking; precompiled frontend; per-user auth with persistent data (designed, planned next); webhook-based scraper completion instead of polling; and structured evals with a larger labeled dataset for the matching pipeline.

**Q: Biggest lesson?**
A: External systems fail in silent, weird ways — the majority of my debugging was making failures *visible*: schema-verifying third-party inputs, surfacing real errors to users, and building graceful degradation so the product never shows a dead end.

---

*Built solo, end to end: product design, data modeling, API design, AI pipeline engineering, scraping orchestration, UI/UX design, testing, and reliability engineering.*
