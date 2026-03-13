"""
Auren Leads — Flask Backend (v2 — hardened)
Run: python app.py  →  open http://localhost:5000

Requirements:
    pip install flask flask-cors gspread google-auth requests beautifulsoup4
"""

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import json, os, re, threading, time, traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── APP SETUP ─────────────────────────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="/static")
CORS(app)

SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
STATE_FILE    = os.path.join(BASE_DIR, "state.json")
LOG_FILE      = os.path.join(BASE_DIR, "run.log")
FRONTEND      = os.path.join(FRONTEND_DIR, "index.html")

# ── THREAD SAFETY ─────────────────────────────────────────────────────────────
_state_lock    = threading.Lock()
_settings_lock = threading.Lock()
_run_lock      = threading.Lock()

# ── SETTINGS ─────────────────────────────────────────────────────────────────
DEFAULTS = {
    "google_maps_key": "",
    "hunter_key":      "",
    "sheet_id":        "",
    "creds_json":      "",
    "schedule_time":   "09:00",
    "leads_per_run":   100,
    "quality_split":   {"no": 70, "bad": 20, "ok": 10},
    "active_cities": [
        "Mumbai","Delhi","Bangalore","Pune","Hyderabad","Chennai",
        "Ahmedabad","Kolkata","Jaipur","Surat","Chandigarh","Kochi",
        "Nagpur","Indore","Lucknow","Coimbatore","Bhopal","Gurgaon",
        "Noida","Vadodara"
    ],
    "active_cats": [
        "cafe","restaurant","real estate agent","salon","spa",
        "retail store","clothing store","jewelry store","gym",
        "dental clinic","interior designer","catering service",
        "event planner","photography studio","coaching institute"
    ],
}

SENSITIVE_KEYS = {"creds_json", "google_maps_key", "hunter_key"}


def load_settings():
    with _settings_lock:
        s = dict(DEFAULTS)
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE) as f:
                    s.update(json.load(f))
            except Exception:
                pass
        return s


def save_settings(data: dict):
    with _settings_lock:
        current = dict(DEFAULTS)
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE) as f:
                    current.update(json.load(f))
            except Exception:
                pass
        current.update(data)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(current, f, indent=2)


def _default_state():
    return {
        "automation_on":  False,
        "last_run":       None,
        "total_leads":    0,
        "running":        False,
        "last_run_count": 0,
        "last_error":     "",
        "progress":       "",
        "progress_pct":   0,
        "started_at":     None,
    }


def load_state():
    with _state_lock:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return _default_state()


def save_state(s: dict):
    with _state_lock:
        with open(STATE_FILE, "w") as f:
            json.dump(s, f, indent=2)


# ── API ROUTES ────────────────────────────────────────────────────────────────
@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    s    = load_settings()
    safe = {k: v for k, v in s.items() if k not in SENSITIVE_KEYS}
    safe["has_creds"]      = bool(s.get("creds_json", "").strip())
    safe["has_maps_key"]   = bool(s.get("google_maps_key", "").strip())
    safe["has_hunter_key"] = bool(s.get("hunter_key", "").strip())
    return jsonify(safe)


@app.route("/api/settings", methods=["POST"])
def api_post_settings():
    data = request.get_json(force=True, silent=True) or {}
    save_settings(data)
    return jsonify({"ok": True})


@app.route("/api/state", methods=["GET"])
def api_get_state():
    return jsonify(load_state())


@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    with _run_lock:
        s = load_state()
        s["automation_on"] = not s.get("automation_on", False)
        save_state(s)
    return jsonify({"automation_on": s["automation_on"]})


