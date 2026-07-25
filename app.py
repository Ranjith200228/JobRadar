# -*- coding: utf-8 -*-
import os
import re
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
    original_page_count = db.Column(db.Integer, nullable=True)
    profile_name = db.Column(db.String(128), nullable=True)
    is_active = db.Column(db.Boolean, default=False)
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
            "original_page_count": self.original_page_count or 1,
            "profile_name": self.profile_name or self.current_job_title or "My CV",
            "is_active": bool(self.is_active),
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
    notes = db.Column(db.Text, nullable=True)
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
            "status_updated_at": self.status_updated_at.isoformat() if self.status_updated_at else None,
            "notes": self.notes or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_lenient_json(text: str):
    """Parse JSON from an AI response, repairing truncation if needed.
    1. Extract the outermost {...} block.
    2. Try strict parse.
    3. If truncated: close any open string, strip dangling fragments, close
       open brackets — chopping back to the last complete element if necessary."""
    m = re.search(r"\{[\s\S]*", text)
    if not m:
        raise ValueError("no JSON object found")
    s = m.group()

    def _close(fragment: str) -> str:
        stack, instr, esc = [], False, False
        close_at = None   # index where the top-level {...} fully balances
        for i, ch in enumerate(fragment):
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if instr:
                if ch == '"':
                    instr = False
                continue
            if ch == '"':
                instr = True
            elif ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                if stack:
                    stack.pop()
                    if not stack:
                        close_at = i + 1
                        break   # top-level object/array is complete — ignore anything after
        if close_at is not None:
            # Complete JSON already present, just followed by trailing text
            # (markdown fences, commentary, etc.) — discard the trailing text.
            return fragment[:close_at]
        out = fragment
        if instr:
            out += '"'
        out = re.sub(r"[,:\s]+$", "", out)
        for ch in reversed(stack):
            out += "}" if ch == "{" else "]"
        return out

    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    fragment = s
    for _ in range(200):
        try:
            return json.loads(_close(fragment))
        except json.JSONDecodeError:
            idx = fragment.rfind(",")
            if idx < 0:
                raise
            fragment = fragment[:idx]
    raise ValueError("could not repair JSON")


def _get_active_profile():
    """Return the active CV profile, or fall back to the most recently created."""
    active = CVProfile.query.filter_by(is_active=True).first()
    if active:
        return active
    return CVProfile.query.order_by(CVProfile.created_at.desc()).first()


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


def _name_to_slug(text: str, max_len: int = 40) -> str:
    """Convert 'John Smith' → 'john_smith' for use in filenames."""
    slug = re.sub(r'[^a-zA-Z0-9\s]', '', text.strip())
    slug = re.sub(r'\s+', '_', slug).lower().strip('_')
    return slug[:max_len] or "candidate"


def _save_profile(parsed: dict, raw_text: str, page_count: int = 1, profile_name: str = "") -> CVProfile:
    # Guard against a malformed/empty AI parse response silently creating a
    # blank ghost profile — require at minimum a name to trust the extraction.
    if not (parsed.get("full_name") or "").strip():
        raise ValueError(
            "Could not extract your details from this CV. Make sure it's a real, "
            "readable resume with your name clearly visible, then try again."
        )
    # Re-uploading the same CV replaces the old copy instead of duplicating it
    dupes = CVProfile.query.filter_by(
        full_name=parsed.get("full_name", ""),
        current_job_title=parsed.get("current_job_title", ""),
    ).all()
    for d in dupes:
        db.session.delete(d)
    db.session.flush()
    # Deactivate all existing profiles (new upload becomes active)
    CVProfile.query.update({"is_active": False})
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
        original_page_count=page_count,
        profile_name=profile_name or parsed.get("current_job_title") or "My CV",
        is_active=True,
    )
    db.session.add(profile)
    db.session.commit()
    return profile


def _compact_profile_summary(p: dict) -> str:
    """3-line scoring context — ~120 tokens instead of the full profile JSON (~4000-8000 tokens).
    Skills, title, and years of experience are all a scoring model needs.
    """
    skills = ", ".join((p.get("technical_skills") or [])[:30])
    return (
        f"Role: {p.get('current_job_title','')}\n"
        f"Experience: {p.get('years_of_experience',0)} years\n"
        f"Skills: {skills}"
    )


def score_job(profile_dict: dict, job_description: str, search_query: str = "") -> int:
    """Score a single job. Uses a compact profile summary to minimise token usage.
    search_query: the title the user searched for — used to penalise off-topic jobs.
    """
    client   = get_anthropic()
    summary  = _compact_profile_summary(profile_dict)
    jd_short = job_description[:1500]
    query_line = f"\nUser is searching for: {search_query}" if search_query else ""
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,      # only needs {"score":75}
        temperature=0,
        system=(
            'Return ONLY JSON: {"score":<integer 0-100>}. '
            'Score how well the candidate fits this job. Rules: '
            '1) If the job is a DIFFERENT role type than the user is searching for, score 30 or below. '
            '2) If the job matches the searched role, score 70-100 based on skill fit. '
            '3) If the job description is missing or very short, judge by the TITLE alone — '
            'a title matching the searched role and the candidate profile deserves 75+. '
            'Do NOT penalize a job for having little text.'
        ),
        messages=[{"role": "user", "content": f"Candidate:\n{summary}{query_line}\n\nJob:\n{jd_short}"}],
    )
    return json.loads(_clean_json(msg.content[0].text))["score"]


# ── Search result cache (in-memory, 30-min TTL) ───────────────────────────────
_SEARCH_CACHE: dict = {}
_SEARCH_CACHE_TTL = 1800   # seconds

def _cache_key(query: str, location: str, platforms: list) -> tuple:
    return (query.lower().strip(), location.lower().strip(), tuple(sorted(platforms)))

def _cache_get(key: tuple):
    entry = _SEARCH_CACHE.get(key)
    if entry:
        ts, data = entry
        if dt.datetime.utcnow().timestamp() - ts < _SEARCH_CACHE_TTL:
            return data
    return None

def _cache_set(key: tuple, data: list):
    _SEARCH_CACHE[key] = (dt.datetime.utcnow().timestamp(), data)
    # Evict old entries if cache grows large
    if len(_SEARCH_CACHE) > 50:
        oldest = sorted(_SEARCH_CACHE.items(), key=lambda x: x[1][0])
        for k, _ in oldest[:20]:
            _SEARCH_CACHE.pop(k, None)


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

# ── Country detection for international job searches ──────────────────────────
# Maps lowercase city/country keywords → (indeed_country_code, wellfound_country_name)
COUNTRY_MAP = {
    # India
    "india":      ("in", "India"),
    "hyderabad":  ("in", "India"),
    "bangalore":  ("in", "India"),
    "bengaluru":  ("in", "India"),
    "mumbai":     ("in", "India"),
    "delhi":      ("in", "India"),
    "new delhi":  ("in", "India"),
    "chennai":    ("in", "India"),
    "pune":       ("in", "India"),
    "kolkata":    ("in", "India"),
    "ahmedabad":  ("in", "India"),
    "noida":      ("in", "India"),
    "gurgaon":    ("in", "India"),
    "gurugram":   ("in", "India"),
    "kochi":      ("in", "India"),
    # United Kingdom
    "uk":             ("uk", "United Kingdom"),
    "united kingdom": ("uk", "United Kingdom"),
    "england":        ("uk", "United Kingdom"),
    "london":         ("uk", "United Kingdom"),
    "manchester":     ("uk", "United Kingdom"),
    "birmingham":     ("uk", "United Kingdom"),
    "edinburgh":      ("uk", "United Kingdom"),
    "glasgow":        ("uk", "United Kingdom"),
    "bristol":        ("uk", "United Kingdom"),
    "leeds":          ("uk", "United Kingdom"),
    # Canada
    "canada":    ("ca", "Canada"),
    "toronto":   ("ca", "Canada"),
    "vancouver": ("ca", "Canada"),
    "montreal":  ("ca", "Canada"),
    "calgary":   ("ca", "Canada"),
    "ottawa":    ("ca", "Canada"),
    # Australia
    "australia": ("au", "Australia"),
    "sydney":    ("au", "Australia"),
    "melbourne": ("au", "Australia"),
    "brisbane":  ("au", "Australia"),
    "perth":     ("au", "Australia"),
    "adelaide":  ("au", "Australia"),
    # Germany
    "germany":  ("de", "Germany"),
    "berlin":   ("de", "Germany"),
    "munich":   ("de", "Germany"),
    "hamburg":  ("de", "Germany"),
    "frankfurt":("de", "Germany"),
    # Singapore
    "singapore": ("sg", "Singapore"),
    # UAE / Dubai
    "uae":   ("ae", "United Arab Emirates"),
    "dubai": ("ae", "United Arab Emirates"),
    "abu dhabi": ("ae", "United Arab Emirates"),
    # Netherlands
    "netherlands": ("nl", "Netherlands"),
    "amsterdam":   ("nl", "Netherlands"),
    # France
    "france": ("fr", "France"),
    "paris":  ("fr", "France"),
    # Ireland
    "ireland": ("ie", "Ireland"),
    "dublin":  ("ie", "Ireland"),
    # New Zealand
    "new zealand":  ("nz", "New Zealand"),
    "auckland":     ("nz", "New Zealand"),
    "wellington":   ("nz", "New Zealand"),
    # Pakistan
    "pakistan": ("pk", "Pakistan"),
    "karachi":  ("pk", "Pakistan"),
    "lahore":   ("pk", "Pakistan"),
    "islamabad":("pk", "Pakistan"),
    # Philippines
    "philippines": ("ph", "Philippines"),
    "manila":      ("ph", "Philippines"),
    # Brazil
    "brazil":      ("br", "Brazil"),
    "sao paulo":   ("br", "Brazil"),
    "são paulo":   ("br", "Brazil"),
    # South Africa
    "south africa":  ("za", "South Africa"),
    "johannesburg":  ("za", "South Africa"),
    "cape town":     ("za", "South Africa"),
    # Mexico
    "mexico":       ("mx", "Mexico"),
    "mexico city":  ("mx", "Mexico"),
    # Spain
    "spain":    ("es", "Spain"),
    "madrid":   ("es", "Spain"),
    "barcelona":("es", "Spain"),
    # Switzerland
    "switzerland": ("ch", "Switzerland"),
    "zurich":      ("ch", "Switzerland"),
    "geneva":      ("ch", "Switzerland"),
    # Sweden
    "sweden":    ("se", "Sweden"),
    "stockholm": ("se", "Sweden"),
}

