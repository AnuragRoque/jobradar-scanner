#!/usr/bin/env python3
"""
JobRadar - a local arrivals board for jobs at the companies you actually want.

Zero dependencies. Python 3.9+. Everything stays on your machine.

  python jobradar.py add https://job-boards.greenhouse.io/somecompany
  python jobradar.py resolve companies.json      # bulk-guess ATS slugs from a name list
  python jobradar.py fetch                       # poll every board once
  python jobradar.py serve                       # dashboard at http://localhost:8765

Commands:
  add <careers-url> [--name NAME]   detect the ATS behind a careers page, verify, save
  bulk <file.txt>                   one careers URL per line
  resolve <names.json>              try slug patterns across 6 ATS platforms for each name
  fetch                             poll all sources once, score, merge into the store
  serve [--port N] [--every MIN]    dashboard + background polling
  doctor                            re-check every configured source
  list                              show configured sources
"""

import argparse
import concurrent.futures as futures
import html
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
COMPANIES = os.path.join(ROOT, "companies.json")
PROFILE = os.path.join(ROOT, "profile.json")
STORE = os.path.join(ROOT, "data", "store.json")
DASHBOARD = os.path.join(ROOT, "dashboard.html")
DB_PAGE = os.path.join(ROOT, "db.html")
PROFILE_PAGE = os.path.join(ROOT, "profile.html")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TIMEOUT = 45
_SSL = ssl.create_default_context()
_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# tiny http helpers
# --------------------------------------------------------------------------- #

def _req(url, data=None, headers=None):
    hdrs = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    hdrs.update(headers or {})
    r = urllib.request.Request(url, data=data, headers=hdrs)
    for attempt in range(2):                 # one retry on transient failures
        try:
            with urllib.request.urlopen(r, timeout=TIMEOUT, context=_SSL) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError:
            raise                            # real 4xx/5xx: don't retry
        except (TimeoutError, urllib.error.URLError, ConnectionError):
            if attempt:                      # already retried once, give up
                raise
            time.sleep(1.5)


def get_json(url, data=None):
    return json.loads(_req(url, data=data))


def strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"[ \t]+", " ", s).strip()


def iso(dt):
    if isinstance(dt, (int, float)):
        if dt > 1e11:           # milliseconds
            dt = dt / 1000.0
        dt = datetime.fromtimestamp(dt, tz=timezone.utc)
    if isinstance(dt, str):
        s = dt.strip().replace("Z", "+00:00")
        for fmt in (None, "%Y-%m-%d", "%d %b %Y", "%b %d, %Y"):
            try:
                dt = datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
                break
            except Exception:
                continue
        else:
            return None
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def hours_since(iso_str):
    if not iso_str:
        return None
    try:
        d = datetime.fromisoformat(iso_str)
    except Exception:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - d).total_seconds() / 3600.0


# --------------------------------------------------------------------------- #
# ATS adapters -- each returns a list of normalised dicts
# --------------------------------------------------------------------------- #

def _job(src, company, jid, title, location, url, posted, desc, team=None):
    return {
        "key": f"{src}:{company}:{jid}",
        "source": src,
        "company": company,
        "title": (title or "").strip(),
        "location": (location or "").strip() or "Not stated",
        "url": url,
        "posted_at": posted,
        "team": team or "",
        "description": (desc or "")[:6000],
    }


def ats_greenhouse(c):
    slug = c["slug"]
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    for j in d.get("jobs", []):
        out.append(_job(
            "greenhouse", c["name"], j["id"], j.get("title"),
            (j.get("location") or {}).get("name"),
            j.get("absolute_url"),
            iso(j.get("first_published") or j.get("updated_at")),
            strip_html(j.get("content", "")),
            ", ".join(x.get("name", "") for x in (j.get("departments") or [])),
        ))
    return out


def ats_lever(c):
    slug = c["slug"]
    d = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in d:
        cat = j.get("categories") or {}
        out.append(_job(
            "lever", c["name"], j.get("id"), j.get("text"),
            cat.get("location"),
            j.get("hostedUrl"),
            iso(j.get("createdAt")),
            j.get("descriptionPlain") or strip_html(j.get("description", "")),
            cat.get("team"),
        ))
    return out


