# -*- coding: utf-8 -*-
import os
import json
import threading
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, request, jsonify, send_from_directory, Response
from flask_sqlalchemy import SQLAlchemy
import fitz  # PyMuPDF
from anthropic import Anthropic
from apify_client import ApifyClient
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobradar.db")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{_DB_PATH}"
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"connect_args": {"check_same_thread": False}}
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

db = SQLAlchemy(app)


# ── Models ────────────────────────────────────────────────────────────────────

class CVProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(256))
    current_job_title = db.Column(db.String(256))
    years_of_experience = db.Column(db.Float)
    technical_skills = db.Column(db.Text)    # JSON list[str]
    professional_summary = db.Column(db.Text)
    education_text = db.Column(db.Text)      # JSON list[str]
    projects_text = db.Column(db.Text)       # JSON list[dict]
    certifications_text = db.Column(db.Text) # JSON list[str]
    experience_text = db.Column(db.Text)     # JSON list[dict]
    # Contact / social links
    contact_email = db.Column(db.String(256))
    phone = db.Column(db.String(64))
    linkedin_url = db.Column(db.String(512))
    github_url = db.Column(db.String(512))
    portfolio_url = db.Column(db.String(512))
    location_text = db.Column(db.String(256))
    raw_text = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "full_name": self.full_name,
            "current_job_title": self.current_job_title,
            "years_of_experience": self.years_of_experience,
            "technical_skills": json.loads(self.technical_skills or "[]"),
            "professional_summary": self.professional_summary,
            "education": json.loads(self.education_text or "[]"),
            "projects": json.loads(self.projects_text or "[]"),
            "certifications": json.loads(self.certifications_text or "[]"),
            "experience": json.loads(self.experience_text or "[]"),
            "contact_email": self.contact_email or "",
            "phone": self.phone or "",
            "linkedin_url": self.linkedin_url or "",
            "github_url": self.github_url or "",
            "portfolio_url": self.portfolio_url or "",
            "location_text": self.location_text or "",
            "created_at": self.created_at.isoformat(),
        }


class Job(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    job_title = db.Column(db.String(512))
    company_name = db.Column(db.String(256))
    location = db.Column(db.String(256))
    job_description = db.Column(db.Text)
    source_url = db.Column(db.String(1024))
    platform_name = db.Column(db.String(64))
    date_posted = db.Column(db.String(32), nullable=True)
    match_score = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(64), default="Saved")
    status_updated_at = db.Column(db.DateTime, default=dt.datetime.utcnow)
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "job_title": self.job_title,
            "company_name": self.company_name,
            "location": self.location,
            "job_description": self.job_description,
            "source_url": self.source_url,
            "platform_name": self.platform_name,
            "date_posted": self.date_posted,
            "match_score": self.match_score,
            "status": self.status,
            "status_updated_at": self.status_updated_at.isoformat(),
            "created_at": self.created_at.isoformat(),
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_anthropic():
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise ValueError("Anthropic API key not set. Open Settings (gear icon) and paste your key.")
    return Anthropic(api_key=key)


def get_apify():
    key = os.environ.get("APIFY_API_KEY", "").strip()
    if not key:
        raise ValueError("Apify API key not set. Open Settings (gear icon) and paste your key.")
    return ApifyClient(key)


def extract_pdf_text(file_bytes: bytes) -> str:
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    return "\n".join(page.get_text() for page in doc).strip()


def _clean_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _flatten_to_strings(lst: list) -> list:
    """Ensure a list contains only plain strings, never dicts/objects."""
    result = []
    for item in lst:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            parts = [str(v) for v in item.values() if v]
            result.append(", ".join(parts))
    return result


CV_PARSE_SYSTEM = """You are an expert CV/resume parser. Extract EVERY piece of information including contact details and social links. Return ONLY a valid raw JSON object — no markdown fences, no explanation, nothing else.

JSON structure:
{
  "full_name": "string",
  "current_job_title": "string",
  "years_of_experience": number,
  "professional_summary": "full summary paragraph as written",
  "contact_email": "email address or empty string",
  "phone": "phone number or empty string",
  "location": "city, state/country or empty string",
  "linkedin_url": "full LinkedIn URL (e.g. https://linkedin.com/in/username) or empty string",
  "github_url": "full GitHub URL (e.g. https://github.com/username) or empty string",
  "portfolio_url": "personal website / portfolio URL or empty string",
  "technical_skills": ["skill1", "skill2"],
  "skills": [
    {"category": "Programming Languages", "items": "Python, SQL, TypeScript"},
    {"category": "GenAI & LLMs", "items": "RAG, FAISS, ..."}
  ],
  "experience": [
    {
      "title": "exact job title",
      "company": "exact company name and location",
      "dates": "exact date range",
      "bullets": ["exact bullet 1", "exact bullet 2"]
    }
  ],
  "projects": [
    {
      "name": "project name",
      "technologies": ["tech1", "tech2"],
      "bullets": ["what was done, metrics, outcome"]
    }
  ],
  "education": [
    {
      "degree": "Degree title - GPA: X.XX/X.X",
      "institution": "University Name - City, State",
      "dates": "Mon YYYY - Mon YYYY"
    }
  ],
  "certifications": ["Certification name — plain string"]
}

CRITICAL:
- Extract skills as CATEGORIZED groups exactly as they appear in the resume (Programming Languages, GenAI & LLMs, etc.)
- Education MUST be structured objects with degree, institution, dates — NOT plain strings
- For linkedin_url, github_url, portfolio_url: extract full URL. If only username shown, prepend https://
- certifications MUST be plain strings
- If a field is not found, use empty string "" for strings, [] for arrays"""


def parse_cv(raw_text: str) -> dict:
    client = get_anthropic()
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8192,
        temperature=0,
        system=CV_PARSE_SYSTEM,
        messages=[{"role": "user", "content": raw_text}],
    )
    return json.loads(_clean_json(msg.content[0].text))


def _normalise_url(url: str) -> str:
    """Ensure URL has https:// prefix."""
    if not url:
        return ""
    url = url.strip()
    if url and not url.startswith("http"):
        url = "https://" + url
    return url


def _save_profile(parsed: dict, raw_text: str) -> CVProfile:
    CVProfile.query.delete()
    profile = CVProfile(
        full_name=parsed.get("full_name", ""),
        current_job_title=parsed.get("current_job_title", ""),
        years_of_experience=float(parsed.get("years_of_experience") or 0),
        technical_skills=json.dumps(parsed.get("technical_skills", [])),
        professional_summary=parsed.get("professional_summary", ""),
        education_text=json.dumps(_flatten_to_strings(parsed.get("education", []))),
        projects_text=json.dumps(parsed.get("projects", [])),
        certifications_text=json.dumps(_flatten_to_strings(parsed.get("certifications", []))),
        experience_text=json.dumps(parsed.get("experience", [])),
        contact_email=parsed.get("contact_email", ""),
        phone=parsed.get("phone", ""),
        linkedin_url=_normalise_url(parsed.get("linkedin_url", "")),
        github_url=_normalise_url(parsed.get("github_url", "")),
        portfolio_url=_normalise_url(parsed.get("portfolio_url", "")),
        location_text=parsed.get("location", ""),
        raw_text=raw_text,
    )
    db.session.add(profile)
    db.session.commit()
    return profile