def _detect_country(location: str) -> tuple:
    """
    Detect country from a location string.
    Returns (indeed_country_code, wellfound_country_name).
    Defaults to ('usa', 'United States') if not recognized.
    """
    if not location:
        return ("usa", "United States")
    loc_lower = location.lower()
    for keyword, (code, name) in COUNTRY_MAP.items():
        if keyword in loc_lower:
            return (code, name)
    return ("usa", "United States")


# The multi-board actor's countryIndeed accepts common country names —
# verified against the actor's live input schema (examples: usa, uk, canada,
# australia, germany, france, india, singapore, uae).
_INDEED_COUNTRY = {
    "usa": "usa", "in": "india", "uk": "uk", "ca": "canada", "au": "australia",
    "de": "germany", "sg": "singapore", "ae": "uae", "nl": "netherlands",
    "fr": "france", "ie": "ireland", "nz": "new zealand", "pk": "pakistan",
    "ph": "philippines", "br": "brazil", "za": "south africa", "mx": "mexico",
    "es": "spain", "ch": "switzerland", "se": "sweden",
}


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


# Collects scraper failures during a fetch so they can be shown to the user
# instead of a misleading generic "no jobs found".
_LAST_FETCH_ERRORS: list = []


def _run_actor(apify, actor_id: str, run_input: dict) -> list:
    short = actor_id.split("/")[-1]
    try:
        try:
            # Hard cap: abort any scraper run that exceeds 4 minutes
            run = apify.actor(actor_id).call(run_input=run_input, timeout_secs=240)
        except TypeError:
            # Older apify-client versions don't accept timeout_secs
            run = apify.actor(actor_id).call(run_input=run_input)

        status = run.get("status") if isinstance(run, dict) else getattr(run, "status", None)
        if status and status != "SUCCEEDED":
            msg = f"{short}: run ended with status {status}"
            print(f"[Apify:{short}] {msg}")
            _LAST_FETCH_ERRORS.append(msg)

        items = list(apify.dataset(_get_dataset_id(run)).iterate_items())
        print(f"[Apify:{short}] {len(items)} results")
        return items
    except Exception as e:
        err = str(e)
        print(f"[Apify:{short}] ERROR: {err}")
        # Surface billing / quota errors immediately — don't mask as "no results"
        lower = err.lower()
        if "hard limit" in lower or "usage" in lower or "quota" in lower or "billing" in lower or "payment" in lower or "credit" in lower:
            raise RuntimeError(
                "Your Apify account has hit its monthly usage limit. "
                "Please go to apify.com → Billing and upgrade your plan or wait for the monthly reset."
            )
        _LAST_FETCH_ERRORS.append(f"{short}: {err[:200]}")
        return []


def _run_multi_board(apify, query, location, site_map, hours_old=48, country_code="usa", search_terms=None):
    """Run the multi-board actor.
    search_terms: optional list of query variants — the actor OR-searches them
    all in ONE run (much faster than separate retry calls).
    maxResults is per-site; 12 keeps runs fast while giving plenty to score.
    """
    run_input = {
        "location":                 location,
        "sites":                    list(site_map.keys()),
        "maxResults":               10,
        "descriptionFormat":        "markdown",   # valid enum: markdown | html
        "linkedinFetchDescription": False,        # major speed win — titles are enough for matching
        "countryIndeed":            country_code,
    }
    if search_terms and len(search_terms) > 1:
        run_input["searchTerms"] = search_terms[:5]   # OR-search, single run
    else:
        run_input["searchTerm"] = query
    if hours_old:
        run_input["hoursOld"] = hours_old
    items = _run_actor(apify, ACTOR_MULTI_BOARD, run_input)
    out = []
    for item in items:
        site  = item.get("site", "")
        label = site_map.get(site, site or "Job Board")
        out.append(_normalise(item, label))
    return out


# Query rewrites — job boards match titles literally, so the acronym and the
# spelled-out form return DIFFERENT results. If the first search comes back
# thin, we retry with the alternate form.
_QUERY_REWRITES = [
    ("identity and access management", "IAM"),
    ("identity access management",     "IAM"),
    ("iam",                            "Identity Access Management"),
    ("machine learning",               "ML"),
    ("artificial intelligence",        "AI"),
    ("site reliability engineer",      "SRE"),
    ("sre",                            "Site Reliability Engineer"),
    ("quality assurance",              "QA"),
    ("qa",                             "Quality Assurance"),
    ("microsoft 365",                  "M365"),
    ("m365",                           "Microsoft 365"),
    ("amazon web services",            "AWS"),
    ("user experience",                "UX"),
    ("business analyst",               "BA"),
]

def _alt_query(query: str) -> str | None:
    """Return an alternate phrasing (acronym ↔ full form), or None."""
    q = query.lower()
    for old, new in _QUERY_REWRITES:
        if re.search(r"(?<![a-z0-9])" + re.escape(old) + r"(?![a-z0-9])", q):
            alt = re.sub(r"(?i)(?<![a-z0-9])" + re.escape(old) + r"(?![a-z0-9])", new, query)
            if alt.lower() != q:
                return alt
    return None


def fetch_jobs(query: str, location: str, platforms: list, hours: int = 48) -> list:
    """hours: strict freshness window chosen by the user — 6, 24, or 48.
    The window is NEVER widened; only jobs posted within it are returned."""
    apify    = get_apify()
    results  = []
    selected = set(platforms)

    # Detect country once — used by all actors
    country_code, country_name = _detect_country(location)
    indeed_cc = _INDEED_COUNTRY.get(country_code, "usa")
    city = location.split(",")[0].strip() if location else ""
    print(f"[JobFetch] Location='{location}' → country='{indeed_cc}' ({country_name})")

    # Multi-board platforms
    multi = selected - DEDICATED_ACTORS
    site_map: dict[str, str] = {}
    if multi:
        for p in multi:
            site = MULTI_BOARD_MAP.get(p, "indeed")
            if site not in site_map:
                site_map[site] = p

        # Regional boards — huge coverage boost for local searches
        if country_code == "in":
            site_map.setdefault("naukri", "Naukri")      # India's biggest job board
        if country_code == "ae":
            site_map.setdefault("bayt", "Bayt")          # Middle East's biggest

        # Both query phrasings (e.g. 'IAM' + 'Identity Access Management')
        # OR-searched in a SINGLE scraper run. The user's freshness window is
        # respected strictly — never widened.
        terms = [query]
        alt = _alt_query(query)
        if alt:
            terms.append(alt)

        mb_results = _run_multi_board(apify, query, location, site_map,
                                      hours_old=hours, country_code=indeed_cc,
                                      search_terms=terms)
        results.extend(mb_results)

    # Wellfound — strict freshness: 'today' for 6/24h, never wider than the window
    if "Wellfound" in selected:
        wf_input = {
            "countryName":    country_name,
            "locationName":   city or country_name,   # required — must not be empty
            "includeKeyword": query,
            "pagesToFetch":   1,
            "datePosted":     "today" if hours <= 24 else "3days",   # enum: all|today|3days|week|month
        }
        items = _run_actor(apify, ACTOR_WELLFOUND, wf_input)
        for item in items[:15]:
            results.append(_normalise(item, "Wellfound"))

    # Dice — US-focused board; skip if non-US country detected
    if "Dice" in selected:
        if country_code != "usa":
            print(f"[JobFetch] Skipping Dice — not a US location (country_code='{country_code}')")
        else:
            items = _run_actor(apify, ACTOR_DICE, {
                "keyword":        query,
                "location":       location,
                "posted_date":    "24h" if hours <= 24 else "3d",   # enum: all|24h|3d|7d|30d
                "results_wanted": 15,
            })
            for item in items[:15]:
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
    p = _get_active_profile()
    return jsonify({"profile": p.to_dict() if p else None})