def ats_ashby(c):
    slug = c["slug"]
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    out = []
    for j in d.get("jobs", []):
        out.append(_job(
            "ashby", c["name"], j.get("id"), j.get("title"),
            j.get("location"),
            j.get("jobUrl") or j.get("applyUrl"),
            iso(j.get("publishedAt")),
            j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
            j.get("department"),
        ))
    return out


def ats_smartrecruiters(c):
    slug = c["slug"]
    d = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    out = []
    for j in d.get("content", []):
        loc = j.get("location") or {}
        where = ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
        out.append(_job(
            "smartrecruiters", c["name"], j.get("id"), j.get("name"), where,
            f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
            iso(j.get("releasedDate")),
            "",
            (j.get("department") or {}).get("label"),
        ))
    return out


def ats_recruitee(c):
    slug = c["slug"]
    d = get_json(f"https://{slug}.recruitee.com/api/offers/")
    out = []
    for j in d.get("offers", []):
        where = ", ".join(x for x in [j.get("city"), j.get("country")] if x)
        out.append(_job(
            "recruitee", c["name"], j.get("id"), j.get("title"), where or j.get("location"),
            j.get("careers_url") or j.get("careers_apply_url"),
            iso(j.get("published_at")),
            strip_html(j.get("description", "")),
            j.get("department"),
        ))
    return out


def ats_workable(c):
    slug = c["slug"]
    d = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    out = []
    for j in d.get("jobs", []):
        out.append(_job(
            "workable", c["name"], j.get("shortcode"), j.get("title"),
            j.get("location") or ", ".join(
                x for x in [j.get("city"), j.get("country")] if x),
            j.get("url") or j.get("application_url"),
            iso(j.get("published_on")),
            strip_html(j.get("description", "")),
            j.get("department"),
        ))
    return out


def ats_workday(c):
    """Needs the cxs endpoint, e.g.
       https://co.wd3.myworkdayjobs.com/wday/cxs/co/Careers/jobs"""
    cxs = c["url"].rstrip("/")
    apply_base = c.get("apply_base") or re.sub(r"/wday/cxs/[^/]+/([^/]+)/jobs$",
                                               r"/\1", cxs)
    # Big Workday tenants (Nvidia, Adobe, ...) have thousands of global roles.
    # Without a search term we'd only ever see the first N (mostly US) postings
    # and almost no India roles survive the location gate. "search": "India" in
    # the source config narrows the query server-side so we actually reach them.
    search = c.get("search", "")
    max_pages = c.get("max_pages", 15)       # 15 * 20 = up to 300 postings
    out, offset = [], 0
    for _ in range(max_pages):
        d = get_json(cxs, data={"appliedFacets": {}, "limit": 20,
                                "offset": offset, "searchText": search})
        posts = d.get("jobPostings", [])
        if not posts:
            break
        for j in posts:
            path = j.get("externalPath", "")
            out.append(_job(
                "workday", c["name"], path.rsplit("/", 1)[-1], j.get("title"),
                j.get("locationsText"),
                apply_base.rstrip("/") + path,
                _workday_posted(j.get("postedOn")),
                j.get("bulletFields") and " | ".join(j["bulletFields"]) or "",
            ))
        offset += 20
        if offset >= d.get("total", 0):
            break
    return out


def _workday_posted(text):
    if not text:
        return None
    t = text.lower()
    if "today" in t:
        return now_iso()
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return (datetime.now(timezone.utc) - timedelta(days=int(m.group(1)))).isoformat()
    m = re.search(r"(\d+)\+?\s*month", t)
    if m:
        return (datetime.now(timezone.utc) - timedelta(days=30 * int(m.group(1)))).isoformat()
    return None