@app.route("/api/run-now", methods=["POST"])
def api_run_now():
    with _run_lock:
        if load_state().get("running"):
            return jsonify({"ok": False, "msg": "Already running"})
        s = load_state()
        s["running"] = True
        save_state(s)

    data = request.get_json(force=True, silent=True) or {}
    if data:
        save_settings(data)

    threading.Thread(target=_run_pipeline, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/leads", methods=["GET"])
def api_get_leads():
    try:
        s = load_settings()
        if not s.get("sheet_id", "").strip() or not s.get("creds_json", "").strip():
            return jsonify({
                "leads": [],
                "error": "Google Sheets not configured — go to Settings.",
            })
        ws   = _get_sheet(s)
        rows = ws.get_all_values()
        if len(rows) < 2:
            return jsonify({"leads": [], "headers": rows[0] if rows else []})
        heads = rows[0]
        leads = list(reversed([dict(zip(heads, r)) for r in rows[1:]]))[:500]
        return jsonify({"leads": leads, "headers": heads, "total": len(rows) - 1})
    except Exception as e:
        return jsonify({"leads": [], "error": str(e)})


@app.route("/api/logs", methods=["GET"])
def api_get_logs():
    try:
        with open(LOG_FILE) as f:
            lines = f.readlines()[-120:]
        return jsonify({"log": "".join(lines)})
    except FileNotFoundError:
        return jsonify({"log": "No logs yet."})


@app.route("/api/verify-keys", methods=["POST"])
def api_verify_keys():
    import requests as req
    s   = load_settings()
    res = {}

    key = s.get("google_maps_key", "").strip()
    if key:
        try:
            r  = req.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json",
                params={"query": "cafe in Mumbai India", "key": key},
                timeout=10,
            )
            st = r.json().get("status", "ERROR")
            res["google_maps"] = (
                "ok" if st in ("OK", "ZERO_RESULTS") else f"API error: {st}"
            )
        except Exception as e:
            res["google_maps"] = f"Connection error: {e}"
    else:
        res["google_maps"] = "Key not set"

    try:
        _get_sheet(s)
        res["google_sheets"] = "ok"
    except ValueError as e:
        res["google_sheets"] = str(e)
    except Exception as e:
        res["google_sheets"] = f"Error: {str(e)[:80]}"

    return jsonify(res)


@app.route("/api/clear-log", methods=["POST"])
def api_clear_log():
    with open(LOG_FILE, "w") as f:
        f.write("")
    return jsonify({"ok": True})


# ── PIPELINE ──────────────────────────────────────────────────────────────────
def _set_progress(msg, pct=None):
    s = load_state()
    s["progress"] = msg
    if pct is not None:
        s["progress_pct"] = pct
    save_state(s)