@app.route("/api/upload-cv", methods=["POST"])
def upload_cv():
    if "file" not in request.files:
        return jsonify({"error": "No file attached."}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are accepted."}), 400
    try:
        file_bytes = f.read()
        # Detect original page count to use for adaptive resume generation
        try:
            _orig_doc   = fitz.open(stream=file_bytes, filetype="pdf")
            _page_count = _orig_doc.page_count
            _orig_doc.close()
        except Exception:
            _page_count = 1
        raw = extract_pdf_text(file_bytes)
        if not raw:
            return jsonify({"error": "Could not extract text. Make sure it's not a scanned image PDF."}), 400
        parsed  = parse_cv(raw)
        profile = _save_profile(parsed, raw, _page_count)
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
    profile = _get_active_profile()
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
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
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

    # Strict freshness window — only 6, 24, or 48 hours allowed
    try:
        hours = int(data.get("hours") or 48)
    except (TypeError, ValueError):
        hours = 48
    if hours not in (6, 24, 48):
        hours = 48

    if not query:
        return jsonify({"error": "Enter a job title to search."}), 400
    try:
        # ── Check in-memory cache before hitting Apify ────────────────────────
        ckey = _cache_key(query, location, platforms + [f"h{hours}"])
        raw  = _cache_get(ckey)
        if raw is not None:
            print(f"[JobFetch] Cache hit — skipping Apify call for '{query}'")
        else:
            _LAST_FETCH_ERRORS.clear()
            raw = fetch_jobs(query, location, platforms, hours)
            # Only cache healthy result sets — a thin result (scrapers having a
            # bad moment) shouldn't be locked in for 30 minutes.
            if len(raw) >= 5:
                _cache_set(ckey, raw)

        if not raw:
            if _LAST_FETCH_ERRORS:
                detail = " | ".join(_LAST_FETCH_ERRORS[:2])
                return jsonify({"error": f"The job scraper hit a problem: {detail}"}), 502
            return jsonify({"error": f"No jobs posted in the last {hours} hours were found for this search. Try the 48-hour filter, broader keywords, or a different location."}), 404

        # Wipe old unsaved jobs — every search starts fresh
        Job.query.filter_by(status="Saved").delete()
        db.session.commit()

        # Relevance gate at save time — scrapers often return loosely related
        # jobs (e.g. a Python job for an 'IAM engineer' search). Drop them here
        # so they never even appear in the feed.
        qf = _query_tokens(query)
        gated_out = [0]
        seen: set[tuple] = set()
        saved = []

        def _gate_and_save(raw_list):
            for jd in raw_list:
                if not jd["job_title"]:
                    continue
                if not _passes_query_gate(jd["job_title"], jd.get("job_description", ""), qf):
                    gated_out[0] += 1
                    continue
                dedup_key = (jd["job_title"].lower()[:60], (jd["company_name"] or "").lower()[:40])
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
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

        _gate_and_save(raw)

        # ── Auto-rescue: nothing relevant + narrow board selection → transparently
        # retry across ALL major boards (same query, same strict time window).
        note = ""
        MAJOR = ["LinkedIn", "Indeed", "Glassdoor", "Google Jobs", "ZipRecruiter"]
        if not saved and any(p not in platforms for p in MAJOR):
            print(f"[JobFetch] 0 relevant results from {len(platforms)} board(s) — auto-expanding to all major boards")
            raw2 = fetch_jobs(query, location, MAJOR, hours)
            _gate_and_save(raw2)
            if saved:
                note = f"Your selected board(s) had no matches, so we searched all major boards and found {len(saved)}."

        db.session.commit()
        print(f"[JobFetch] Saved {len(saved)} relevant jobs ({gated_out[0]} off-topic results dropped)")
        if not saved:
            return jsonify({"error": f"No '{query}' jobs posted in the last {hours} hours were found, even after searching all major boards. Try the 48-hour filter or broader keywords."}), 404
        return jsonify({"jobs": [j.to_dict() for j in saved], "count": len(saved), "note": note})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Fetch failed: {e}"}), 500


_QUERY_STOP = {"and","the","for","with","via","per","of","in","at","a","an","to","by","or","as","is"}

# Generic role words — these appear in almost every job title, so matching them
# alone proves nothing ("Engineer" matches both "IAM Engineer" and "Python Engineer").
_GENERIC_ROLE_WORDS = {
    "engineer","engineers","engineering","developer","developers","development",
    "manager","managers","management","analyst","analysts","specialist","specialists",
    "consultant","consultants","architect","architects","administrator","administrators",
    "admin","lead","senior","junior","intern","associate","staff","principal",
    "expert","professional","coordinator","technician","officer","head","director",
}

# Acronym / synonym expansion — a search for "IAM" should also match
# "Identity & Access Management Engineer" job titles.
_QUERY_SYNONYMS = {
    "iam":        ["identity", "access management"],
    "identity":   ["iam"],
    "sre":        ["site reliability"],
    "ml":         ["machine learning"],
    "ai":         ["artificial intelligence"],
    "qa":         ["quality assurance", "quality engineer", "test"],
    "ux":         ["user experience"],
    "ui":         ["user interface"],
    "hr":         ["human resources"],
    "db":         ["database"],
    "sec":        ["security"],
    "cyber":      ["security"],
    "devops":     ["dev ops", "platform engineer"],
    "fullstack":  ["full stack", "full-stack"],
    "frontend":   ["front end", "front-end"],
    "backend":    ["back end", "back-end"],
    "sysadmin":   ["system administrator", "systems administrator"],
    "pm":         ["product manager", "project manager"],
    "ba":         ["business analyst"],
    "365":        ["m365", "o365", "office 365", "microsoft 365"],
    "m365":       ["o365", "microsoft 365", "office 365"],
    "o365":       ["m365", "microsoft 365", "office 365"],
    "azure":      ["entra"],
    "entra":      ["azure ad", "azure active directory"],
}


def _tok_match(tok: str, text: str) -> bool:
    """Word-boundary prefix match. 'iam' matches 'IAM Engineer' but NOT 'William'.
    'admin' matches 'Administrator' (prefix). Multi-word tokens use substring."""
    if " " in tok:
        return tok in text
    return re.search(r"(?<![a-z0-9])" + re.escape(tok), text) is not None


def _query_tokens(search_query: str) -> tuple:
    """Split query into (specific, generic) token sets.
    specific: distinguishing words like 'iam', 'windows', '365', 'python'
    generic:  role words like 'engineer', 'administrator', 'manager'
    Acronyms (3+ chars) are kept and expanded with synonyms."""
    words = [w.lower() for w in re.split(r"[^A-Za-z0-9+#]+", search_query) if w]
    specific, generic = set(), set()
    for w in words:
        if len(w) < 3 or w in _QUERY_STOP:
            continue
        if w in _GENERIC_ROLE_WORDS:
            generic.add(w[:5])   # 'admin' matches both 'Admin' and 'Administrator'
        else:
            specific.add(w[:10])
            for syn in _QUERY_SYNONYMS.get(w, []):
                specific.add(syn)
    return (specific, generic)


def _passes_query_gate(title: str, desc: str, qf: tuple | None) -> bool:
    """Hard relevance gate against the user's search query.

    Rules:
    - If the query has SPECIFIC words (e.g. 'iam', 'windows'), the job title must
      contain one of them. A generic-word-only title match (e.g. just 'Engineer')
      only passes if the description also mentions a specific word.
    - If the query has only generic words, the title must contain one of them.
    """
    if not qf:
        return True
    specific, generic = qf
    title = (title or "").lower()
    desc  = (desc or "")[:1500].lower()
    if specific:
        if any(_tok_match(t, title) for t in specific):
            return True
        if any(_tok_match(t, title) for t in generic):
            # Generic title ('Systems Administrator'). With a description, require
            # a specific-tech mention. With NO description (fast LinkedIn mode),
            # give benefit of the doubt — AI scoring makes the final call.
            if not desc.strip():
                return True
            if any(_tok_match(t, desc) for t in specific):
                return True
        return False
    if generic:
        return any(_tok_match(t, title) for t in generic)
    return True


def _worth_scoring(job: "Job", skill_tokens: set, qf: tuple | None = None) -> bool:
    """Pre-filter before Claude scoring: query relevance gate + CV skill overlap."""
    if not _passes_query_gate(job.job_title, job.job_description, qf):
        return False
    if not skill_tokens:
        return True
    text = (job.job_title or "").lower() + " " + (job.job_description or "")[:400].lower()
    return any(tok in text for tok in skill_tokens)


@app.route("/api/jobs/score-all", methods=["POST"])
def score_all():
    profile = _get_active_profile()
    if not profile:
        return jsonify({"error": "Upload your CV first."}), 400

    # Read optional search query from the request — used for relevance pre-filter
    search_query = ((request.json or {}).get("query") or "").strip()
    qf = _query_tokens(search_query) if search_query else None
    if qf:
        print(f"[score] Query gate — specific: {qf[0]} | generic: {qf[1]}")

    unscored = Job.query.filter(Job.match_score.is_(None)).all()
    if not unscored:
        return jsonify({"jobs": [j.to_dict() for j in Job.query.order_by(Job.created_at.desc()).all()]})

    pdict = profile.to_dict()

    # Build CV-skill token set — used by stage-2 pre-filter
    raw_skills: list = pdict.get("technical_skills") or []
    skill_tokens = {s.lower()[:5] for s in raw_skills if len(s) >= 3}
    for word in (pdict.get("current_job_title") or "").lower().split():
        if len(word) >= 4:
            skill_tokens.add(word[:5])

    scores: dict[int, int] = {}
    score_errors: list = []
    lock = threading.Lock()

    def _score(job):
        try:
            s = score_job(pdict, job.job_description or job.job_title or "", search_query)
            with lock:
                scores[job.id] = s
        except Exception as e:
            with lock:
                score_errors.append(str(e))
            print(f"[score] {job.id}: {e}")

    # Pre-filter: query-title gate first, then CV-skill overlap
    to_score  = [j for j in unscored if _worth_scoring(j, skill_tokens, qf)]
    skip_jobs = [j for j in unscored if j not in to_score]

    # Jobs that fail the pre-filter → score 0 (will be removed below)
    for job in skip_jobs:
        job.match_score = 0
    print(f"[score] Scoring {len(to_score)} / {len(unscored)} jobs (skipped {len(skip_jobs)} irrelevant)")

    with ThreadPoolExecutor(max_workers=4) as pool:
        for _ in as_completed([pool.submit(_score, j) for j in to_score]):
            pass

    # Every single AI call failed → API problem, NOT bad matches.
    # Surface the real error instead of silently deleting all jobs.
    if to_score and not scores:
        db.session.commit()   # keep gate zeros
        msg = (score_errors[0] if score_errors else "unknown error")[:250]
        return jsonify({"error": f"AI scoring failed — {msg}"}), 502

    for job in to_score:
        # Scoring failed (API error etc.) → mark 0 so it gets filtered out
        job.match_score = scores.get(job.id, 0)
    db.session.commit()

    # ── Adaptive quality gate ─────────────────────────────────────────────────
    # Prefer ≥75%. If nothing clears it, fall back to ≥60, then ≥40 — an empty
    # screen helps nobody when there ARE relevant jobs, just scored cautiously.
    scored_saved = Job.query.filter(Job.status == "Saved", Job.match_score.isnot(None)).all()
    threshold = 75
    if scored_saved and not any(j.match_score >= 75 for j in scored_saved):
        if any(j.match_score >= 60 for j in scored_saved):
            threshold = 60
        elif any(j.match_score >= 40 for j in scored_saved):
            threshold = 40
    if threshold != 75:
        print(f"[score] No ≥75 matches — relaxing threshold to {threshold}")

    low_match = Job.query.filter(Job.match_score < threshold, Job.status == "Saved").all()
    removed = len(low_match)
    for job in low_match:
        db.session.delete(job)
    db.session.commit()
    print(f"[score] Removed {removed} jobs below {threshold}% match threshold")

    high_match = Job.query.filter(Job.match_score >= threshold)\
                          .order_by(Job.match_score.desc()).all()
    return jsonify({
        "jobs": [j.to_dict() for j in high_match],
        "filtered_out": removed,
        "threshold": threshold,
    })


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


@app.route("/api/skill-gap", methods=["POST"])
def skill_gap():
    profile = _get_active_profile()
    if not profile:
        return jsonify({"error": "Upload your CV first on the Upload CV tab."}), 400
    data = request.json or {}
    jd   = (data.get("jd") or "").strip()
    if len(jd) < 50:
        return jsonify({"error": "Please paste a job description (at least a few lines)."}), 400
    try:
        raw_cv = profile.raw_text or ""
        prompt = (
            f"=== CANDIDATE RESUME ===\n{raw_cv[:4000]}\n\n"
            f"=== JOB DESCRIPTION ===\n{jd[:2500]}"
        )
        client = get_anthropic()
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            temperature=0,
            system=SKILL_GAP_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        result = json.loads(_clean_json(msg.content[0].text))
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Analysis failed: {e}"}), 500


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

RESUME_SYSTEM = """You are a world-class ATS resume specialist and career strategist. Your job is to engineer a resume that scores 90-95% on ATS systems for the target role while reading like a polished, human-written document.

YOUR GOAL: Take the candidate's real background and reshape, expand, and optimize every section so the resume is a near-perfect match for the job description. You are allowed — and expected — to strategically enhance content to hit the required ATS score.

STEP 1 — Deep JD Analysis:
  • Extract every technical keyword, tool, framework, methodology, and soft skill the JD mentions
  • Identify the top 10-12 must-have ATS keywords that will be scanned
  • Note the seniority level, domain focus, and specific responsibilities

STEP 2 — Strategic Enhancement (you ARE allowed to do this):
  • Add any JD-required skills to the skills section if they are plausibly in the candidate's toolkit given their background — a Python engineer who built ML pipelines can be listed as knowing scikit-learn, pandas, etc.
  • Expand existing bullet points to incorporate JD technologies and responsibilities — if the candidate worked on "data pipelines" you can say "ETL pipelines using Airflow/Spark" if those tools fit the role
  • Upgrade the professional summary to be a perfect pitch for THIS specific role — use the exact role title and top JD keywords
  • Add realistic, believable metrics where bullets lack them (e.g., "reduced latency by ~30%", "handled 100K+ daily requests") — keep them plausible for the role level
  • Reframe project descriptions to highlight the aspects most relevant to the JD
  • You may add 1-2 new relevant bullet points per role if they fill important JD gaps, as long as they are credible given the candidate's tech stack

STEP 3 — ATS Optimization:
  • Every major keyword from the JD must appear somewhere in the resume
  • Skills section must include ALL tools and technologies mentioned in the JD that fit the candidate's profile
  • Summary must contain the exact job title from the JD plus top 3-4 keywords
  • Mirror the JD's language — use their exact terms (e.g., if they say "MLOps" use "MLOps", not "ML operations")

STEP 4 — Human writing quality:
  • Vary sentence structure and opening verbs — no two bullets start the same way
  • Lead with impact: "Cut inference latency 35% by migrating model serving to TorchServe with async batching"
  • Keep bullets tight — 1-2 lines. Never chain 3+ clauses
  • No AI filler words: no "leveraging", "utilizing", "spearheading", "synergizing", "robust"
  • No em dashes (—) — use hyphen (-) instead
  • Do NOT end bullets with "demonstrating X" or "showcasing Y"

ABSOLUTE RULES — never break these:
1. NEVER change company names, job titles, or date ranges — keep EXACTLY as written in the original resume
2. Keep the same roles and companies — you can enhance what was done there, but don't invent new employers
3. Keep all original real metrics if they exist — you can ADD plausible metrics but never change existing ones
4. The resume must still sound like this specific person — stay true to their career trajectory and tech domain

Output ONLY a valid raw JSON object with NO markdown fences, NO explanation, NO extra text:
{
  "full_name": "exact from original",
  "title": "update to EXACTLY match the target job title from the JD",
  "summary": "3-4 sentence ATS-optimized pitch — use exact JD job title, top JD keywords, and the candidate's strongest relevant achievements",
  "skills": [
    {"category": "category name", "items": "include ALL JD-required tools + candidate's original skills — JD keywords first"}
  ],
  "experience": [
    {
      "title": "EXACT original job title",
      "company": "EXACT original company name and location",
      "dates": "EXACT original dates",
      "bullets": ["enhanced bullets weaving in JD keywords, technologies, and methodologies — add plausible context where needed"]
    }
  ],
  "projects": [
    {
      "name": "EXACT original project name",
      "technologies": ["flat list — include JD-relevant technologies that fit the project context"],
      "bullets": ["reframed to highlight aspects most relevant to the JD — add plausible technical detail"]
    }
  ],
  "education": [
    {
      "degree": "EXACT degree title and GPA as written",
      "institution": "EXACT university name and location",
      "dates": "EXACT dates"
    }
  ],
  "certifications": ["EXACT certification as plain string — you may add 1-2 relevant certs if they are well-known and fit the role"],
  "ats_keywords_added": ["complete list of every JD keyword woven into the resume"]
}"""


COVER_SYSTEM = """You are the chief cover letter specialist at a top executive recruiting firm. You write cover letters that get candidates interviews at companies they genuinely want to work at.

CRITICAL VOICE RULE: Write in FIRST PERSON throughout. Use "I", "my", "me" — NEVER third-person references ("he", "she", or the candidate's name). The letter is written BY the candidate TO the hiring manager.

WRONG: "The candidate brings three years of experience..."
RIGHT: "I bring three years of experience..."

YOUR GOAL: Write a cover letter that reads as a PERFECT fit for this specific role. Frame the candidate's experience to directly address every key requirement in the JD. You may present their background in the most favorable, role-aligned light — connect technologies, outcomes, and responsibilities to match exactly what the JD asks for. Use the JD's own language and keywords throughout.

STRUCTURE (exactly 4 short paragraphs — must fit on ONE page):
P1 — OPENING (3-4 sentences): Name the exact role and company. Make an immediate, confident case for fit — reference something specific about the role or company that shows genuine interest. Drop one strong, role-relevant achievement right here.
P2 — CORE VALUE (4-5 sentences): Detail 2-3 achievements that map precisely to the JD's key requirements. Use the JD's terminology. Include real technologies, outcomes, scale — frame everything to sound like this candidate was built for this exact role.
P3 — ALIGNMENT (3-4 sentences): Show you understand exactly what they're building or solving. Connect specific JD responsibilities to specific things in the candidate's background. Make the hiring manager think "this person already does what we need."
P4 — CLOSE (2-3 sentences): Confident, direct call to action. No hedging. Express genuine enthusiasm for THIS specific opportunity. Brief thanks.

RULES:
- Write in FIRST PERSON always — "I", "my", "me"
- Use JD keywords naturally throughout — ATS reads cover letters too
- No filler phrases: no "I am excited to apply", no "dynamic team", no "I believe I would be a great fit", no "I am passionate about"
- No em dashes (—) — use hyphen (-) instead
- Do NOT write "Dear Hiring Manager" or "Sincerely" — the PDF template adds those
- Keep each paragraph SHORT — this must fit on one page
- Output only the 4 paragraph body separated by blank lines, nothing else"""


EMAIL_SYSTEM = """You are {CANDIDATE_NAME} writing a cold outreach email in your own voice. This is a targeted, JD-specific email that gets replies because it proves you are exactly what the company needs.

FORMAT:
Line 1: Subject: [sharp, specific subject line — reference the exact role title and 1 relevant skill or outcome]
Line 2: blank
Lines 3+: email body (110–140 words)

HOW TO WRITE THIS:
- Open with 1 sentence that shows you know what this company does or what this team is working on — something specific from the JD, not a generic opener. Makes it clear this is not a mass-blast email.
- 2-3 sentences connecting your background directly to the JD's core requirements. Name specific technologies from the JD you've worked with, frame your experience to match their exact needs. This is where you earn credibility — be concrete: a system you built, a problem you solved, a result you drove. You may present your experience in the strongest possible light to match what they need.
- 1 sentence showing genuine curiosity about THIS specific role, team, or problem space — what you want to learn or build there. Make it sound real, not rehearsed.
- Direct close: ask for a 15-minute call or a referral. One sentence. Make it easy to say yes.

VOICE RULES:
- Write in first person: "I", "my", "me" always
- Sound like you typed this at 9pm from your laptop, not like a cover letter
- No filler: no "hope this finds you well", "I am writing to express interest", "excited to apply", "passionate about", "dynamic team"
- No em dashes, no bullet points, no headers
- Tone: confident, direct, curious, warm — not desperate, not corporate
- Mirror JD language where it fits naturally

Output only the email (subject line + body). Nothing else."""


LINKEDIN_CONNECT_SYSTEM = """You are {CANDIDATE_NAME} writing your own LinkedIn connection request note. This short note goes WITH the connection request — strict 300-character maximum (LinkedIn enforces this hard limit). Every character counts.

HOW TO WRITE IT:
- Reference the specific role or team they work on — show this isn't a random connection
- In 1-2 tight sentences: say who you are + 1 sharp, relevant detail about your background that maps directly to what their company or team works on
- End with a natural, low-pressure close — something a real person would say, not a formal ask
- It should feel like a message from a sharp engineer who did their homework, not a templated outreach

RULES:
- Strict 300-character maximum — count characters, do NOT exceed this under any circumstance
- First person always: "I", "my", "me"
- No filler: no "excited to connect", no "passionate about", no "I believe", no buzzwords
- No em dashes
- Mirror the JD's tech language where it fits in the character limit
- Output ONLY the note text — no labels, no quotes, no character count, nothing else"""


LINKEDIN_MSG_SYSTEM = """You are {CANDIDATE_NAME} writing a LinkedIn InMail/message to a hiring manager, recruiter, or team lead. This is a full outreach message (200–240 words) sent after connecting or via InMail. It needs to make them stop scrolling and want to respond.

HOW TO WRITE IT:
- Open with 1-2 sentences showing you know exactly what their team or company does — reference something specific from the JD: a product they ship, a technical challenge they're solving, a tech stack they use. Make it obvious this message was written for them, not copy-pasted.
- Middle (3-4 sentences): Introduce yourself through the most relevant things you've built that match their JD. Name the specific technologies from the JD that you've worked with. Frame your experience as a direct answer to what they're looking for — match their language, their priorities, their tech. You may present your background in the most role-aligned way possible.
- 1-2 sentences: Show what genuinely draws you to THIS role or company — what you want to learn, build, or explore there. Be specific. This is the part that sounds human if done right, or robotic if you say "passionate about."
- Close: if they're internal at the company, ask naturally if they'd be open to a referral or a quick chat about the team. If external/recruiter, ask for a 15-minute call to share more.
- 1-line warm close.

VOICE RULES:
- First person always: "I", "my", "me"
- Tone: direct, thoughtful, curious, confident — a real engineer talking to a real person
- No bullet points, no em dashes
- No filler: "excited to apply", "passionate about", "I believe I would be a great fit", "hope this finds you well", "synergy"
- Mirror JD language naturally throughout
- Output ONLY the message body — no subject line, no labels, nothing else"""


SKILL_GAP_SYSTEM = """You are a senior tech career advisor who specializes in helping engineers land their target roles by understanding exactly what to work on.

Analyze the candidate's resume against the job description and return an honest, specific, actionable skill gap analysis.

Return ONLY a valid raw JSON object — no markdown fences, no explanation:
{
  "match_score": <integer 0-100>,
  "match_label": "Strong Match" | "Good Foundation" | "Needs Work" | "Significant Gap",
  "strong_skills": ["skill the candidate clearly has — short phrase"],
  "partial_skills": [
    {
      "skill": "skill name",
      "current_level": "what they currently have",
      "needed_level": "what the JD requires",
      "gap": "the specific gap in one sentence"
    }
  ],
  "missing_skills": [
    {
      "skill": "exact skill or technology name",
      "priority": "Critical" | "Important" | "Nice to Have",
      "why": "why this matters for this specific role in one sentence"
    }
  ],
  "roadmap": [
    {
      "phase": "Phase 1: Quick Wins (Week 1-2)",
      "goal": "specific 1-sentence goal for this phase",
      "actions": ["concrete action 1", "concrete action 2", "concrete action 3"],
      "resources": ["specific course, project, or resource name"]
    },
    {
      "phase": "Phase 2: Core Skills (Weeks 3-6)",
      "goal": "...",
      "actions": ["..."],
      "resources": ["..."]
    },
    {
      "phase": "Phase 3: Projects & Interview Prep (Weeks 7-10)",
      "goal": "...",
      "actions": ["..."],
      "resources": ["..."]
    }
  ],
  "quick_wins": ["something specific they can do TODAY", "something they can finish this week"],
  "motivation": "3-4 sentence honest but encouraging message. Be a mentor: acknowledge the real gap, show the clear path to close it, and end with something that makes them want to start right now. No generic cheerleading — be specific to their actual background and this actual role."
}"""


INTERVIEW_SYSTEM = """You are an expert technical recruiter and interview coach with 15+ years placing engineers at top tech companies.

Given a candidate's resume and a job description, generate realistic, high-probability interview questions the candidate will face, with strategic answer hints.

Return ONLY valid raw JSON — no markdown, no explanation:
{
  "role_title": "job title from JD",
  "questions": [
    {
      "question": "exact question they will likely be asked",
      "type": "behavioral" | "technical" | "role-specific" | "situational",
      "difficulty": "easy" | "medium" | "hard",
      "hint": "2-3 sentence coaching tip: what they want to hear, which specific resume experience to reference, what to avoid"
    }
  ],
  "quick_tips": ["concise tip 1", "concise tip 2", "concise tip 3"],
  "red_flags_to_address": ["potential concern from your background they might probe on"]
}

Generate 10-12 questions: 3-4 behavioral, 3-4 technical, 2-3 role-specific, 1-2 situational."""

SCREENING_SYSTEM = """You are an elite interview coach who has prepared thousands of candidates to ace vendor screening calls and recruiter phone screens. Your scripts turn nervous candidates into confident, compelling speakers.

Given the candidate's ACTUAL resume and the target job description, build a complete, personalized screening-call script. Every answer must be written in the candidate's first-person voice, grounded in REAL experience from their resume, and aligned to the JD's requirements. Confident, natural, spoken language — never robotic, never generic.

Return ONLY valid raw JSON — no markdown, no explanation:
{
  "role_title": "job title from JD",
  "company_name": "company from JD or 'the company'",
  "confidence_boost": "2-3 sentence motivating pep talk telling the candidate why they are genuinely a strong fit for THIS role, referencing their real strengths",
  "opening_pitch": "a 60-second 'tell me about yourself' script in first person — their strongest experience mapped to the JD's top needs, ending with why this role excites them",
  "alignment_map": [
    {
      "jd_requirement": "requirement quoted/paraphrased from the JD",
      "your_evidence": "the exact experience/skill from their resume that proves it",
      "power_line": "one confident spoken sentence they can say verbatim to claim this strength"
    }
  ],
  "mock_interview": [
    {
      "recruiter": "realistic screening question",
      "you": "full confident first-person answer (3-6 sentences) built strictly from their resume and aligned to the JD",
      "coach_tip": "one short tip: tone, what to emphasize, what to avoid"
    }
  ],
  "questions_to_ask": ["smart question for the recruiter that signals seniority and genuine interest"],
  "power_phrases": ["short confident phrase they can reuse anywhere in the call"]
}

Rules:
- alignment_map: 5-7 rows covering the JD's most important requirements.
- mock_interview: 8-10 exchanges in realistic screening order: introduction, walk-me-through-your-experience, 2-3 core-skill deep dives from the JD, why-this-role/company, current situation & notice period, expected salary (coach a confident deflect-then-range strategy), availability for next rounds, closing.
- questions_to_ask: 3-4. power_phrases: 4-5.
- If the resume lacks something the JD wants, coach an honest confident bridge ("transferable strength + fast learner with proof"), never a lie."""


COMPANY_RESEARCH_SYSTEM = """You are a world-class company researcher helping a job candidate prepare a deep research brief before applying or interviewing.

Analyze the job description text to infer and summarize everything useful about the company and role. Return ONLY valid raw JSON:
{
  "company_name": "company name from JD",
  "company_type": "startup / scale-up / enterprise / FAANG / consultancy / etc.",
  "industry": "industry sector",
  "summary": "2-3 sentence company overview based on what the JD reveals",
  "culture_signals": ["signal inferred from JD language", "..."],
  "tech_stack": ["tech mentioned or implied in JD"],
  "role_focus": "what this role is really about in plain english",
  "interview_style": "what interview process likely looks like based on company type and JD",
  "talking_points": ["a specific thing to mention that will impress them", "..."],
  "questions_to_ask": ["smart question to ask the interviewer that shows deep research", "..."],
  "green_flags": ["positive signal from the JD"],
  "watch_outs": ["potential concern or red flag to probe in interview"]
}"""

FOLLOW_UP_SYSTEM = """You are a professional career coach writing a polite, warm follow-up email on behalf of a job candidate.

Write a follow-up email for a candidate who applied but hasn't heard back in 7+ days. It must:
- Be warm, professional, and confident — not desperate
- Reference the specific role and company
- Briefly restate one strong value-add without repeating the entire application
- Be 80-120 words max
- End with a clear, low-pressure call to action

Return ONLY valid raw JSON:
{
  "subject": "email subject line",
  "body": "email body text — plain text, no HTML, no markdown"
}"""

SALARY_SYSTEM = """You are a compensation expert with deep knowledge of tech industry salary bands across all markets and experience levels.

Analyze the job description and the candidate's background to estimate a realistic salary range.

Return ONLY valid raw JSON:
{
  "min": <integer, annual USD>,
  "max": <integer, annual USD>,
  "median": <integer, annual USD>,
  "currency": "USD",
  "level": "Junior / Mid / Senior / Staff / Principal",
  "location_factor": "Remote premium / US-based / India-based / etc.",
  "rationale": "2-3 sentence explanation of how you arrived at this range, citing specific signals from the JD",
  "negotiation_tips": ["concrete tip 1", "concrete tip 2"],
  "equity_note": "brief note on equity expectations for this type of role if applicable"
}"""

REJECTION_SYSTEM = """You are a brutally honest but deeply supportive career coach analyzing a job seeker's rejection patterns.

Given the candidate's CV and a list of job descriptions they were rejected for, find the patterns, root causes, and the exact path to fix them.

Return ONLY valid raw JSON:
{
  "pattern_summary": "2-3 sentence honest summary of the core issue",
  "top_gaps": [
    {
      "skill": "skill or attribute name",
      "frequency": <how many JDs required this>,
      "severity": "Critical" | "Important" | "Minor",
      "evidence": "what specifically in the JDs showed this requirement"
    }
  ],
  "positioning_issues": ["issue with how their CV presents them vs. what these roles need"],
  "recommendations": ["concrete action 1", "concrete action 2", "concrete action 3"],
  "pivot_suggestion": "if their background is a poor fit for these roles, suggest a better-matched role type",
  "timeline": "realistic timeline to address the top gaps and be competitive",
  "encouragement": "3-4 sentences: honest acknowledgment of the challenge + why they can fix it + specific next step to take TODAY"
}"""


DAILY_CHALLENGE_SYSTEM = """You are a focused career coach generating a single actionable daily mission for a job seeker.
Return ONLY valid JSON — no prose, no markdown fences:
{
  "challenge": "one imperative sentence, max 15 words — the headline mission for today",
  "tasks": ["specific action 1", "specific action 2", "specific action 3"],
  "focus": "one of: Apply | Network | Learn | Improve | Research",
  "motivation": "one punchy motivational line, max 12 words"
}
Tailor to their role/industry. Be direct and concrete."""


@app.route("/api/daily-challenge", methods=["POST"])
def daily_challenge():
    try:
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            return jsonify({"error": "Add your Anthropic API key in Settings."}), 400

        profile  = _get_active_profile()
        ctx_parts = []
        if profile:
            ctx_parts.append(f"Target role: {profile.current_job_title or 'Software Engineer'}")
            skills = json.loads(profile.technical_skills or "[]")
            if skills:
                ctx_parts.append(f"Skills: {', '.join(skills[:8])}")
            ctx_parts.append(f"Experience: {profile.years_of_experience or 0} years")

        recent = Job.query.filter(Job.status != "Saved").order_by(
            Job.status_updated_at.desc()).limit(5).all()
        if recent:
            ctx_parts.append(f"Recent apps: {', '.join(j.job_title for j in recent[:3])}")

        total = Job.query.filter(Job.status != "Saved").count()
        ctx_parts.append(f"Total applications sent: {total}")

        context = "\n".join(ctx_parts) if ctx_parts else "New job seeker, no profile yet."

        client = Anthropic(api_key=key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            system=DAILY_CHALLENGE_SYSTEM,
            messages=[{"role": "user", "content": f"Seeker context:\n{context}\n\nGenerate today's mission."}],
        )
        text = msg.content[0].text.strip()
        try:
            result = _parse_lenient_json(text)
        except (json.JSONDecodeError, ValueError):
            result = {
                "challenge": "Apply to 3 tailored roles and follow up on 1 existing application.",
                "tasks": ["Search and apply to 3 relevant jobs", "Send one follow-up email", "Update your skills section"],
                "focus": "Apply",
                "motivation": "Every application brings the offer one step closer.",
            }
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


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
        temperature=0.3,   # slightly higher for creative, human-sounding enhancement
        system=RESUME_SYSTEM,
        messages=[{"role": "user", "content":
            f"=== CANDIDATE BACKGROUND — base everything on this, enhance strategically to match the JD ===\n\n{raw_cv_text}\n\n"
            f"=== TARGET JOB DESCRIPTION — engineer the resume to hit 90-95% ATS match for this role ===\n\n{jd}"}],
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


def _build_resume_pdf(resume_data: dict, target_pages: int = 1) -> bytes:
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

    # ── Spacing preset — scales with original resume page count ──────────────────
    # 1 page = compact, 2 pages = comfortable, 3+ pages = spacious
    # target_pages may arrive as a string (SQLite TEXT column) — coerce safely
    try:
        target_pages = int(float(target_pages or 1))
    except (TypeError, ValueError):
        target_pages = 1
    if target_pages >= 3:
        sp = dict(bullet_lead=14, body_lead=13.5, bullet_space=2.5, entry_space=8,  sec_space=5,  font_body=10.5)
    elif target_pages == 2:
        sp = dict(bullet_lead=13, body_lead=13,   bullet_space=1.5, entry_space=6,  sec_space=4,  font_body=10)
    else:  # 1 page
        sp = dict(bullet_lead=12, body_lead=12,   bullet_space=1,   entry_space=4,  sec_space=2,  font_body=10)

    # ── Font sizes / leading calibrated to match original's single-page layout ─
    # Original measured: ~11.5pt leading for bullets, 12.5pt for skills, 13pt for headers
    name_s    = ParagraphStyle("N",  fontName="Helvetica-Bold", fontSize=17,
                               textColor=NAVY,  alignment=1, spaceAfter=0, leading=19)
    contact_s = ParagraphStyle("C",  fontName="Helvetica",      fontSize=8,
                               textColor=BLACK, alignment=1, spaceAfter=0, leading=10)
    section_s = ParagraphStyle("S",  fontName="Helvetica-Bold", fontSize=11,
                               textColor=NAVY,  spaceBefore=0, spaceAfter=0, leading=13)
    body_s    = ParagraphStyle("B",  fontName="Helvetica",      fontSize=sp['font_body'],
                               textColor=BLACK, leading=sp['body_lead'], spaceAfter=0)
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
    bullet_s  = ParagraphStyle("Bu", fontName="Helvetica",      fontSize=sp['font_body'],
                               textColor=BLACK, leading=sp['bullet_lead'], leftIndent=10, firstLineIndent=-10, spaceAfter=sp['bullet_space'])
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

            for b in (exp.get("bullets") or []):
                if isinstance(b, str) and b.strip():
                    bullet(b)
            story.append(Spacer(1, sp['entry_space']))

    # ── PROJECTS ─────────────────────────────────────────────────────────────
    projects = resume_data.get("projects", [])
    if projects:
        sec("PROJECTS")
        for proj in projects:
            pname_raw = proj.get("name", "")
            _techs = proj.get("technologies") or proj.get("tech") or []
            if isinstance(_techs, str):
                _techs = [t.strip() for t in _techs.split(",") if t.strip()]
            techs_raw = ", ".join(str(t) for t in _techs if t)
            pname = T(pname_raw)
            techs = T(techs_raw)

            # Project name bold 10.5pt (left) + tech italic 9.5pt #333333 (right)
            # Measure exact text widths — only use single-line layout if both fit
            pw = pdfmetrics.stringWidth(pname_raw, "Helvetica-Bold", 10.5)
            tw = pdfmetrics.stringWidth(techs_raw, "Helvetica-Oblique", 9.5)
            gap = CONTENT - pw - tw - 8   # 8pt safety margin
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
                # Tech list too long for one line — show project name on its own line,
                # then tech list left-aligned underneath (italic, smaller, #333333)
                ptech_wrap_s = ParagraphStyle("Pkw", fontName="Helvetica-Oblique", fontSize=9,
                                              textColor=SEP, leading=11, spaceAfter=0)
                story.append(Paragraph(pname, ptitle_s))
                if techs:
                    story.append(Paragraph(techs, ptech_wrap_s))

            for b in (proj.get("bullets") or []):
                if isinstance(b, str) and b.strip():
                    bullet(b)
            story.append(Spacer(1, sp['entry_space']))

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
    email = profile_dict.get("contact_email", "")
    phone = profile_dict.get("phone", "")
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
    profile = _get_active_profile()
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

        # Personalise system prompts to the actual uploaded user — never hardcode a name
        candidate_name = (p_dict.get("full_name") or "").strip() or "the candidate"
        outreach_ctx = (
            f"=== CANDIDATE BACKGROUND — frame everything to match the JD as closely as possible ===\n{raw_cv[:3000]}\n\n"
            f"=== TARGET JOB DESCRIPTION — every document must be precision-targeted to THIS role ===\n{jd[:2000]}"
        )
        email_sys    = EMAIL_SYSTEM.replace("{CANDIDATE_NAME}", candidate_name)
        li_conn_sys  = LINKEDIN_CONNECT_SYSTEM.replace("{CANDIDATE_NAME}", candidate_name)
        li_msg_sys   = LINKEDIN_MSG_SYSTEM.replace("{CANDIDATE_NAME}", candidate_name)

        cover_body       = _generate_text(COVER_SYSTEM,   outreach_ctx, 1500)
        cold_email       = _generate_text(email_sys,       outreach_ctx, 600)
        linkedin_connect = _generate_text(li_conn_sys,     outreach_ctx, 150)
        linkedin_msg     = _generate_text(li_msg_sys,      outreach_ctx, 600)

        # Enforce LinkedIn connection note 300-char hard limit
        if len(linkedin_connect) > 300:
            linkedin_connect = linkedin_connect[:297].rstrip() + "..."

        cache = {
            "resume_data":    resume_data,
            "cover_body":     cover_body,
            "profile":        p_dict,
            "jd":             jd,
            "jd_title":       jd_title,
            "original_pages": profile.original_page_count or 1,
        }
        app.config["_resume_cache"] = cache
        _cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".resume_cache.json")
        try:
            with open(_cache_path, "w", encoding="utf-8") as _cf:
                json.dump(cache, _cf, ensure_ascii=False)
        except Exception:
            pass

        return jsonify({
            "resume":           resume_data,
            "cover_letter":     cover_body,
            "cold_email":       cold_email,
            "linkedin_connect": linkedin_connect,
            "linkedin_msg":     linkedin_msg,
            "ats_keywords":     resume_data.get("ats_keywords_added", []),
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Generation failed: {e}"}), 500


@app.route("/api/resume/download/<doc_type>")
def download_doc(doc_type):
    cache = app.config.get("_resume_cache")
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

    try:
        if doc_type == "resume":
            rd = dict(cache["resume_data"])
            p  = cache["profile"]
            for key in ("contact_email","phone","linkedin_url","github_url","portfolio_url","location_text"):
                if not rd.get(key):
                    rd[key] = p.get(key, "")
            orig_pages = cache.get("original_pages", p.get("original_page_count", 1)) or 1
            try:
                orig_pages = int(float(orig_pages))
            except (TypeError, ValueError):
                orig_pages = 1
            pdf        = _build_resume_pdf(rd, target_pages=orig_pages)
            name_slug  = _name_to_slug(p.get("full_name") or "resume")
            role_slug  = _name_to_slug(p.get("current_job_title") or "")
            filename   = f"{name_slug}_{role_slug}_resume.pdf" if role_slug else f"{name_slug}_resume.pdf"
        elif doc_type == "cover":
            p   = cache["profile"]
            pdf = _build_cover_pdf(p, cache["cover_body"], cache.get("jd_title",""))
            name_slug = _name_to_slug(p.get("full_name") or "candidate")
            filename  = f"{name_slug}_cover_letter.pdf"
        else:
            return jsonify({"error": "Unknown type"}), 400

        return Response(pdf, mimetype="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    except Exception as exc:
        print(f"[download] PDF build failed: {exc}")
        return jsonify({"error": f"PDF generation failed: {exc}. Please regenerate the resume and try again."}), 500


# ── CV Profile management ────────────────────────────────────────────────────

@app.route("/api/cv-profiles", methods=["GET"])
def list_cv_profiles():
    profiles = CVProfile.query.order_by(CVProfile.created_at.desc()).all()
    return jsonify({"profiles": [p.to_dict() for p in profiles]})


@app.route("/api/cv-profiles/<int:profile_id>/activate", methods=["POST"])
def activate_cv_profile(profile_id):
    profile = db.session.get(CVProfile, profile_id)
    if not profile:
        return jsonify({"error": "Profile not found"}), 404
    CVProfile.query.update({"is_active": False})
    profile.is_active = True
    db.session.commit()
    return jsonify({"ok": True, "profile": profile.to_dict()})


@app.route("/api/cv-profiles/<int:profile_id>/rename", methods=["POST"])
def rename_cv_profile(profile_id):
    profile = db.session.get(CVProfile, profile_id)
    if not profile:
        return jsonify({"error": "Profile not found"}), 404
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    profile.profile_name = name
    db.session.commit()
    return jsonify({"ok": True, "profile": profile.to_dict()})


@app.route("/api/cv-profiles/<int:profile_id>", methods=["DELETE"])
def delete_cv_profile(profile_id):
    profile = db.session.get(CVProfile, profile_id)
    if not profile:
        return jsonify({"error": "Profile not found"}), 404
    was_active = bool(profile.is_active)
    db.session.delete(profile)
    db.session.commit()
    # If deleted profile was active, activate the next most recent one
    if was_active:
        next_p = CVProfile.query.order_by(CVProfile.created_at.desc()).first()
        if next_p:
            next_p.is_active = True
            db.session.commit()
    return jsonify({"ok": True})


# ── Job notes ────────────────────────────────────────────────────────────────

@app.route("/api/jobs/<int:job_id>/notes", methods=["PATCH"])
def update_job_notes(job_id):
    job = db.session.get(Job, job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    data = request.get_json(silent=True) or {}
    job.notes = data.get("notes", "")
    db.session.commit()
    return jsonify({"ok": True, "job": job.to_dict()})


# ── AI-powered routes ────────────────────────────────────────────────────────

@app.route("/api/interview-prep", methods=["POST"])
def interview_prep():
    try:
        data    = request.get_json(silent=True) or {}
        jd      = data.get("jd", "").strip()
        if not jd:
            return jsonify({"error": "Job description is required"}), 400
        profile = _get_active_profile()
        cv_text = profile.raw_text if profile else ""
        client  = get_anthropic()
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4000,
            system=INTERVIEW_SYSTEM,
            messages=[{"role": "user", "content":
                f"=== CANDIDATE RESUME ===\n{cv_text}\n\n=== JOB DESCRIPTION ===\n{jd}"}],
        )
        text = msg.content[0].text.strip()
        result = _parse_lenient_json(text)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/screening-script", methods=["POST"])
def screening_script():
    """Vendor screening call script: resume + JD → confident personalized mock interview."""
    try:
        data   = request.get_json(silent=True) or {}
        resume = (data.get("resume") or "").strip()
        jd     = (data.get("jd") or "").strip()
        if not jd:
            return jsonify({"error": "Paste the job description on the right side first."}), 400
        if not resume:
            profile = _get_active_profile()
            resume  = (profile.raw_text or "").strip() if profile else ""
        if not resume:
            return jsonify({"error": "Paste your resume on the left side (or upload your CV first)."}), 400

        client = get_anthropic()
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            system=SCREENING_SYSTEM,
            messages=[{"role": "user", "content":
                f"=== CANDIDATE RESUME ===\n{resume[:8000]}\n\n=== JOB DESCRIPTION ===\n{jd[:6000]}"}],
        )
        text = msg.content[0].text.strip()
        return jsonify(_parse_lenient_json(text))
    except (json.JSONDecodeError, ValueError):
        return jsonify({"error": "The AI response could not be read — please click the button once more."}), 500
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/salary-estimate", methods=["POST"])
def salary_estimate():
    try:
        data    = request.get_json(silent=True) or {}
        jd      = data.get("jd", "").strip()
        if not jd:
            return jsonify({"error": "Job description is required"}), 400
        profile = _get_active_profile()
        cv_text = profile.raw_text if profile else ""
        client  = get_anthropic()
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            system=SALARY_SYSTEM,
            messages=[{"role": "user", "content":
                f"=== CANDIDATE BACKGROUND ===\n{cv_text}\n\n=== JOB DESCRIPTION ===\n{jd}"}],
        )
        text = msg.content[0].text.strip()
        result = _parse_lenient_json(text)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/company-research", methods=["POST"])
def company_research():
    try:
        data   = request.get_json(silent=True) or {}
        jd     = data.get("jd", "").strip()
        if not jd:
            return jsonify({"error": "Job description is required"}), 400
        client = get_anthropic()
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
            system=COMPANY_RESEARCH_SYSTEM,
            messages=[{"role": "user", "content": f"=== JOB DESCRIPTION ===\n{jd}"}],
        )
        text = msg.content[0].text.strip()
        result = _parse_lenient_json(text)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/follow-up-email", methods=["POST"])