def ats_oraclecloud(c):
    """Oracle Fusion/Cloud HCM 'CandidateExperience' REST feed.
       Config needs host + site (the siteNumber, e.g. CX_1 / Jobs-at-Icertis).
       Both are derived from careers_url/slug if not given explicitly."""
    host = c.get("host") or urllib.parse.urlparse(c.get("careers_url", "")).netloc
    site = c.get("site") or c.get("slug")
    # public job page: .../sites/<site>/job/<Id>
    apply_base = c.get("apply_base") or \
        f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job"
    base = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    limit = c.get("limit", 200)
    max_pages = c.get("max_pages", 8)     # newest 1600 postings is plenty for a radar
    out, offset = [], 0
    for _ in range(max_pages):
        d = get_json(f"{base}?onlyData=true&expand=requisitionList.secondaryLocations"
                     f"&finder=findReqs;siteNumber={site},limit={limit},offset={offset},"
                     f"sortBy=POSTING_DATES_DESC")
        items = d.get("items") or []
        reqs = items[0].get("requisitionList", []) if items else []
        if not reqs:
            break
        for r in reqs:
            out.append(_job(
                "oraclecloud", c["name"], r.get("Id"), r.get("Title"),
                r.get("PrimaryLocation"),
                f"{apply_base.rstrip('/')}/{r.get('Id')}",
                iso(r.get("PostedDate")),
                strip_html(r.get("ShortDescriptionStr", "")),
                r.get("JobFunction") or r.get("Department"),
            ))
        offset += len(reqs)
        if offset >= (items[0].get("TotalJobsCount") or 0):
            break
    return out


def ats_eightfold(c):
    """Eightfold talent-intelligence careers API.
       Config needs host (e.g. hsbc.eightfold.ai) + domain (e.g. hsbc.com).
       Eightfold caps a page at 10 postings regardless of `num`, so big tenants
       (HSBC ~1500 roles) need many pages. Set "search": "India" to filter
       server-side to India and pull the whole India set in ~20 pages."""
    host = c.get("host") or urllib.parse.urlparse(c.get("careers_url", "")).netloc
    domain = c.get("domain")
    loc = urllib.parse.quote(c.get("search", ""))
    max_pages = c.get("max_pages", 20)
    out, start = [], 0
    for _ in range(max_pages):
        d = get_json(f"https://{host}/api/apply/v2/jobs?domain={domain}"
                     f"&start={start}&num=100&sort_by=relevance"
                     + (f"&location={loc}" if loc else ""))
        pos = d.get("positions") or []
        if not pos:
            break
        for p in pos:
            out.append(_job(
                "eightfold", c["name"], p.get("id"), p.get("name"),
                p.get("location") or ", ".join(p.get("locations") or []),
                p.get("canonicalPositionUrl"),
                iso(p.get("t_create")),
                strip_html(p.get("job_description", "")),
                p.get("department"),
            ))
        start += len(pos)
        if start >= (d.get("count") or 0):
            break
    return out


def ats_keka(c):
    """Keka Hire hosted career site (popular with Indian startups).
       Config needs tenant + board (the embed GUID from the careers page).
       Feed: https://{tenant}.keka.com/careers/api/embedjobs/default/active/{board}"""
    tenant = c.get("tenant") or \
        urllib.parse.urlparse(c.get("careers_url", "")).netloc.split(".")[0]
    board = c.get("board") or c.get("slug")
    d = get_json(f"https://{tenant}.keka.com/careers/api/embedjobs/default/active/{board}")
    jobs = d if isinstance(d, list) else d.get("jobs", [])
    out = []
    for j in jobs:
        locs = j.get("jobLocations") or []
        loc = locs[0] if locs else {}
        where = ", ".join(x for x in [loc.get("city") or loc.get("name"),
                                      loc.get("countryName")] if x)
        out.append(_job(
            "keka", c["name"], j.get("id"), j.get("title"), where,
            f"https://{tenant}.keka.com/careers/jobdetails/{j.get('id')}",
            iso(j.get("publishedOn")),
            strip_html(j.get("description", "")),
            j.get("departmentName"),
        ))
    return out


ADAPTERS = {
    "greenhouse": ats_greenhouse,
    "lever": ats_lever,
    "ashby": ats_ashby,
    "smartrecruiters": ats_smartrecruiters,
    "recruitee": ats_recruitee,
    "workable": ats_workable,
    "workday": ats_workday,
    "oraclecloud": ats_oraclecloud,
    "eightfold": ats_eightfold,
    "keka": ats_keka,
}


# --------------------------------------------------------------------------- #
# ATS detection from a pasted careers URL
# --------------------------------------------------------------------------- #