def _run_pipeline():
    s = load_state()
    s.update({
        "running":      True,
        "last_error":   "",
        "progress":     "Starting…",
        "progress_pct": 0,
        "started_at":   datetime.now().isoformat(),
    })
    save_state(s)

    _log("=" * 58)
    _log(f"  AUREN LEADS PIPELINE  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    _log("=" * 58)

    try:
        import random

        cfg    = load_settings()
        cities = cfg.get("active_cities") or DEFAULTS["active_cities"]
        cats   = cfg.get("active_cats")   or DEFAULTS["active_cats"]
        target = int(cfg.get("leads_per_run", 100))
        sp     = cfg.get("quality_split", {"no": 70, "bad": 20, "ok": 10})
        t_no   = round(target * (sp.get("no", 70) / 100))
        t_bad  = round(target * (sp.get("bad", 20) / 100))
        t_ok   = max(0, target - t_no - t_bad)
        key    = cfg.get("google_maps_key", "").strip()

        if not key:
            raise ValueError("Google Maps API key not set. Go to Settings tab.")
        if not cfg.get("sheet_id", "").strip():
            raise ValueError("Google Sheet ID not set. Go to Settings tab.")
        if not cfg.get("creds_json", "").strip():
            raise ValueError("Service Account JSON not set. Go to Settings tab.")

        _log(
            f"Target: {target} leads | Split: {sp.get('no')}% no-site / "
            f"{sp.get('bad')}% bad / {sp.get('ok')}% ok"
        )
        _log(f"Cities: {len(cities)} | Categories: {len(cats)}")

        # ── PARALLEL SCRAPE ────────────────────────────────────────────
        _set_progress("Scraping Google Maps & JustDial in parallel…", 5)
        combos = [
            (c, k)
            for c in random.sample(cities, len(cities))
            for k in random.sample(cats, len(cats))
        ]

        def scrape_combo(cc):
            city, cat = cc
            res = []
            maps = _search_maps(city, cat, key, max_r=8)
            good = [b for b in maps if b.get("website_status") in ("none", "bad")]
            for b in good:
                b["category"] = cat
                b["city"]     = city
            res.extend(good)
            jd = _scrape_jd(city, cat, max_r=6)
            for b in jd:
                b["category"] = cat
                b["city"]     = city
            res.extend(jd)
            return res

        raw              = []
        completed_combos = 0
        total_combos     = len(combos)
        pool_limit       = target * 3

        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(scrape_combo, c): c for c in combos}
            for fut in as_completed(futs):
                completed_combos += 1
                if len(raw) >= pool_limit:
                    for f in futs:
                        f.cancel()
                    break
                try:
                    res = fut.result(timeout=30)
                    raw.extend(res)
                    city, cat = futs[fut]
                    _log(
                        f"🔍 {cat[:20]:20} / {city:14} → "
                        f"{len(res)} raw (pool: {len(raw)})"
                    )
                    pct = min(40, int((completed_combos / total_combos) * 40))
                    _set_progress(f"Scraped {len(raw)} raw leads…", pct)
                except Exception as e:
                    _log(f"   Scrape error: {e}")

        _log(
            f"\n📦 Raw pool: {len(raw)} | "
            f"Enriching top {min(target * 2, len(raw))}…"
        )

        # ── PARALLEL ENRICHMENT ────────────────────────────────────────
        enrich_count = min(target * 2, len(raw))
        _set_progress(
            f"Enriching {enrich_count} leads (email + Instagram)…", 42
        )
        to_enrich = raw[:enrich_count]
        enriched  = []
        done_n    = 0

        def enrich(lead):
            lead["email"] = _find_email(
                lead["name"], lead.get("city", ""),
                lead.get("website", ""), cfg,
            )
            lead["instagram"] = _find_instagram(
                lead["name"], lead.get("city", ""),
                lead.get("website", ""),
            )
            lead["score"] = _score(lead)
            return lead

        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(enrich, l) for l in to_enrich]
            for fut in as_completed(futs):
                try:
                    enriched.append(fut.result(timeout=30))
                    done_n += 1
                    if done_n % 10 == 0:
                        pct = 42 + int((done_n / len(to_enrich)) * 45)
                        _set_progress(
                            f"Enriched {done_n} / {len(to_enrich)} leads…", pct
                        )
                        _log(f"   ✓ Enriched {done_n}/{len(to_enrich)}")
                except Exception as e:
                    _log(f"   Enrich error: {e}")

        # ── BUCKET & DEDUPLICATE ───────────────────────────────────────
        deduped = _dedup(enriched)
        b_no  = [l for l in deduped if l.get("website_status") == "none"][:t_no]
        b_bad = [l for l in deduped if l.get("website_status") == "bad"][:t_bad]
        b_ok  = [l for l in deduped if l.get("website_status") == "ok"][:t_ok]
        final = (b_no + b_bad + b_ok)[:target]
        _log(
            f"\n📊 Final: {len(final)} leads — "
            f"no-site:{len(b_no)} | bad:{len(b_bad)} | ok:{len(b_ok)}"
        )

        # ── WRITE TO SHEETS ────────────────────────────────────────────
        _set_progress("Writing to Google Sheets (batch)…", 90)
        _log("📤 Writing to Google Sheets…")
        ws      = _get_sheet(cfg)
        _ensure_header(ws)
        written = _write_leads(ws, final)

        s = load_state()
        s["total_leads"]    = s.get("total_leads", 0) + written
        s["last_run"]       = datetime.now().isoformat()
        s["last_run_count"] = written
        s["running"]        = False
        s["progress"]       = f"✅ Done — {written} leads written to Sheet"
        s["progress_pct"]   = 100
        s["started_at"]     = None
        save_state(s)
        _log(
            f"\n✅ DONE! {written} leads written. "
            f"All-time total: {s['total_leads']}"
        )
        _log("=" * 58)

    except Exception as e:
        err = traceback.format_exc()
        _log(f"\n❌ ERROR: {e}\n{err}")
        s = load_state()
        s["running"]    = False
        s["last_error"] = str(e)
        s["progress"]   = f"❌ Error: {e}"
        s["started_at"] = None
        save_state(s)