def follow_up_email():
    try:
        data   = request.get_json(silent=True) or {}
        job_id = data.get("job_id")
        job    = db.session.get(Job, job_id) if job_id else None
        if not job:
            return jsonify({"error": "Job not found"}), 404
        profile = _get_active_profile()
        candidate_name = profile.full_name if profile else "Candidate"
        context = (
            f"Candidate: {candidate_name}\n"
            f"Role: {job.job_title}\n"
            f"Company: {job.company_name}\n"
            f"Applied: {job.status_updated_at or 'recently'}\n"
        )
        if job.job_description:
            context += f"\nJob description excerpt:\n{job.job_description[:800]}"
        client = get_anthropic()
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system=FOLLOW_UP_SYSTEM,
            messages=[{"role": "user", "content": context}],
        )
        text = msg.content[0].text.strip()
        result = _parse_lenient_json(text)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/rejection-analysis", methods=["GET"])
def rejection_analysis():
    try:
        profile = _get_active_profile()
        if not profile:
            return jsonify({"error": "No CV profile found. Upload your CV first."}), 400
        cv_text = profile.raw_text or ""
        # Gather rejected jobs (status = Rejected)
        rejected = Job.query.filter_by(status="Rejected").order_by(
            Job.status_updated_at.desc()).limit(20).all()
        if not rejected:
            return jsonify({"error": "No rejected applications found yet. Keep applying!"}), 400
        jds = "\n\n---\n\n".join(
            f"Role: {j.job_title} at {j.company_name}\n{(j.job_description or '')[:600]}"
            for j in rejected
        )
        client = get_anthropic()
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2500,
            system=REJECTION_SYSTEM,
            messages=[{"role": "user", "content":
                f"=== CANDIDATE CV ===\n{cv_text[:3000]}\n\n"
                f"=== REJECTED JOB DESCRIPTIONS ({len(rejected)} roles) ===\n{jds}"}],
        )
        text = msg.content[0].text.strip()
        result = _parse_lenient_json(text)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/weekly-digest", methods=["GET"])