def score_job(profile_dict: dict, job_description: str) -> int:
    client = get_anthropic()
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        temperature=0,
        system="Return a JSON object {\"score\": <integer 0-100>} representing how well this candidate matches the job. Return only JSON, no explanation.",
        messages=[{"role": "user", "content":
            f"Candidate: {json.dumps(profile_dict)}\n\nJob: {job_description[:1500]}"}],
    )
    return json.loads(_clean_json(msg.content[0].text))["score"]


# ── Apify / Job fetching ──────────────────────────────────────────────────────

ACTOR_MULTI_BOARD = "openclawai/job-board-scraper"
ACTOR_WELLFOUND   = "orgupdate/wellfound-jobs-scraper"
ACTOR_DICE        = "shahidirfan/Dice-Job-Scraper"

MULTI_BOARD_MAP = {
    "LinkedIn":         "linkedin",
    "Indeed":           "indeed",
    "Glassdoor":        "glassdoor",
    "Google Jobs":      "google",
    "ZipRecruiter":     "zip_recruiter",
    "Monster":          "indeed",
    "SimplyHired":      "indeed",
    "CareerBuilder":    "indeed",
    "Handshake":        "google",
    "Upwork":           "google",
    "We Work Remotely": "google",
    "Greenhouse":       "google",
    "Lever":            "google",
}
DEDICATED_ACTORS = {"Wellfound", "Dice"}


def _get_dataset_id(run):
    return run["defaultDatasetId"] if isinstance(run, dict) else run.default_dataset_id


def _normalise(item: dict, platform: str = "") -> dict:
    url = (item.get("job_url_direct") or item.get("job_url") or
           item.get("jobUrl") or item.get("applyUrl") or
           item.get("url") or item.get("link") or "")
    return {
        "job_title":       (item.get("title") or item.get("job_title") or item.get("position") or "").strip(),
        "company_name":    (item.get("company") or item.get("company_name") or item.get("companyName") or "").strip(),
        "location":        (item.get("location") or item.get("jobLocation") or "").strip(),
        "job_description": (item.get("description") or item.get("job_description") or "").strip(),
        "source_url":      url.strip(),
        "platform_name":   platform or item.get("site") or item.get("source") or "Job Board",
        "date_posted":     item.get("date_posted") or item.get("datePosted") or "",
    }


def _run_actor(apify, actor_id: str, run_input: dict) -> list:
    try:
        run   = apify.actor(actor_id).call(run_input=run_input)
        items = list(apify.dataset(_get_dataset_id(run)).iterate_items())
        print(f"[Apify:{actor_id.split('/')[-1]}] {len(items)} results")
        return items
    except Exception as e:
        print(f"[Apify:{actor_id.split('/')[-1]}] ERROR: {e}")
        return []


def _run_multi_board(apify, query, location, site_map, hours_old):
    """Run the multi-board actor and return normalised results."""
    items = _run_actor(apify, ACTOR_MULTI_BOARD, {
        "searchTerm":               query,
        "location":                 location,
        "sites":                    list(site_map.keys()),
        "maxResults":               15,
        "hoursOld":                 hours_old,
        "descriptionFormat":        "markdown",
        "linkedinFetchDescription": True,
        "countryIndeed":            "usa",
    })
    out = []
    for item in items:
        site  = item.get("site", "")
        label = site_map.get(site, site or "Job Board")
        out.append(_normalise(item, label))
    return out


def fetch_jobs(query: str, location: str, platforms: list) -> list:
    apify    = get_apify()
    results  = []
    selected = set(platforms)

    # Multi-board platforms — try 72 h first, fall back to 7 days if empty
    multi = selected - DEDICATED_ACTORS
    if multi:
        site_map: dict[str, str] = {}
        for p in multi:
            site = MULTI_BOARD_MAP.get(p, "indeed")
            if site not in site_map:
                site_map[site] = p

        mb_results = _run_multi_board(apify, query, location, site_map, hours_old=72)
        if not mb_results:
            print("[JobFetch] 72h returned 0 results — retrying with 7-day window")
            mb_results = _run_multi_board(apify, query, location, site_map, hours_old=168)
        results.extend(mb_results)

    # Wellfound
    if "Wellfound" in selected:
        city = location.split(",")[0].strip() if location else ""
        items = _run_actor(apify, ACTOR_WELLFOUND, {
            "countryName":    "United States",
            "locationName":   city,
            "includeKeyword": query,
            "pagesToFetch":   1,
            "datePosted":     "3days",
        })
        for item in items:
            results.append(_normalise(item, "Wellfound"))

    # Dice
    if "Dice" in selected:
        items = _run_actor(apify, ACTOR_DICE, {
            "keyword":        query,
            "location":       location,
            "posted_date":    "3d",
            "results_wanted": 15,
        })
        for item in items:
            results.append(_normalise(item, "Dice"))

    return results


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"),
        "index.html"
    )


# CV routes
@app.route("/api/profile")
def get_profile():
    p = CVProfile.query.order_by(CVProfile.created_at.desc()).first()
    return jsonify({"profile": p.to_dict() if p else None})