URL_PATTERNS = [
    (r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([\w.-]+)", "greenhouse"),
    (r"greenhouse\.io/embed/job_board\?for=([\w.-]+)", "greenhouse"),
    (r"jobs\.lever\.co/([\w.-]+)", "lever"),
    (r"jobs\.ashbyhq\.com/([\w.-]+)", "ashby"),
    (r"jobs\.smartrecruiters\.com/([\w.-]+)", "smartrecruiters"),
    (r"([\w-]+)\.recruitee\.com", "recruitee"),
    (r"apply\.workable\.com/([\w.-]+)", "workable"),
]


def detect(url, name=None):
    """Return a candidate source config from a careers URL, or None."""
    url = url.strip()
    for pat, ats in URL_PATTERNS:
        m = re.search(pat, url, re.I)
        if m:
            slug = m.group(1)
            return {"name": name or slug.replace("-", " ").title(),
                    "ats": ats, "slug": slug, "careers_url": url}

    if "myworkdayjobs.com" in url:
        m = re.match(r"https?://([^/]+)/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)", url)
        if m:
            host, site = m.group(1), m.group(2)
            tenant = host.split(".")[0]
            cxs = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
            return {"name": name or tenant.title(), "ats": "workday",
                    "url": cxs, "apply_base": f"https://{host}/{site}",
                    "careers_url": url}

    # last resort: fetch the page and sniff embedded board references
    try:
        page = _req(url)
    except Exception:
        return None
    for pat, ats in URL_PATTERNS:
        m = re.search(pat, page, re.I)
        if m:
            return {"name": name or m.group(1).replace("-", " ").title(),
                    "ats": ats, "slug": m.group(1), "careers_url": url}
    return None


def verify(cfg):
    """Actually call the board. Returns (ok, count_or_error)."""
    try:
        jobs = ADAPTERS[cfg["ats"]](cfg)
        return True, len(jobs)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, type(e).__name__


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #

DEFAULT_PROFILE = {
    "titles_core": ["ai engineer", "ml engineer", "machine learning engineer",
                    "llm engineer", "genai", "generative ai", "applied ai",
                    "rag engineer", "ai/ml"],
    "titles_ok": ["data engineer", "python developer", "python engineer",
                  "backend engineer", "automation engineer", "software engineer",
                  "ai product", "data scientist", "platform engineer"],
    "titles_block": ["intern", "manager", "director", "head of", "vp ", "principal",
                     "architect", "sales", "marketing", "recruiter", "hr ",
                     "designer", "analyst ii", "staff "],
    "seniority_stretch": ["senior", "sr.", "sr ", "lead ", "iii"],
    "skills": {
        "rag": 9, "retrieval augmented": 9, "vector database": 7, "vector db": 7,
        "llm": 7, "large language model": 7, "ollama": 9, "local model": 6,
        "on-prem": 7, "on prem": 7, "self-hosted": 6,
        "ocr": 8, "document ai": 7, "document intelligence": 7,
        "mcp": 9, "model context protocol": 9, "tool calling": 6, "agentic": 7,
        "langchain": 5, "llamaindex": 5, "fastapi": 6, "flask": 4,
        "python": 5, "postgres": 4, "docker": 3, "automation": 4,
        "embedding": 5, "fine-tun": 3, "prompt engineering": 3,
        "chatbot": 3, "nlp": 3, "pipeline": 2
    },
    "gaps": {
        "kubernetes": 3, "k8s": 3, "kafka": 4, "spark": 4, "airflow": 3,
        "dbt": 3, "mlflow": 2, "sagemaker": 3, "azure ml": 3, "vertex ai": 2,
        "kubeflow": 3, "terraform": 2, "snowflake": 2, "databricks": 3,
        "qlora": 2, "lora": 2, "rlhf": 2, "cuda": 2, "scala": 4, "java": 3
    },
    "locations": {
        "remote": 16, "work from home": 14, "anywhere": 10,
        "gurugram": 14, "gurgaon": 14, "noida": 13, "delhi": 12, "ncr": 12,
        "faridabad": 8, "ghaziabad": 8,
        "bengaluru": 5, "bangalore": 5, "hyderabad": 5, "pune": 3,
        "mumbai": 3, "chennai": 2, "india": 4
    },
    "location_required": ["india", "remote", "gurugram", "gurgaon", "noida",
                          "delhi", "ncr", "bengaluru", "bangalore", "hyderabad",
                          "pune", "mumbai", "chennai", "anywhere", "hybrid"],
    "years_max": 5
}


def load_profile():
    if os.path.exists(PROFILE):
        with open(PROFILE) as f:
            p = json.load(f)
        merged = dict(DEFAULT_PROFILE)
        merged.update(p)
        return merged
    return DEFAULT_PROFILE


# Keys the profile editor is allowed to write, and how each is shaped.
_PROFILE_LISTS = ("titles_core", "titles_ok", "titles_block",
                  "seniority_stretch", "location_required")
_PROFILE_WEIGHTED = ("skills", "gaps", "locations")


def _to_num(v, default=0):
    try:
        return int(v) if float(v).is_integer() else round(float(v), 2)
    except (TypeError, ValueError):
        return default


def save_profile(payload):
    """Validate + persist the scoring profile coming from the Profile page.
    Returns (ok, error). Unknown keys are dropped; weights coerced to numbers."""
    if not isinstance(payload, dict):
        return False, "profile must be an object"
    prof = {}
    for k in _PROFILE_LISTS:
        if isinstance(payload.get(k), list):
            seen, cleaned = set(), []
            for x in payload[k]:
                s = str(x).strip().lower()
                if s and s not in seen:
                    seen.add(s)
                    cleaned.append(s)
            prof[k] = cleaned
    for k in _PROFILE_WEIGHTED:
        if isinstance(payload.get(k), dict):
            prof[k] = {str(kk).strip().lower(): _to_num(vv)
                       for kk, vv in payload[k].items() if str(kk).strip()}
    if "years_max" in payload:
        prof["years_max"] = int(_to_num(payload["years_max"], DEFAULT_PROFILE["years_max"]))
    if not prof:
        return False, "nothing to save"
    with _lock:
        save_json(PROFILE, prof)
    return True, None


def score(job, prof):
    title = job["title"].lower()
    loc = job["location"].lower()
    text = (job["title"] + " " + job["description"]).lower()
    pts, notes, matched, gaps, flags = 0, [], [], [], []

    # ---- title -----------------------------------------------------------
    if any(b in title for b in prof["titles_block"]):
        return None                                   # not our lane at all
    title_hit = False
    if any(t in title for t in prof["titles_core"]):
        pts += 32
        notes.append("core title")
        title_hit = True
    elif any(t in title for t in prof["titles_ok"]):
        pts += 18
        notes.append("adjacent title")
        title_hit = True
    else:
        pts -= 6
    if any(s in title for s in prof["seniority_stretch"]):
        pts -= 8
        flags.append("STRETCH")

    # ---- skills ----------------------------------------------------------
    for kw, w in prof["skills"].items():
        if kw in text:
            pts += w
            matched.append(kw)

    # ---- relevance gate --------------------------------------------------
    # A role with neither a target title NOR any of your skills is a different
    # profile entirely (procurement, legal, vigilance, voice-over ...). Being in
    # a well-weighted city (Noida +13) must not float it onto the board, so drop
    # it here -- before location points are ever added.
    if not title_hit and not matched:
        return None

    gap_pen = 0
    for kw, w in prof["gaps"].items():
        if kw in text:
            gap_pen += w
            gaps.append(kw)
    pts -= min(gap_pen, 12)          # gaps inform the score, they don't bury a good role

    # ---- location (India only: on-site India or remote-India) ------------
    def _lin(kw):                    # word-boundary match so "india" != "indiana"
        return re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", loc) is not None
    loc_pts = 0
    for kw, w in prof["locations"].items():
        if _lin(kw):
            loc_pts = max(loc_pts, w)
    if loc_pts == 0 and not any(_lin(k) for k in prof["location_required"]):
        if loc != "not stated":
            return None                               # outside India / remote
    pts += loc_pts

    # ---- experience ------------------------------------------------------
    # Only trust a number that sits right next to the word "experience" -- a bare
    # "N years" anywhere in the text grabs salaries, tenure boilerplate, "founded
    # 8 years ago", etc. (that's what produced nonsense like "81+ yrs").
    yrs = None
    dl = job["description"].lower()
    m = (re.search(r"(\d{1,2})\s*\+?\s*(?:[-–—]|to)?\s*(?:\d{1,2})?\s*\+?\s*years?\b"
                   r"[^.\n]{0,25}\bexperience\b", dl)
         or re.search(r"experience[^.\n]{0,25}?(\d{1,2})\s*\+?\s*years?\b", dl))
    if m and 0 < int(m.group(1)) <= 20:   # sanity-bound: ignore absurd extractions
        yrs = int(m.group(1))
        if yrs > prof["years_max"]:
            pts -= 14
            flags.append(f"{yrs}+ yrs")
        elif yrs <= 2:
            pts += 6                    # entry / early-career: ideal for a 2-yr candidate
            notes.append(f"{yrs}y req")

    # ---- recency ---------------------------------------------------------
    h = hours_since(job.get("posted_at"))
    if h is not None:
        if h <= 24:
            pts += 12
        elif h <= 72:
            pts += 8
        elif h <= 168:
            pts += 4
        elif h > 720:
            pts -= 6
            flags.append("stale")

    fit = max(0, min(10, round(pts / 10.5, 1)))
    job["fit"] = fit
    job["matched"] = sorted(set(matched), key=lambda k: -prof["skills"][k])[:10]
    job["gaps"] = sorted(set(gaps))[:8]
    job["flags"] = flags
    job["why"] = ", ".join(notes) or "keyword match"
    job["years_req"] = yrs
    return job


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def load_store():
    return load_json(STORE, {"jobs": {}, "meta": {}})


def source_stats():
    """Per-company rollup for the sources page: live/unseen/applied counts etc.
    Computed over the full store (applied/saved counts include closed postings so
    you never lose sight of a role you already actioned)."""
    store = load_store()
    per = {}
    for j in store["jobs"].values():
        co = j.get("company") or "Unknown"
        d = per.setdefault(co, {"total": 0, "live": 0, "unseen": 0, "fresh24": 0,
                                "applied": 0, "saved": 0, "top_fit": 0.0})
        d["total"] += 1
        if j.get("status") == "applied":
            d["applied"] += 1
        if j.get("status") == "saved":
            d["saved"] += 1
        if not j.get("closed"):
            d["live"] += 1
            if not j.get("seen"):
                d["unseen"] += 1
            f = j.get("fit") or 0
            if f > d["top_fit"]:
                d["top_fit"] = f
            h = hours_since(j.get("first_seen"))
            if h is not None and h <= 24:
                d["fresh24"] += 1
    return {"per_company": per, "meta": store.get("meta", {})}


def fetch_all(verbose=True):
    sources = load_json(COMPANIES, {"companies": []})["companies"]
    active = [c for c in sources if c.get("ats") in ADAPTERS and not c.get("disabled")]
    prof = load_profile()
    found, errors, ok_companies = [], [], set()

    def pull(c):
        try:
            return c, ADAPTERS[c["ats"]](c), None
        except Exception as e:
            return c, [], f"{type(e).__name__}: {e}"

    with futures.ThreadPoolExecutor(max_workers=8) as ex:
        for c, jobs, err in ex.map(pull, active):
            if err:
                errors.append({"company": c["name"], "error": err})
                if verbose:
                    print(f"  x {c['name']:<28} {err}")
                continue
            ok_companies.add(c["name"])
            kept = [s for s in (score(j, prof) for j in jobs) if s]
            found.extend(kept)
            if verbose:
                print(f"  - {c['name']:<28} {len(jobs):>3} posted  {len(kept):>3} relevant")

    with _lock:
        store = load_store()
        jobs, new_count = store["jobs"], 0
        for j in found:
            old = jobs.get(j["key"])
            if old:
                j["first_seen"] = old.get("first_seen", now_iso())
                j["status"] = old.get("status", "new")
                j["seen"] = old.get("seen", False)
            else:
                j["first_seen"] = now_iso()
                j["status"] = "new"
                j["seen"] = False
                new_count += 1
            jobs[j["key"]] = j
        # drop postings that vanished from the board and were never actioned --
        # but ONLY for companies that actually fetched this cycle. Otherwise a
        # transient timeout would mark all of a company's jobs closed and wipe
        # it from the board until the next successful fetch.
        live = {j["key"] for j in found}
        for k in list(jobs):
            j = jobs[k]
            if (k not in live and j.get("company") in ok_companies
                    and j.get("status") in ("new", None)):
                j["closed"] = True
        store["meta"] = {"last_fetch": now_iso(), "sources": len(active),
                         "errors": errors, "total": len(jobs)}
        save_json(STORE, store)

    if verbose:
        print(f"\n{len(found)} relevant / {new_count} brand new / "
              f"{len(active)} sources / {len(errors)} failing")
    return new_count


# --------------------------------------------------------------------------- #
# server
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)
        except (ConnectionError, BrokenPipeError):
            # Client closed the connection before we finished responding
            # (e.g. page refreshed / navigated away during a slow refresh).
            # Harmless — nothing to send to anymore.
            pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(DASHBOARD, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path in ("/db", "/db.html", "/sources"):
            with open(DB_PAGE, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path in ("/profile", "/profile.html"):
            with open(PROFILE_PAGE, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/profile":
            return self._send(200, json.dumps(load_profile()))
        if path == "/api/jobs":
            store = load_store()
            rows = [j for j in store["jobs"].values() if not j.get("closed")]
            rows.sort(key=lambda j: (j.get("fit", 0), j.get("first_seen") or ""),
                      reverse=True)
            return self._send(200, json.dumps({"jobs": rows, "meta": store["meta"]}))
        if path == "/api/sources":
            return self._send(200, json.dumps(load_json(COMPANIES, {"companies": []})))
        if path == "/api/stats":
            return self._send(200, json.dumps(source_stats()))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or "{}")

        if path == "/api/refresh":
            new = fetch_all(verbose=False)
            return self._send(200, json.dumps({"new": new}))

        if path == "/api/profile":
            ok, err = save_profile(payload)
            return self._send(200, json.dumps({"ok": ok, "error": err}))

        if path == "/api/status":
            with _lock:
                store = load_store()
                j = store["jobs"].get(payload.get("key"))
                if j:
                    j["status"] = payload.get("status", "new")
                    j["seen"] = True
                    save_json(STORE, store)
            return self._send(200, json.dumps({"ok": True}))

        if path == "/api/seen":
            with _lock:
                store = load_store()
                for k in payload.get("keys", []):
                    if k in store["jobs"]:
                        store["jobs"][k]["seen"] = True
                save_json(STORE, store)
            return self._send(200, json.dumps({"ok": True}))

        if path == "/api/add":
            cfg = detect(payload.get("url", ""), payload.get("name"))
            if not cfg:
                return self._send(200, json.dumps(
                    {"ok": False, "error": "No supported job board found on that page."}))
            ok, info = verify(cfg)
            if not ok:
                return self._send(200, json.dumps(
                    {"ok": False, "error": f"Board found but not reachable ({info})."}))
            with _lock:
                data = load_json(COMPANIES, {"companies": []})
                ident = (cfg["ats"], cfg.get("slug") or cfg.get("url"))
                if any((c.get("ats"), c.get("slug") or c.get("url")) == ident
                       for c in data["companies"]):
                    return self._send(200, json.dumps(
                        {"ok": False, "error": "Already tracking that board."}))
                data["companies"].append(cfg)
                save_json(COMPANIES, data)
            return self._send(200, json.dumps(
                {"ok": True, "name": cfg["name"], "ats": cfg["ats"], "count": info}))

        return self._send(404, json.dumps({"error": "not found"}))


def serve(port, every):
    def loop():
        while True:
            time.sleep(every * 60)
            try:
                fetch_all(verbose=False)
            except Exception as e:
                print("background fetch failed:", e)

    threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"JobRadar on http://localhost:{port}   (polling every {every} min)")
    print("Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


# --------------------------------------------------------------------------- #
# resolver -- bulk-guess slugs for a plain list of company names
# --------------------------------------------------------------------------- #

def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "", name.lower())
    return s


def slug_variants(name):
    base = name.lower()
    base = re.sub(r"\b(inc|llc|ltd|limited|pvt|private|technologies|technology|"
                  r"systems|solutions|labs|software|india|corp)\b", "", base)
    base = base.strip()
    return list(dict.fromkeys([
        re.sub(r"[^a-z0-9]+", "", base),
        re.sub(r"[^a-z0-9]+", "-", base).strip("-"),
        re.sub(r"[^a-z0-9]+", "", name.lower()),
        re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
    ]))


def resolve(path):
    data = load_json(path, None)
    if data is None:
        sys.exit(f"can't read {path}")
    names = data["companies"] if isinstance(data, dict) else data
    names = [c["name"] if isinstance(c, dict) else c for c in names]
    print(f"resolving {len(names)} companies across 6 platforms "
          f"(~{len(names) * 4 // 60 + 1} min)...\n")

    order = ["greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee"]

    def try_one(name):
        for slug in slug_variants(name):
            if not slug:
                continue
            for ats in order:
                cfg = {"name": name, "ats": ats, "slug": slug}
                try:
                    jobs = ADAPTERS[ats](cfg)
                except Exception:
                    continue
                if jobs:
                    cfg["resolved"] = now_iso()
                    return cfg
        return None

    hits = []
    with futures.ThreadPoolExecutor(max_workers=6) as ex:
        for name, cfg in zip(names, ex.map(try_one, names)):
            if cfg:
                hits.append(cfg)
                print(f"  ok  {name:<32} {cfg['ats']}/{cfg['slug']}")
            else:
                print(f"  --  {name}")

    existing = load_json(COMPANIES, {"companies": []})
    known = {(c.get("ats"), c.get("slug")) for c in existing["companies"]}
    added = [c for c in hits if (c["ats"], c["slug"]) not in known]
    existing["companies"].extend(added)
    save_json(COMPANIES, existing)
    print(f"\nresolved {len(hits)}/{len(names)}; {len(added)} added to companies.json")
    print("Unresolved companies are usually on Workday, Taleo, Naukri or a custom site "
          "-- add those with:  python jobradar.py add <careers-url>")


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #

def cmd_add(url, name=None):
    cfg = detect(url, name)
    if not cfg:
        print("No supported job board found there.")
        print("Supported: Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee, "
              "Workable, Workday.")
        return
    ok, info = verify(cfg)
    if not ok:
        print(f"Detected {cfg['ats']} ({cfg.get('slug') or cfg.get('url')}) "
              f"but the board did not respond: {info}")
        return
    data = load_json(COMPANIES, {"companies": []})
    ident = (cfg["ats"], cfg.get("slug") or cfg.get("url"))
    if any((c.get("ats"), c.get("slug") or c.get("url")) == ident
           for c in data["companies"]):
        print(f"Already tracking {cfg['name']}.")
        return
    data["companies"].append(cfg)
    save_json(COMPANIES, data)
    print(f"Added {cfg['name']}  [{cfg['ats']}]  {info} live postings.")


def cmd_doctor():
    data = load_json(COMPANIES, {"companies": []})
    for c in data["companies"]:
        if c.get("ats") not in ADAPTERS:
            print(f"  ?   {c['name']:<30} unknown ats {c.get('ats')}")
            continue
        ok, info = verify(c)
        print(f"  {'ok ' if ok else 'BAD'} {c['name']:<30} {c['ats']:<16} {info}")


def main():
    p = argparse.ArgumentParser(prog="jobradar")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("url"); a.add_argument("--name")
    b = sub.add_parser("bulk"); b.add_argument("file")
    r = sub.add_parser("resolve"); r.add_argument("file")
    sub.add_parser("fetch")
    sub.add_parser("doctor")
    sub.add_parser("list")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--every", type=int, default=10)
    args = p.parse_args()

    if args.cmd == "add":
        cmd_add(args.url, args.name)
    elif args.cmd == "bulk":
        for line in open(args.file):
            line = line.strip()
            if line and not line.startswith("#"):
                cmd_add(line)
    elif args.cmd == "resolve":
        resolve(args.file)
    elif args.cmd == "fetch":
        fetch_all()
    elif args.cmd == "doctor":
        cmd_doctor()
    elif args.cmd == "list":
        for c in load_json(COMPANIES, {"companies": []})["companies"]:
            print(f"  {c['name']:<32} {c.get('ats'):<16} "
                  f"{c.get('slug') or c.get('url')}")
    elif args.cmd == "serve":
        serve(args.port, args.every)


if __name__ == "__main__":
    main()