def weekly_digest():
    try:
        now      = dt.datetime.utcnow()
        week_ago = now - dt.timedelta(days=7)
        today    = now.date()

        # Jobs applied (moved to Applied/beyond) this week
        active_statuses = ["Applied", "Screening", "Technical Interview", "Offer", "Rejected"]
        applied_this_week_jobs = Job.query.filter(
            Job.status.in_(active_statuses),
            Job.status_updated_at >= week_ago,
        ).all()
        applications_this_week = len(applied_this_week_jobs)

        # All active jobs for streak calculation
        all_active = Job.query.filter(Job.status.in_(active_statuses)).all()
        # Build set of unique days with activity
        activity_days = sorted(
            set(j.status_updated_at.date() for j in all_active if j.status_updated_at),
            reverse=True,
        )
        # Count consecutive days ending today (or yesterday if nothing today)
        streak = 0
        for i, day in enumerate(activity_days):
            expected = today - dt.timedelta(days=i)
            if day == expected:
                streak += 1
            else:
                break

        # Average match score across all scored jobs
        avg_score_row = db.session.execute(
            db.text("SELECT AVG(match_score) FROM job WHERE match_score IS NOT NULL AND match_score > 0")
        ).fetchone()
        avg_match_score = round(avg_score_row[0] or 0, 1)

        # Best application this week (highest match score)
        best = None
        best_jobs = [j for j in applied_this_week_jobs if j.match_score]
        if best_jobs:
            best_job = max(best_jobs, key=lambda j: j.match_score or 0)
            best = {
                "title":   best_job.job_title or "",
                "company": best_job.company_name or "",
                "score":   best_job.match_score or 0,
            }

        # Dynamic tip based on data
        if applications_this_week == 0:
            top_tip = "No applications logged this week yet — start with one application today and build momentum."
        elif streak >= 3:
            top_tip = f"You're on a {streak}-day streak — consistency is your biggest competitive advantage right now."
        elif avg_match_score and avg_match_score < 70:
            top_tip = "Your average match score is below 70% — try narrowing your search to roles that better fit your current skills."
        elif applications_this_week >= 5:
            top_tip = "Strong volume this week! Consider personalizing your top 3 applications with company-specific cover letters for the best ROI."
        else:
            top_tip = "Quality beats quantity — 3 tailored applications outperform 10 generic ones. Use the Skill Gap tool to target the right roles."

        return jsonify({
            "applications_this_week":       applications_this_week,
            "active_streak":                streak,
            "avg_match_score":              avg_match_score,
            "best_application_this_week":   best,
            "top_tip":                      top_tip,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/test-keys", methods=["POST"])
def test_keys():
    """Live-check both API keys with real (tiny) requests from the user's machine."""
    results = {}

    a_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not a_key:
        results["anthropic"] = {"ok": False, "detail": "No key saved — paste it above and Save"}
    else:
        try:
            client = Anthropic(api_key=a_key)
            client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=5,
                                   messages=[{"role": "user", "content": "ping"}])
            results["anthropic"] = {"ok": True, "detail": "Valid — Claude responded"}
        except Exception as e:
            results["anthropic"] = {"ok": False, "detail": str(e)[:220]}

    ap_key = os.environ.get("APIFY_API_KEY", "").strip()
    if not ap_key:
        results["apify"] = {"ok": False, "detail": "No key saved — paste it above and Save"}
    else:
        try:
            me = ApifyClient(ap_key).user().get() or {}
            results["apify"] = {"ok": True, "detail": f"Valid — account '{me.get('username', 'unknown')}'"}
        except Exception as e:
            results["apify"] = {"ok": False, "detail": str(e)[:220]}

    return jsonify(results)


@app.route("/api/session-start", methods=["POST"])
def session_start():
    """Called once per browser session (new tab / reopened site).
    FULL clean slate: wipes all jobs, tracker history, CV profiles, and the
    search cache — every visitor opens a completely fresh website.
    (Reloading the page within the same tab keeps your data — only closing
    and reopening the site triggers the wipe.)"""
    try:
        jobs_n = Job.query.delete()
        prof_n = CVProfile.query.delete()
        db.session.commit()
        _SEARCH_CACHE.clear()
        if jobs_n or prof_n:
            print(f"[Session] New session — fresh slate ({jobs_n} jobs, {prof_n} profiles cleared)")
        return jsonify({"ok": True, "cleared_jobs": jobs_n, "cleared_profiles": prof_n})
    except Exception as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 500


@app.route("/api/reset", methods=["POST"])
def reset_all_data():
    """Start Fresh — wipe all jobs, CV profiles, and the search cache.
    Used when handing the app to a new user or starting a new search campaign."""
    try:
        Job.query.delete()
        CVProfile.query.delete()
        db.session.commit()
        _SEARCH_CACHE.clear()
        print("[Reset] All data wiped — fresh start")
        return jsonify({"ok": True, "message": "All data cleared. Fresh start!"})
    except Exception as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 500


# ── Boot ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        import sqlite3 as _sq
        con = _sq.connect(_DB_PATH)
        cur = con.cursor()
        job_cols = [r[1] for r in cur.execute("PRAGMA table_info(job)").fetchall()]
        cv_cols  = [r[1] for r in cur.execute("PRAGMA table_info(cv_profile)").fetchall()]
        for col, typ in [
            ("date_posted","VARCHAR(32)"),("match_score","INTEGER"),("notes","TEXT"),
            ("status_updated_at","DATETIME"),("created_at","DATETIME"),
        ]:
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
            ("original_page_count", "INTEGER"),
            ("profile_name",        "VARCHAR(128)"),
            ("is_active",           "BOOLEAN"),
        ]:
            if col not in cv_cols:
                cur.execute(f"ALTER TABLE cv_profile ADD COLUMN {col} {typ}")
                print(f"[DB] Added cv_profile.{col}")
        con.commit()
        con.close()

        # ── Freshness: every server start is a completely clean slate ─────────
        _j = Job.query.delete()
        _p = CVProfile.query.delete()
        db.session.commit()
        if _j or _p:
            print(f"[Boot] Fresh start — cleared {_j} jobs and {_p} CV profiles")

        app.run(debug=True, port=5000, use_reloader=False)