# ── SCRAPING HELPERS ─────────────────────────────────────────────────────────
BAD_DOMAINS = [
    "wix.com","weebly.com","blogspot.com","wordpress.com",
    "business.site","sites.google.com","carrd.co",
    "justdial.com","indiamart.com","sulekha.com",
    "facebook.com","instagram.com",
]
HDR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _cls(url: str) -> str:
    if not url:
        return "none"
    u = url.lower()
    for d in BAD_DOMAINS:
        if d in u:
            return "bad"
    return "ok"


def _search_maps(city: str, cat: str, key: str, max_r: int = 8):
    """Search Google Maps — detail calls are sequential to avoid rate bombs."""
    import requests
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/place/textsearch/json",
            params={
                "query":  f"{cat} in {city}, India",
                "key":    key,
                "region": "in",
            },
            timeout=12,
        )
        data = r.json()
        if data.get("status") not in ("OK", "ZERO_RESULTS"):
            return []
        pids    = [p["place_id"] for p in data.get("results", [])[:max_r]]
        results = []
        for pid in pids:
            d = _maps_detail(pid, key)
            if d:
                results.append(d)
        return results
    except Exception as e:
        _log(f"   Maps search error ({city}/{cat}): {e}")
        return []


def _maps_detail(pid: str, key: str):
    import requests
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={
                "place_id": pid,
                "fields": (
                    "name,formatted_address,formatted_phone_number,"
                    "website,rating,user_ratings_total,url"
                ),
                "key": key,
            },
            timeout=8,
        )
        res = r.json().get("result", {})
        if not res:
            return None
        w = res.get("website", "")
        return {
            "name":           res.get("name", ""),
            "address":        res.get("formatted_address", ""),
            "phone":          res.get("formatted_phone_number", ""),
            "website":        w,
            "website_status": _cls(w),
            "rating":         res.get("rating", 0),
            "reviews":        res.get("user_ratings_total", 0),
            "maps_url":       res.get("url", ""),
            "source":         "Google Maps",
        }
    except Exception:
        return None


JD_CITY = {
    "Mumbai": "mumbai",   "Delhi": "delhi",       "Bangalore": "bangalore",
    "Pune": "pune",       "Hyderabad": "hyderabad","Chennai": "chennai",
    "Ahmedabad": "ahmedabad","Kolkata": "kolkata", "Jaipur": "jaipur",
    "Surat": "surat",     "Chandigarh": "chandigarh","Kochi": "kochi",
    "Nagpur": "nagpur",   "Indore": "indore",     "Lucknow": "lucknow",
    "Gurgaon": "gurgaon", "Noida": "noida",       "Coimbatore": "coimbatore",
    "Bhopal": "bhopal",   "Vadodara": "vadodara",
}
JD_CAT = {
    "cafe": "cafes",                    "restaurant": "restaurants",
    "real estate agent": "real-estate-agents",
    "salon": "beauty-parlours",         "spa": "spas",
    "retail store": "retail-shops",     "clothing store": "clothing-stores",
    "jewelry store": "jewellery-showrooms",
    "gym": "gym-fitness-centres",       "dental clinic": "dentists",
    "interior designer": "interior-designers",
    "coaching institute": "coaching-classes",
    "catering service": "catering-services",
    "event planner": "event-planners",
    "photography studio": "photographers",
}


def _scrape_jd(city: str, cat: str, max_r: int = 6):
    import requests as req
    from bs4 import BeautifulSoup

    cs  = JD_CITY.get(city, city.lower())
    ks  = JD_CAT.get(cat, cat.replace(" ", "-"))
    url = f"https://www.justdial.com/{cs}/{ks}"
    try:
        r = req.get(url, headers=HDR, timeout=10)
        if r.status_code != 200:
            return []
        soup  = BeautifulSoup(r.text, "html.parser")
        cards = (
            soup.select("li[class*='cntanr']")
            or soup.select(".resultbox_info")
            or soup.select("[class*='resultbox']")
        )
        out = []
        for card in cards[:max_r]:
            nt = card.select_one(".resultbox_title_anchor, .jdnm, h2 a")
            if not nt:
                continue
            pt = card.select_one(".contact_info, .tel")
            at = card.select_one(".resultbox_address, .adr")
            rt = card.select_one("[class*='rating']")
            m  = re.search(r"[\d.]+", rt.get_text() if rt else "0")
            ph = re.sub(r"[^\d+\-\s]", "", pt.get_text()).strip() if pt else ""
            out.append({
                "name":           nt.get_text(strip=True),
                "address":        at.get_text(strip=True) if at else city,
                "phone":          ph,
                "website":        "",
                "website_status": "none",
                "rating":         float(m.group()) if m else 0,
                "reviews":        0,
                "maps_url":       "",
                "source":         "JustDial",
            })
        return out
    except Exception as e:
        _log(f"   JustDial error ({city}/{cat}): {e}")
        return []