@app.route("/api/upload-cv", methods=["POST"])
def upload_cv():
    if "file" not in request.files:
        return jsonify({"error": "No file attached."}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are accepted."}), 400
    try:
        raw = extract_pdf_text(f.read())
        if not raw:
            return jsonify({"error": "Could not extract text. Make sure it's not a scanned image PDF."}), 400
        parsed  = parse_cv(raw)
        profile = _save_profile(parsed, raw)
        return jsonify({"profile": profile.to_dict()})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except json.JSONDecodeError:
        return jsonify({"error": "Parsing failed — unexpected Claude response. Try again."}), 500
    except Exception as e:
        return jsonify({"error": f"Error: {e}"}), 500


@app.route("/api/profile/links", methods=["POST"])
def update_profile_links():
    """Save/update LinkedIn, GitHub, portfolio, email, phone manually."""
    profile = CVProfile.query.order_by(CVProfile.created_at.desc()).first()
    if not profile:
        return jsonify({"error": "Upload your CV first."}), 400
    data = request.json or {}
    if data.get("linkedin_url") is not None:
        profile.linkedin_url  = _normalise_url(data["linkedin_url"])
    if data.get("github_url") is not None:
        profile.github_url    = _normalise_url(data["github_url"])
    if data.get("portfolio_url") is not None:
        profile.portfolio_url = _normalise_url(data["portfolio_url"])
    if data.get("contact_email") is not None:
        profile.contact_email = data["contact_email"]
    if data.get("phone") is not None:
        profile.phone         = data["phone"]
    if data.get("location_text") is not None:
        profile.location_text = data["location_text"]
    db.session.commit()
    return jsonify({"profile": profile.to_dict()})


@app.route("/api/upload-cv-text", methods=["POST"])
def upload_cv_text():
    data = request.json or {}
    raw  = (data.get("text") or "").strip()
    if len(raw) < 50:
        return jsonify({"error": "Please paste your full CV text."}), 400
    try:
        parsed  = parse_cv(raw)
        profile = _save_profile(parsed, raw)
        return jsonify({"profile": profile.to_dict()})
    except Exception as e:
        return jsonify({"error": f"Parsing failed: {e}"}), 500


# Job routes
@app.route("/api/jobs")
def get_jobs():
    jobs = Job.query.order_by(Job.created_at.desc()).all()
    return jsonify({"jobs": [j.to_dict() for j in jobs]})


@app.route("/api/jobs/fetch", methods=["POST"])
def fetch_jobs_route():
    data      = request.json or {}
    query     = (data.get("query") or "").strip()
    location  = (data.get("location") or "").strip()
    platforms = data.get("platforms") or ["LinkedIn", "Indeed"]

    if not query:
        return jsonify({"error": "Enter a job title to search."}), 400
    try:
        raw = fetch_jobs(query, location, platforms)
        if not raw:
            return jsonify({"error": "No jobs found in the last 7 days for this search. Try different keywords or broaden your platforms."}), 404

        # Wipe old unsaved jobs — every search starts fresh
        Job.query.filter_by(status="Saved").delete()
        db.session.commit()

        saved = []
        for jd in raw:
            if not jd["job_title"]:
                continue
            job = Job(
                job_title=jd["job_title"],
                company_name=jd["company_name"],
                location=jd["location"] or location,
                job_description=jd["job_description"],
                source_url=jd["source_url"],
                platform_name=jd["platform_name"],
                date_posted=jd.get("date_posted", ""),
            )
            db.session.add(job)
            saved.append(job)
        db.session.commit()
        return jsonify({"jobs": [j.to_dict() for j in saved], "count": len(saved)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Fetch failed: {e}"}), 500


@app.route("/api/jobs/score-all", methods=["POST"])
def score_all():
    profile = CVProfile.query.order_by(CVProfile.created_at.desc()).first()
    if not profile:
        return jsonify({"error": "Upload your CV first."}), 400

    unscored = Job.query.filter(Job.match_score.is_(None)).all()
    if not unscored:
        return jsonify({"jobs": [j.to_dict() for j in Job.query.order_by(Job.created_at.desc()).all()]})

    pdict = profile.to_dict()
    scores: dict[int, int] = {}
    lock = threading.Lock()

    def _score(job):
        try:
            s = score_job(pdict, job.job_description or job.job_title or "")
            with lock:
                scores[job.id] = s
        except Exception as e:
            print(f"[score] {job.id}: {e}")

    with ThreadPoolExecutor(max_workers=5) as pool:
        for f in as_completed([pool.submit(_score, j) for j in unscored]):
            pass

    for job in unscored:
        if job.id in scores:
            job.match_score = scores[job.id]
    db.session.commit()
    return jsonify({"jobs": [j.to_dict() for j in Job.query.order_by(Job.created_at.desc()).all()]})


@app.route("/api/jobs/<int:job_id>/apply", methods=["POST"])
def apply_job(job_id):
    job = db.session.get(Job, job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    job.status = "Applied"
    job.status_updated_at = dt.datetime.utcnow()
    db.session.commit()
    return jsonify({"job": job.to_dict()})


@app.route("/api/jobs/<int:job_id>/status", methods=["PATCH"])
def update_status(job_id):
    job = db.session.get(Job, job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    valid = ["Saved", "Applied", "Screening", "Technical Interview", "Offer", "Rejected"]
    new_status = (request.json or {}).get("status")
    if new_status not in valid:
        return jsonify({"error": f"Invalid status"}), 400
    job.status = new_status
    job.status_updated_at = dt.datetime.utcnow()
    db.session.commit()
    return jsonify({"job": job.to_dict()})


@app.route("/api/tracker")
def get_tracker():
    cols = ["Saved", "Applied", "Screening", "Technical Interview", "Offer", "Rejected"]
    return jsonify({"tracker": {
        c: [j.to_dict() for j in Job.query.filter_by(status=c).order_by(Job.status_updated_at.desc()).all()]
        for c in cols
    }})


@app.route("/api/dashboard")
def get_dashboard():
    all_jobs = Job.query.all()
    applied  = ["Applied", "Screening", "Technical Interview", "Offer", "Rejected"]
    beyond   = ["Screening", "Technical Interview", "Offer", "Rejected"]
    total    = sum(1 for j in all_jobs if j.status in applied)
    adv      = sum(1 for j in all_jobs if j.status in beyond)
    scored   = [j.match_score for j in all_jobs if j.status in applied and j.match_score is not None]
    return jsonify({
        "total_applications": total,
        "response_rate":      round(adv / total * 100, 1) if total else 0,
        "avg_match_score":    round(sum(scored) / len(scored), 1) if scored else 0,
        "funnel":             {s: sum(1 for j in all_jobs if j.status == s) for s in applied},
    })


@app.route("/api/settings")
def get_settings():
    return jsonify({
        "anthropic_key_set": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
        "apify_key_set":     bool(os.environ.get("APIFY_API_KEY", "").strip()),
    })


@app.route("/api/settings", methods=["POST"])
def save_settings():
    data     = request.json or {}
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    lines    = open(env_path).readlines() if os.path.exists(env_path) else []

    def upsert(lines, key, val):
        found, out = False, []
        for l in lines:
            if l.startswith(f"{key}="):
                out.append(f"{key}={val}\n"); found = True
            else:
                out.append(l)
        if not found:
            out.append(f"{key}={val}\n")
        return out

    if data.get("anthropic_key"):
        os.environ["ANTHROPIC_API_KEY"] = data["anthropic_key"]
        lines = upsert(lines, "ANTHROPIC_API_KEY", data["anthropic_key"])
    if data.get("apify_key"):
        os.environ["APIFY_API_KEY"] = data["apify_key"]
        lines = upsert(lines, "APIFY_API_KEY", data["apify_key"])
    open(env_path, "w").writelines(lines)
    return jsonify({"success": True, "message": "Keys saved successfully."})


# ── Resume Builder ────────────────────────────────────────────────────────────

RESUME_SYSTEM = """You are a master resume writer — not an AI, but a human expert who has spent 20+ years crafting resumes that get interviews at top companies. You write the way the best human resume writers do: punchy, varied, specific, and natural.

YOUR PROCESS:
STEP 1 — Read the job description deeply:
  • Extract the top 8-10 must-have technical keywords the ATS will scan for
  • Note the seniority, domain focus, and specific tools/frameworks required

STEP 2 — Map JD requirements to the original resume:
  • Find where each required skill already exists in the resume (even under a different name)
  • Identify which bullets can naturally absorb JD keywords without sounding forced

STEP 3 — Rewrite bullets to sound human-crafted:
  • Reorder bullets per role — most JD-relevant first
  • Rephrase using JD keywords where the work genuinely matches
  • Vary sentence structure, length, and opening verbs — no two bullets should start the same way
  • Lead with impact/outcome when possible, not just the action

WRITING RULES — these make the difference between AI-sounding and human-sounding:

BANNED PATTERNS (these scream AI — never use them):
  ✗ Do NOT start multiple bullets with the same verb ("Built X", "Built Y", "Built Z")
  ✗ Do NOT end bullets with "demonstrating X" or "showcasing Y" or "enabling Z"
  ✗ Do NOT use these words: leveraging, utilizing, spearheading, orchestrating, streamlining, synergizing, robust, scalable (unless quoting the JD directly)
  ✗ Do NOT write "X using Y to achieve Z" as a formula — vary the structure
  ✗ Do NOT use em dashes (—) anywhere — use a regular hyphen (-) instead always
  ✗ Do NOT write long run-on bullets that chain too many clauses
  ✗ Do NOT add filler qualifiers like "rigorous", "iterative", "research-driven" unless they were in the original
  ✗ Do NOT write the same structure twice in a row

HUMAN PATTERNS (what skilled resume writers actually do):
  ✓ Use varied, specific action verbs: built, shipped, trained, diagnosed, cut, improved, deployed, reduced, led, ran, wrote, designed, debugged, integrated
  ✓ Keep bullets tight — 1-2 lines max. If you need 3 lines, split into two bullets
  ✓ Lead with the result when impactful: "Cut manual audit effort 20-30% by shipping an Isolation Forest pipeline over ~1M billing records"
  ✓ Use numbers and specifics: exact metrics, dataset sizes, benchmark names
  ✓ Mix short punchy bullets with slightly more detailed ones for variety
  ✓ The summary should sound like the candidate wrote it about themselves — confident, direct, not fluffy

ABSOLUTE RULES — never break these:
1. NEVER invent, fabricate, or add ANY company, project, skill, metric, tool, or achievement not in the original resume
2. NEVER change company names, job titles, or date ranges — keep EXACTLY as written
3. NEVER change metrics — preserve EXACTLY as written (e.g. "20-30%", "15-20%", "50K-100K rows")
4. ONLY rephrase bullets to use JD keywords when the underlying work genuinely matches
5. Include EVERY section from the original resume

Output ONLY a valid raw JSON object with NO markdown fences, NO explanation, NO extra text:
{
  "full_name": "exact from original",
  "title": "exact from original",
  "summary": "3-4 sentence ATS-optimized summary using JD keywords, grounded ONLY in real resume facts",
  "skills": [
    {"category": "EXACT category name from original", "items": "reordered skills — put JD keywords first, never add skills not in original"}
  ],
  "experience": [
    {
      "title": "EXACT original job title",
      "company": "EXACT original company name and location",
      "dates": "EXACT original dates",
      "bullets": ["reframed with JD keywords but never fabricated — preserve ALL metrics exactly"]
    }
  ],
  "projects": [
    {
      "name": "EXACT original project name",
      "technologies": ["exact technologies from original as a flat list"],
      "bullets": ["reframed with JD keywords but never fabricated — preserve ALL metrics exactly"]
    }
  ],
  "education": [
    {
      "degree": "EXACT degree title and GPA as written",
      "institution": "EXACT university name and location",
      "dates": "EXACT dates"
    }
  ],
  "certifications": ["EXACT certification as plain string"],
  "ats_keywords_added": ["list of exact JD keywords woven into existing content"]
}"""


COVER_SYSTEM = """You are the chief cover letter specialist at a top executive recruiting firm. You write cover letters that get interviews.

CRITICAL VOICE RULE: Write in FIRST PERSON throughout. Use "I", "my", "me" — NEVER "he", "she", "Ranjith", or any third-person reference. The letter is written BY the candidate TO the hiring manager.

WRONG: "Ranjith brings three years of experience..."
RIGHT: "I bring three years of experience..."

WRONG: "He built a forecasting system..."
RIGHT: "I built a forecasting system..."

STRUCTURE (exactly 4 short paragraphs — must fit on ONE page):
P1 — OPENING (3-4 sentences): State the specific role I am applying for. Give 1 concrete, specific reason I'm a strong fit based on my real background.
P2 — CORE VALUE (4-5 sentences): Highlight my 2 most relevant real achievements with specifics — real company names, real metrics, real technologies from my resume.
P3 — ALIGNMENT (3-4 sentences): Connect my background directly to the company's specific needs from the JD. Show I understand what they need.
P4 — CLOSE (2-3 sentences): Confident, direct call to action. Express readiness to discuss. Thank them briefly.

RULES:
- Write in FIRST PERSON always — "I", "my", "me"
- Use ONLY real facts from the candidate's resume — zero fabrication
- No filler phrases: no "I am excited to apply", no "dynamic team", no "I believe I would be a great fit"
- Do NOT write "Dear Hiring Manager" or "Sincerely" — the PDF template adds those
- Keep each paragraph SHORT — this must fit on one page
- Output only the 4 paragraph body separated by blank lines, nothing else"""


EMAIL_SYSTEM = """You are Ranjith Maddirala writing a cold outreach email in your own voice. This email should read exactly as if YOU typed it yourself — not as if a recruiter or coach wrote it for you. The reader should feel they are hearing directly from a curious, driven engineer who genuinely wants to contribute, not receiving a templated pitch.

FORMAT:
Line 1: Subject: [specific, intriguing subject line — reference the role or company directly]
Line 2: blank
Lines 3+: email body (100–130 words)

HOW TO WRITE THIS:
- Open with one sentence that shows you've actually thought about THIS company or role — a specific observation, a product they ship, a problem they solve. Not "I saw your job posting."
- In 2–3 sentences, connect your real background to something specific they need. Use concrete things: technologies you've built with, systems you've shipped, results you've driven. Don't list skills — tell a micro-story.
- Show your hunger: one sentence about why THIS problem space or company genuinely excites you. Be specific — what are you curious to learn or build there? Let your energy come through naturally, not through adjectives like "passionate" or "excited."
- End with a direct, low-friction ask — either a 15-minute call or a referral if they're internal. Make it easy to say yes.

VOICE RULES:
- Write in first person: "I", "my", "me" always
- Sound like a real human sent this from their laptop at 9pm — not a cover letter, not a PR pitch
- No corporate filler: no "hope this finds you well", "I am writing to express interest", "dynamic team", "I believe I would be a great fit"
- No em dashes, no bullet points, no headers in the body
- Use only real, verifiable facts from the candidate's resume — zero fabrication
- The tone is: confident but humble, direct but warm, specific but concise

Output only the email (subject line + body). Nothing else."""


LINKEDIN_CONNECT_SYSTEM = """You are Ranjith Maddirala writing your own LinkedIn connection request. This is the short note sent WITH a connection request — strict 300-character maximum (LinkedIn enforces this). Every character counts.

HOW TO WRITE IT:
- Open by referencing something specific: the company, the role, or a genuine reason you're reaching out
- In 1–2 sentences, say who you are and why this connection makes sense — one concrete detail from your real background that's relevant to them
- End with a soft, natural ask — not "let's synergize", just something a real person would say
- Sound like a curious, ambitious engineer, not a job-hunting robot

RULES:
- Strict 300-character maximum — count characters carefully, do NOT exceed
- First person always: "I", "my", "me"
- No "I am excited to connect", no "I am passionate about", no buzzword filler
- No em dashes
- Use only real facts from the candidate's resume
- Output ONLY the note text — no labels, no quotes, nothing else"""


LINKEDIN_MSG_SYSTEM = """You are Ranjith Maddirala writing a LinkedIn outreach message to a hiring manager, recruiter, or team lead at a company you genuinely want to work at. This is longer than a connection note — it's a full message (180–220 words) you'd send via InMail or after connecting.

HOW TO WRITE IT:
- Open with something that shows you've done your homework — a product, a challenge they're solving, a recent engineering blog post, or something specific in the JD that caught your attention. Be specific enough that it couldn't be a copy-paste to 50 other people.
- In the middle, tell them about yourself through your work — not a resume dump, but 2–3 real things you've built or solved that map directly to what they're working on. Reference actual technologies, outcomes, scale. Let the specificity do the selling.
- Show your hunger to learn and build: one genuine sentence about what draws you to this problem space, what you're curious to explore or build at their company. This should feel personal — the kind of thing you'd say in a coffee chat, not a job application.
- If they're internal, ask naturally if they'd be open to a referral or sharing a bit about the team. If external, ask for a 15-minute call or to pass along your resume.
- Close warmly but briefly.

VOICE RULES:
- Write in first person: "I", "my", "me" always
- Tone: genuine, thoughtful, curious — like a real email from a sharp engineer, not a pitch
- No em dashes, no bullet points
- No filler: "I am excited", "I believe I would be a great fit", "hope this finds you well", "passionate about", "synergy"
- Use only verifiable facts from the candidate's resume
- Output ONLY the message body — no subject line, no labels, nothing else"""


def _sanitize(text: str) -> str:
    """Escape XML special chars for ReportLab Paragraph. Also converts em/en dashes to hyphen."""
    if not isinstance(text, str):
        text = str(text)
    text = text.replace("—", "-").replace("–", "-")  # — and –
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _generate_resume_content(raw_cv_text: str, jd: str) -> dict:
    client = get_anthropic()
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        temperature=0.15,   # slight variation = natural language, not robotic
        system=RESUME_SYSTEM,
        messages=[{"role": "user", "content":
            f"=== ORIGINAL RESUME — use ONLY this content, do NOT invent anything ===\n\n{raw_cv_text}\n\n"
            f"=== JOB DESCRIPTION — deeply analyze and optimize the resume to match this ===\n\n{jd}"}],
    )
    data = json.loads(_clean_json(msg.content[0].text))
    # certifications stay as plain strings
    data["certifications"] = _flatten_to_strings(data.get("certifications", []))
    # education must stay as structured dicts — do NOT flatten
    # If Claude returned flat strings, try to keep them as-is (handled in PDF renderer)
    return data


def _generate_text(system: str, prompt: str, max_tokens: int = 1500, model: str = "claude-sonnet-4-6") -> str:
    client = get_anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0.2,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _build_resume_pdf(resume_data: dict) -> bytes:
    """
    Pixel-perfect match to Ranjith's original resume template.
    Colors extracted directly from the original PDF via PyMuPDF:
      - Name & section headers: #1F4E79 (dark navy blue), NOT green
      - Contact link color:     #0563C1 (blue, clickable)
      - Body text:              #000000 (black)
      - Project tech text:      #333333 (dark gray, italic)
      - HR rule under sections: #1F4E79 (navy)
    Font sizes from original PDF:
      - Name: 17pt Bold
      - Section headers: 11pt Bold
      - Body / bullets: 10pt
      - Job title / Project name / Degree: 10.5pt Bold
      - Dates: 10pt Bold (right-aligned)
      - Skills category: 10pt Bold Black
      - Project tech: 9.5pt Italic #333333
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate,
                                    Paragraph, Spacer,
                                    HRFlowable, Table, TableStyle, KeepTogether)
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase import pdfmetrics
    from io import BytesIO

    buf = BytesIO()
    W, H = letter
    L = R = 0.5 * inch          # 0.5" margins match original
    TM = BM = 0.14 * inch
    CONTENT = W - L - R         # 7.5 inches

    # Use BaseDocTemplate + explicit Frame with zero internal padding so that
    # free paragraphs and Table content both start at the same x (leftMargin).
    # SimpleDocTemplate uses Frame(leftPadding=6) by default, which shifts
    # paragraphs 6pt right of tables — causing misalignment.
    frame = Frame(L, BM, CONTENT, H - TM - BM,
                  leftPadding=0, rightPadding=0,
                  topPadding=0, bottomPadding=0)
    doc = BaseDocTemplate(buf, pagesize=letter,
                          pageTemplates=[PageTemplate(id='normal', frames=[frame])])

    # ── Exact colors from original PDF ────────────────────────────────────────
    NAVY    = HexColor("#1F4E79")   # name + section headers (extracted from PDF)
    BLACK   = HexColor("#000000")   # body text
    LINK    = HexColor("#0563C1")   # contact hyperlinks (extracted from PDF)
    SEP     = HexColor("#333333")   # | separators + project tech text
    RULE    = HexColor("#1F4E79")   # HR rule under section headers

    # ── Font sizes / leading calibrated to match original's single-page layout ─
    # Original measured: ~11.5pt leading for bullets, 12.5pt for skills, 13pt for headers
    name_s    = ParagraphStyle("N",  fontName="Helvetica-Bold", fontSize=17,
                               textColor=NAVY,  alignment=1, spaceAfter=0, leading=19)
    contact_s = ParagraphStyle("C",  fontName="Helvetica",      fontSize=8,
                               textColor=BLACK, alignment=1, spaceAfter=0, leading=10)
    section_s = ParagraphStyle("S",  fontName="Helvetica-Bold", fontSize=11,
                               textColor=NAVY,  spaceBefore=0, spaceAfter=0, leading=13)
    body_s    = ParagraphStyle("B",  fontName="Helvetica",      fontSize=10,
                               textColor=BLACK, leading=10.5, spaceAfter=0)
    # Skills: tight leading, same 10pt as original
    cat_s     = ParagraphStyle("Ca", fontName="Helvetica-Bold", fontSize=10,
                               textColor=BLACK, leading=11)
    skv_s     = ParagraphStyle("Sk", fontName="Helvetica",      fontSize=10,
                               textColor=BLACK, leading=11)
    # Experience: title 10.5pt Bold, dates 10pt Bold right-aligned
    etitle_s  = ParagraphStyle("ET", fontName="Helvetica-Bold", fontSize=10.5,
                               textColor=BLACK, leading=13)
    edate_s   = ParagraphStyle("ED", fontName="Helvetica-Bold", fontSize=10,
                               textColor=BLACK, alignment=2, leading=13)
    # Company line: 10pt Bold name + regular location
    eco_s     = ParagraphStyle("EC", fontName="Helvetica-Bold", fontSize=10,
                               textColor=BLACK, leading=10.5, spaceAfter=0)
    bullet_s  = ParagraphStyle("Bu", fontName="Helvetica",      fontSize=10,
                               textColor=BLACK, leading=10.5, leftIndent=0, spaceAfter=0)
    # Project name: 10.5pt Bold (left), tech: 9.5pt Italic #333333 (right)
    ptitle_s  = ParagraphStyle("PT", fontName="Helvetica-Bold", fontSize=10.5,
                               textColor=BLACK, leading=13)
    ptech_s   = ParagraphStyle("Pk", fontName="Helvetica-Oblique", fontSize=9.5,
                               textColor=SEP,   alignment=2, leading=11)
    # Education: degree 10.5pt Bold, dates 10pt Bold right
    deg_s     = ParagraphStyle("Dg", fontName="Helvetica-Bold", fontSize=10.5,
                               textColor=BLACK, leading=13)
    ddate_s   = ParagraphStyle("Dd", fontName="Helvetica-Bold", fontSize=10,
                               textColor=BLACK, alignment=2, leading=13)
    # University: 10pt Bold name + regular location
    univ_s    = ParagraphStyle("Un", fontName="Helvetica-Bold", fontSize=10,
                               textColor=BLACK, leading=11, spaceAfter=1)

    story = []

    # ── Helpers ───────────────────────────────────────────────────────────────
    def T(text):
        """Sanitize for ReportLab XML + replace em/en dashes with hyphens."""
        s = _sanitize(str(text or "").strip())
        s = s.replace("—", "-").replace("–", "-")
        return s

    def sec(label):
        """Section header in navy bold + navy HR rule underneath."""
        story.append(Paragraph(label, section_s))
        story.append(HRFlowable(width="100%", thickness=0.75,
                                color=RULE, spaceAfter=0))

    def row2(left_para, right_para, lw_frac=0.65):
        """Two-column row: left content + right-aligned dates/tech."""
        lw = CONTENT * lw_frac
        rw = CONTENT * (1 - lw_frac)
        t = Table([[left_para, right_para]], colWidths=[lw, rw])
        t.setStyle(TableStyle([
            ("VALIGN",         (0,0),(-1,-1),"TOP"),
            ("LEFTPADDING",    (0,0),(-1,-1),0),
            ("RIGHTPADDING",   (0,0),(-1,-1),0),
            ("TOPPADDING",     (0,0),(-1,-1),0),
            ("BOTTOMPADDING",  (0,0),(-1,-1),0),
        ]))
        story.append(t)

    def row2_edu(left_para, right_para):
        """Wider left column for education — prevents long degree lines from wrapping."""
        lw = CONTENT * 0.76
        rw = CONTENT * 0.24
        t = Table([[left_para, right_para]], colWidths=[lw, rw])
        t.setStyle(TableStyle([
            ("VALIGN",         (0,0),(-1,-1),"TOP"),
            ("LEFTPADDING",    (0,0),(-1,-1),0),
            ("RIGHTPADDING",   (0,0),(-1,-1),0),
            ("TOPPADDING",     (0,0),(-1,-1),0),
            ("BOTTOMPADDING",  (0,0),(-1,-1),0),
        ]))
        story.append(t)

    def bullet(text):
        story.append(Paragraph("&#183; " + T(text), bullet_s))

    def _href(url, label):
        """Clickable hyperlink in blue (#0563C1) matching original PDF."""
        u = url.strip().replace('"', '%22')
        l = T(label)
        return f'<a href="{u}" color="#0563C1"><u>{l}</u></a>'

    def company_para(text, style):
        """Bold company/university name + regular ' - Location' — matches original exactly."""
        if " - " in text:
            idx = text.index(" - ")
            name_part = T(text[:idx])
            rest_part = T(text[idx + 3:])  # everything after " - "
            # eco_s/univ_s base is Helvetica-Bold; switch location to regular weight
            return Paragraph(
                f'<b>{name_part}</b><font name="Helvetica"> - {rest_part}</font>',
                style)
        return Paragraph(f"<b>{T(text)}</b>", style)

    # ── HEADER ────────────────────────────────────────────────────────────────
    raw_name = str(resume_data.get("full_name", "") or "")
    story.append(Paragraph(raw_name.upper(), name_s))

    # Contact line: centered, | separated, clickable blue links
    loc      = str(resume_data.get("location_text", "") or "")
    phone    = str(resume_data.get("phone", "") or "")
    email    = str(resume_data.get("contact_email", "") or "")
    linkedin = str(resume_data.get("linkedin_url", "") or "")
    github   = str(resume_data.get("github_url", "") or "")
    portfolio= str(resume_data.get("portfolio_url", "") or "")

    SEP_SPAN = '<font color="#333333"> | </font>'
    parts = []
    if loc:    parts.append(T(loc))
    if phone:  parts.append(T(phone))
    if email:  parts.append(_href(f"mailto:{email}", email))
    if linkedin:
        li_u = linkedin.rstrip("/").split("/")[-1]
        parts.append("LinkedIn: " + _href(linkedin, li_u))
    if github:
        gh_u = github.rstrip("/").split("/")[-1]
        parts.append("GitHub: " + _href(github, gh_u))
    if portfolio:
        pf_l = portfolio.replace("https://","").replace("http://","").replace("www.","")
        parts.append("Portfolio: " + _href(portfolio, pf_l))

    if parts:
        story.append(Paragraph(SEP_SPAN.join(parts), contact_s))

    # ── SUMMARY ───────────────────────────────────────────────────────────────
    sec("SUMMARY")
    if resume_data.get("summary"):
        story.append(Paragraph(T(resume_data["summary"]), body_s))

    # ── SKILLS ───────────────────────────────────────────────────────────────
    skills = resume_data.get("skills", [])
    if skills:
        sec("SKILLS")
        tdata = []
        for sk in skills:
            if isinstance(sk, dict):
                cat   = T(sk.get("category", ""))
                items = T(sk.get("items", ""))
                # Colon is BOLD in original (":  " with two spaces after colon)
                tdata.append([
                    Paragraph(cat, cat_s),
                    Paragraph('<b>:  </b>' + items, skv_s)
                ])
        if tdata:
            col_l = 1.75 * inch
            col_r = CONTENT - col_l
            tbl = Table(tdata, colWidths=[col_l, col_r])
            tbl.setStyle(TableStyle([
                ("VALIGN",        (0,0),(-1,-1),"TOP"),
                ("LEFTPADDING",   (0,0),(-1,-1),0),
                ("RIGHTPADDING",  (0,0),(-1,-1),3),
                ("TOPPADDING",    (0,0),(-1,-1),0),
                ("BOTTOMPADDING", (0,0),(-1,-1),0),
            ]))
            story.append(tbl)
    elif resume_data.get("core_skills"):
        sec("SKILLS")
        flat = [s for s in resume_data["core_skills"] if isinstance(s, str) and s.strip()]
        if flat:
            story.append(Paragraph(" • ".join(flat), body_s))

    # ── PROFESSIONAL EXPERIENCE ───────────────────────────────────────────────
    experience = resume_data.get("experience", [])
    if experience:
        sec("PROFESSIONAL EXPERIENCE")
        for exp in experience:
            title   = T(exp.get("title", ""))
            company = T(exp.get("company", ""))
            dates   = T(exp.get("dates", ""))

            # Title bold 10.5pt (left) + Dates bold 10pt (right)
            row2(Paragraph(title, etitle_s), Paragraph(dates, edate_s), lw_frac=0.65)

            # Company line: bold company name + regular " - Location" (matches original)
            if company:
                story.append(company_para(company, eco_s))

            for b in exp.get("bullets", []):
                if isinstance(b, str) and b.strip():
                    bullet(b)
            story.append(Spacer(1, 0))

    # ── PROJECTS ─────────────────────────────────────────────────────────────
    projects = resume_data.get("projects", [])
    if projects:
        sec("PROJECTS")
        for proj in projects:
            pname_raw = proj.get("name", "")
            techs_raw = ", ".join(str(t) for t in proj.get("technologies", []) if t)
            pname = T(pname_raw)
            techs = T(techs_raw)

            # Project name bold 10.5pt (left) + tech italic 9.5pt #333333 (right)
            # Measure exact text widths so both always fit on a single line
            pw = pdfmetrics.stringWidth(pname_raw, "Helvetica-Bold", 10.5)
            tw = pdfmetrics.stringWidth(techs_raw, "Helvetica-Oblique", 9.5)
            gap = CONTENT - pw - tw
            if gap >= 0:
                # Both fit on one line — 3-col table: [name | spacer | tech]
                pt = Table([[Paragraph(pname, ptitle_s), '', Paragraph(techs, ptech_s)]],
                           colWidths=[pw, gap, tw])
                pt.setStyle(TableStyle([
                    ("VALIGN",        (0,0),(-1,-1),"TOP"),
                    ("LEFTPADDING",   (0,0),(-1,-1),0),
                    ("RIGHTPADDING",  (0,0),(-1,-1),0),
                    ("TOPPADDING",    (0,0),(-1,-1),0),
                    ("BOTTOMPADDING", (0,0),(-1,-1),0),
                ]))
                story.append(pt)
            else:
                # Fallback: name gets 52 %, tech gets 48 %
                row2(Paragraph(pname, ptitle_s), Paragraph(techs, ptech_s), lw_frac=0.52)

            for b in proj.get("bullets", []):
                if isinstance(b, str) and b.strip():
                    bullet(b)
            story.append(Spacer(1, 0))

    # ── EDUCATION ────────────────────────────────────────────────────────────
    edu_list = resume_data.get("education", [])
    if edu_list:
        sec("EDUCATION")
        for edu in edu_list:
            edu_block = []
            if isinstance(edu, dict):
                degree = T(edu.get("degree", ""))
                inst   = T(edu.get("institution", ""))
                dates  = T(edu.get("dates", ""))
                # Degree bold 10.5pt (left) + dates bold 10pt (right) — KeepTogether with university
                lw = CONTENT * 0.76
                rw = CONTENT * 0.24
                t = Table([[Paragraph(degree, deg_s), Paragraph(dates, ddate_s)]],
                          colWidths=[lw, rw])
                t.setStyle(TableStyle([
                    ("VALIGN",(0,0),(-1,-1),"TOP"),
                    ("LEFTPADDING",(0,0),(-1,-1),0),
                    ("RIGHTPADDING",(0,0),(-1,-1),0),
                    ("TOPPADDING",(0,0),(-1,-1),0),
                    ("BOTTOMPADDING",(0,0),(-1,-1),0),
                ]))
                edu_block.append(t)
                if inst:
                    edu_block.append(company_para(inst, univ_s))
            elif isinstance(edu, str) and edu.strip():
                parts_e = [p.strip() for p in edu.split(",")]
                if len(parts_e) >= 3:
                    lw = CONTENT * 0.76; rw = CONTENT * 0.24
                    t = Table([[Paragraph(T(parts_e[0]), deg_s),
                                Paragraph(T(", ".join(parts_e[2:])), ddate_s)]],
                              colWidths=[lw, rw])
                    t.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),
                        ("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),
                        ("TOPPADDING",(0,0),(-1,-1),0),("BOTTOMPADDING",(0,0),(-1,-1),0)]))
                    edu_block.append(t)
                    if parts_e[1]:
                        edu_block.append(Paragraph(T(parts_e[1]), univ_s))
                else:
                    edu_block.append(Paragraph(T(edu), body_s))
            if edu_block:
                story.append(KeepTogether(edu_block))

    # ── CERTIFICATIONS ────────────────────────────────────────────────────────
    certs = resume_data.get("certifications", [])
    if certs:
        sec("CERTIFICATIONS")
        for c in certs:
            if isinstance(c, str) and c.strip():
                bullet(c)

    doc.build(story)
    return buf.getvalue()


def _build_cover_pdf(profile_dict: dict, cover_body: str, job_title: str = "") -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
    from reportlab.lib.colors import HexColor
    from io import BytesIO

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            rightMargin=0.9*inch, leftMargin=0.9*inch,
                            topMargin=0.7*inch,   bottomMargin=0.7*inch)

    BLACK = HexColor("#1a1a1a")
    GREEN = HexColor("#16a34a")
    GRAY  = HexColor("#666666")

    # Styles — tighter spacing to fit on one page
    name_s  = ParagraphStyle("CN", fontName="Helvetica-Bold", fontSize=18, textColor=BLACK, spaceAfter=1,  leading=22)
    role_s  = ParagraphStyle("CR", fontName="Helvetica",      fontSize=10, textColor=GREEN, spaceAfter=1,  leading=13)
    meta_s  = ParagraphStyle("CM", fontName="Helvetica",      fontSize=9,  textColor=GRAY,  spaceAfter=0,  leading=11)
    date_s  = ParagraphStyle("CD", fontName="Helvetica",      fontSize=10, textColor=GRAY,  spaceAfter=0,  leading=13)
    re_s    = ParagraphStyle("RE", fontName="Helvetica-Bold", fontSize=10, textColor=BLACK, spaceAfter=0,  leading=13)
    dear_s  = ParagraphStyle("DR", fontName="Helvetica",      fontSize=10, textColor=BLACK, spaceAfter=10, leading=14)
    body_s  = ParagraphStyle("BD", fontName="Helvetica",      fontSize=10, textColor=BLACK, leading=15,    spaceAfter=10)
    sign_s  = ParagraphStyle("SG", fontName="Helvetica",      fontSize=10, textColor=BLACK, spaceAfter=2,  leading=13)
    sname_s = ParagraphStyle("SN", fontName="Helvetica-Bold", fontSize=10, textColor=BLACK, spaceAfter=1,  leading=13)
    srole_s = ParagraphStyle("SR", fontName="Helvetica",      fontSize=9,  textColor=GRAY,  spaceAfter=0,  leading=12)

    name  = profile_dict.get("full_name", "")
    role  = profile_dict.get("current_job_title", "")
    email = "ranjithmaddirala24@gmail.com"
    phone = ""   # add if needed
    today = dt.date.today().strftime("%B %d, %Y")

    story = []

    # ── Letterhead: two-column layout (name/role left | date right) ──────────
    left_block  = [Paragraph(_sanitize(name), name_s),
                   Paragraph(_sanitize(role), role_s),
                   Paragraph(_sanitize(email), meta_s)]
    right_block = [Spacer(1, 8), Paragraph(today, date_s)]

    tbl = Table([[left_block, right_block]], colWidths=["70%", "30%"])
    tbl.setStyle(TableStyle([
        ("VALIGN",  (0, 0), (-1, -1), "TOP"),
        ("ALIGN",   (1, 0), (1, 0),   "RIGHT"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
    ]))
    story.append(tbl)
    story.append(HRFlowable(width="100%", thickness=0.8, color=GREEN, spaceBefore=6, spaceAfter=14))

    # ── Re: line ──────────────────────────────────────────────────────────────
    if job_title:
        story.append(Paragraph(f"Re: Application for {_sanitize(job_title)}", re_s))
        story.append(Spacer(1, 10))

    # ── Salutation ────────────────────────────────────────────────────────────
    story.append(Paragraph("Dear Hiring Manager,", dear_s))

    # ── Body ─────────────────────────────────────────────────────────────────
    for para in cover_body.split("\n\n"):
        para = para.strip()
        if para:
            story.append(Paragraph(_sanitize(para.replace("\n", " ")), body_s))

    # ── Closing ───────────────────────────────────────────────────────────────
    story.append(Spacer(1, 4))
    story.append(Paragraph("Sincerely,", sign_s))
    story.append(Spacer(1, 22))   # space for handwritten signature
    story.append(Paragraph(_sanitize(name), sname_s))
    story.append(Paragraph(_sanitize(role), srole_s))
    story.append(Paragraph(_sanitize(email), ParagraphStyle("se", fontName="Helvetica", fontSize=9, textColor=GRAY)))

    doc.build(story)
    return buf.getvalue()


@app.route("/api/resume/generate", methods=["POST"])
def generate_resume():
    profile = CVProfile.query.order_by(CVProfile.created_at.desc()).first()
    if not profile:
        return jsonify({"error": "Upload your CV first on the Upload CV tab."}), 400

    data = request.json or {}
    jd   = (data.get("jd") or "").strip()
    if not jd:
        return jsonify({"error": "Please paste a job description."}), 400

    raw_cv = profile.raw_text or ""
    p_dict = profile.to_dict()

    try:
        resume_data   = _generate_resume_content(raw_cv, jd)

        # Extract job title from first 300 chars of JD for cover letter Re: line
        jd_title = ""
        for line in jd[:300].splitlines():
            line = line.strip()
            if line and len(line) < 80:
                jd_title = line
                break

        outreach_ctx = (
            f"=== CANDIDATE REAL BACKGROUND — use ONLY this, never fabricate ===\n{raw_cv[:3000]}\n\n"
            f"=== JOB DESCRIPTION — write all documents to target THIS specific role ===\n{jd[:1500]}"
        )
        cover_body       = _generate_text(COVER_SYSTEM,          outreach_ctx, 1500)
        cold_email       = _generate_text(EMAIL_SYSTEM,          outreach_ctx, 600)
        linkedin_connect = _generate_text(LINKEDIN_CONNECT_SYSTEM, outreach_ctx, 150)
        linkedin_msg     = _generate_text(LINKEDIN_MSG_SYSTEM,   outreach_ctx, 600)

        # Enforce LinkedIn connection note 300-char hard limit
        if len(linkedin_connect) > 300:
            linkedin_connect = linkedin_connect[:297].rstrip() + "..."

        cache = {
            "resume_data": resume_data,
            "cover_body":  cover_body,
            "profile":     p_dict,
            "jd":          jd,
            "jd_title":    jd_title,
        }
        app.config["_resume_cache"] = cache
        # Persist to disk so downloads work even after server restart
        _cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".resume_cache.json")
        try:
            with open(_cache_path, "w", encoding="utf-8") as _cf:
                json.dump(cache, _cf, ensure_ascii=False)
        except Exception:
            pass

        return jsonify({
            "resume":            resume_data,
            "cover_letter":      cover_body,
            "cold_email":        cold_email,
            "linkedin_connect":  linkedin_connect,
            "linkedin_msg":      linkedin_msg,
            "ats_keywords":      resume_data.get("ats_keywords_added", []),
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Generation failed: {e}"}), 500


@app.route("/api/resume/download/<doc_type>")
def download_doc(doc_type):
    cache = app.config.get("_resume_cache")
    # Load from disk if not in memory (server was restarted)
    if not cache:
        _cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".resume_cache.json")
        if os.path.exists(_cache_path):
            try:
                with open(_cache_path, encoding="utf-8") as _cf:
                    cache = json.load(_cf)
                app.config["_resume_cache"] = cache
            except Exception:
                pass
    if not cache:
        return jsonify({"error": "Please generate a resume first, then download."}), 400

    name = (cache["profile"].get("full_name") or "Resume").replace(" ", "_")

    if doc_type == "resume":
        # Merge contact fields from profile into resume_data for the PDF
        rd = dict(cache["resume_data"])
        p  = cache["profile"]
        for key in ("contact_email","phone","linkedin_url","github_url","portfolio_url","location_text"):
            if not rd.get(key):
                rd[key] = p.get(key, "")
        pdf      = _build_resume_pdf(rd)
        filename = "ranjith_kumar_resume.pdf"
    elif doc_type == "cover":
        pdf      = _build_cover_pdf(cache["profile"], cache["cover_body"], cache.get("jd_title",""))
        filename = "ranjith_kumar_coverletter.pdf"
    else:
        return jsonify({"error": "Unknown type"}), 400

    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ── Boot ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        # Auto-migrate missing columns
        import sqlite3 as _sq
        con = _sq.connect(_DB_PATH)
        cur = con.cursor()
        job_cols = [r[1] for r in cur.execute("PRAGMA table_info(job)").fetchall()]
        cv_cols  = [r[1] for r in cur.execute("PRAGMA table_info(cv_profile)").fetchall()]
        for col, typ in [("date_posted", "VARCHAR(32)"), ("match_score", "INTEGER")]:
            if col not in job_cols:
                cur.execute(f"ALTER TABLE job ADD COLUMN {col} {typ}")
                print(f"[DB] Added job.{col}")
        for col, typ in [
            ("education_text",      "TEXT"),
            ("projects_text",       "TEXT"),
            ("certifications_text", "TEXT"),
            ("experience_text",     "TEXT"),
            ("contact_email",       "VARCHAR(256)"),
            ("phone",               "VARCHAR(64)"),
            ("linkedin_url",        "VARCHAR(512)"),
            ("github_url",          "VARCHAR(512)"),
            ("portfolio_url",       "VARCHAR(512)"),
            ("location_text",       "VARCHAR(256)"),
        ]:
            if col not in cv_cols:
                cur.execute(f"ALTER TABLE cv_profile ADD COLUMN {col} TEXT")
                print(f"[DB] Added cv_profile.{col}")
        con.commit()
        con.close()
        app.run(debug=True, port=5000, use_reloader=False)