def _find_email(name: str, city: str, website: str, cfg: dict) -> str:
    import requests as req

    ER   = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
    SKIP = {
        "example","noreply","wix","sentry","test",
        "schema","w3.org","google","support@",
    }

    def clean(emails):
        return [
            e for e in emails
            if not any(x in e.lower() for x in SKIP) and len(e) < 60
        ]

    # 1. Scrape website — try both "ok" AND "bad" sites (they may have contact info)
    if website and _cls(website) in ("ok", "bad"):
        for pg in [website, website.rstrip("/") + "/contact"]:
            try:
                r = req.get(pg, headers=HDR, timeout=5, allow_redirects=True)
                if r.status_code == 200:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(r.text, "html.parser")
                    for t in soup(["script", "style"]):
                        t.decompose()
                    em = clean(ER.findall(soup.get_text()))
                    if em:
                        return em[0]
            except Exception:
                pass

    # 2. Hunter.io
    hk = cfg.get("hunter_key", "").strip()
    if hk and website:
        try:
            from urllib.parse import urlparse
            dom = urlparse(website).netloc.replace("www.", "")
            if dom:
                r  = req.get(
                    "https://api.hunter.io/v2/domain-search",
                    params={"domain": dom, "api_key": hk, "limit": 1},
                    timeout=6,
                )
                em = r.json().get("data", {}).get("emails", [])
                if em:
                    return em[0].get("value", "")
        except Exception:
            pass

    # 3. Google fallback (note: may be rate-limited / CAPTCHAed)
    try:
        r = req.get(
            "https://www.google.com/search",
            params={"q": f'"{name}" "{city}" email contact'},
            headers=HDR,
            timeout=5,
        )
        if r.status_code == 200:
            em = clean(ER.findall(r.text))
            if em:
                return em[0]
    except Exception:
        pass

    return ""


def _find_instagram(name: str, city: str, website: str) -> str:
    import requests as req

    SKIP = {
        "p","explore","accounts","stories","reels",
        "shoppingbag","about","help","instagram","developer",
    }

    def extract(text):
        m = re.search(r"instagram\.com/([a-zA-Z0-9_.]{2,30})", text)
        if m and m.group(1).lower() not in SKIP:
            return f"@{m.group(1)}"
        return None

    # 1. From website links
    if website:
        try:
            r = req.get(website, headers=HDR, timeout=5)
            if r.status_code == 200:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(r.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    if "instagram.com/" in a["href"].lower():
                        ig = extract(a["href"])
                        if ig:
                            return ig
        except Exception:
            pass

    # 2. Google search (note: may be rate-limited)
    try:
        r = req.get(
            "https://www.google.com/search",
            params={"q": f'site:instagram.com "{name}" {city} India'},
            headers=HDR,
            timeout=5,
        )
        if r.status_code == 200:
            ig = extract(r.text)
            if ig:
                return ig
    except Exception:
        pass

    return ""


def _score(lead: dict) -> int:
    s  = 0
    ws = lead.get("website_status", "ok").lower()
    if ws == "none":
        s += 40
    elif ws == "bad":
        s += 20
    if lead.get("phone"):
        s += 10
    if lead.get("email"):
        s += 15
    if lead.get("instagram"):
        s += 15
    try:
        if float(lead.get("rating", 0)) >= 4.0:
            s += 10
    except (ValueError, TypeError):
        pass
    try:
        if int(lead.get("reviews", 0)) >= 50:
            s += 10
    except (ValueError, TypeError):
        pass
    return min(s, 100)


def _dedup(leads):
    """Deduplicate by phone number AND by name+city for phoneless leads."""
    by_phone = {}
    by_name  = {}

    for l in leads:
        p   = l.get("phone", "").strip()
        nck = (
            l.get("name", "").strip().lower(),
            l.get("city", "").strip().lower(),
        )

        if p:
            if p not in by_phone or l.get("score", 0) > by_phone[p].get("score", 0):
                by_phone[p] = l
        else:
            if nck not in by_name or l.get("score", 0) > by_name[nck].get("score", 0):
                by_name[nck] = l

    combined = list(by_phone.values()) + list(by_name.values())
    return sorted(combined, key=lambda x: x.get("score", 0), reverse=True)


# ── SHEETS HELPERS ────────────────────────────────────────────────────────────
COLS = [
    "Date Found","Business Name","Category","City",
    "Phone","Email","Website Status","Website URL",
    "Instagram","Google Rating","Reviews",
    "Maps URL","Score","Source",
]


def _get_sheet(cfg: dict):
    from google.oauth2.service_account import Credentials
    import gspread

    cj  = cfg.get("creds_json", "").strip()
    sid = cfg.get("sheet_id", "").strip()
    if not cj:
        raise ValueError("Service Account JSON not set")
    if not sid:
        raise ValueError("Sheet ID not set")

    cd  = json.loads(cj) if isinstance(cj, str) else cj
    cre = Credentials.from_service_account_info(
        cd,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    cl     = gspread.authorize(cre)
    ss     = cl.open_by_key(sid)
    titles = [w.title for w in ss.worksheets()]
    if "Leads" not in titles:
        ws = ss.add_worksheet("Leads", rows=10000, cols=len(COLS))
        ws.update("A1", [COLS])
    else:
        ws = ss.worksheet("Leads")
    return ws


def _ensure_header(ws):
    try:
        row1 = ws.row_values(1)
        if not row1 or row1[0] != "Date Found":
            ws.update("A1", [COLS])
    except Exception:
        pass


def _write_leads(ws, leads):
    existing_phones = set(filter(None, ws.col_values(5)[1:]))
    today           = datetime.now().strftime("%Y-%m-%d")
    rows            = []
    for l in leads:
        p = l.get("phone", "")
        if p and p in existing_phones:
            continue
        rows.append([
            today,
            l.get("name", ""),
            l.get("category", ""),
            l.get("city", ""),
            p,
            l.get("email", ""),
            l.get("website_status", "").upper(),
            l.get("website", ""),
            l.get("instagram", ""),
            str(l.get("rating", "")),
            str(l.get("reviews", "")),
            l.get("maps_url", ""),
            str(l.get("score", "")),
            l.get("source", ""),
        ])
        if p:
            existing_phones.add(p)
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
    return len(rows)


def _log(msg: str):
    line = str(msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── SCHEDULER ─────────────────────────────────────────────────────────────────
def _scheduler():
    last_triggered = None
    while True:
        try:
            s   = load_state()
            cfg = load_settings()
            if s.get("automation_on") and not s.get("running"):
                now_hm      = datetime.now().strftime("%H:%M")
                target_time = cfg.get("schedule_time", "09:00")
                today       = datetime.now().strftime("%Y-%m-%d")
                trigger_key = f"{today}_{target_time}"

                if now_hm == target_time and last_triggered != trigger_key:
                    with _run_lock:
                        # Double-check inside the lock
                        if load_state().get("running"):
                            continue
                        st = load_state()
                        st["running"] = True
                        save_state(st)

                    last_triggered = trigger_key
                    _log(f"[Scheduler] ⏰ Auto-triggered at {now_hm}")
                    threading.Thread(
                        target=_run_pipeline, daemon=True
                    ).start()
        except Exception as e:
            _log(f"[Scheduler] Error: {e}")
        time.sleep(30)


# ── SERVE FRONTEND ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    try:
        with open(FRONTEND) as f:
            return f.read()
    except FileNotFoundError:
        return (
            "<h1>Error: frontend/index.html not found</h1>"
            "<p>Make sure the file exists in the <code>frontend/</code> "
            "folder next to app.py</p>"
        ), 404


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


# ── MAIN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=_scheduler, daemon=True).start()
    print("\n" + "=" * 50)
    print("  🚀  Auren Leads is running!")
    print("  👉  Open:  http://localhost:5000")
    print("  ⚠️   DO NOT open index.html directly as a file")
    print("=" * 50 + "\n")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
