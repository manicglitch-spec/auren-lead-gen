"""
Auren Leads — Flask Backend (Improved Production Version)
Run: python app.py → open http://localhost:5000
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import json, os, threading, time, traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR,"settings.json")
STATE_FILE = os.path.join(BASE_DIR,"state.json")
LOG_FILE = os.path.join(BASE_DIR,"run.log")

FRONTEND = os.path.join(BASE_DIR,"frontend","index.html")

# ---------------------------------------------------------
# DEFAULT SETTINGS
# ---------------------------------------------------------

DEFAULTS = {
"google_maps_key":"",
"hunter_key":"",
"sheet_id":"",
"creds_json":"",
"leads_per_run":100,
"schedule_time":"09:00",

"quality_split":{"no":70,"bad":20,"ok":10},

"active_cities":[
"Mumbai","Delhi","Bangalore","Pune","Hyderabad","Chennai",
"Ahmedabad","Kolkata","Jaipur","Surat"
],

"active_cats":[
"cafe","restaurant","salon","gym","real estate agent",
"dental clinic","interior designer","clothing store",
"jewelry store","coaching institute"
]
}

# ---------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------

def load_settings():
    s=dict(DEFAULTS)
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            s.update(json.load(f))
    return s

def save_settings(data):
    s=load_settings()
    s.update(data)
    with open(SETTINGS_FILE,"w") as f:
        json.dump(s,f,indent=2)

# ---------------------------------------------------------
# STATE
# ---------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)

    return {
        "automation_on":False,
        "running":False,
        "progress":"",
        "progress_pct":0,
        "last_error":"",
        "last_run":None,
        "total_leads":0
    }

def save_state(s):
    with open(STATE_FILE,"w") as f:
        json.dump(s,f,indent=2)

# ---------------------------------------------------------
# LOGGING
# ---------------------------------------------------------

def _log(msg):
    print(msg)
    with open(LOG_FILE,"a") as f:
        f.write(msg+"\n")

# ---------------------------------------------------------
# API ROUTES
# ---------------------------------------------------------

@app.route("/api/settings",methods=["GET"])
def api_get_settings():
    s=load_settings()
    safe={k:v for k,v in s.items() if k!="creds_json"}
    safe["has_creds"]=bool(s.get("creds_json"))
    return jsonify(safe)

@app.route("/api/settings",methods=["POST"])
def api_post_settings():
    save_settings(request.json)
    return jsonify({"ok":True})

@app.route("/api/state")
def api_state():
    return jsonify(load_state())

@app.route("/api/run-now",methods=["POST"])
def api_run_now():

    s=load_state()

    if s["running"]:
        return jsonify({"ok":False,"msg":"already running"})

    threading.Thread(target=_run_pipeline,daemon=True).start()

    return jsonify({"ok":True})

# ---------------------------------------------------------
# SCRAPING HELPERS
# ---------------------------------------------------------

BAD_DOMAINS=[
"wix.com","weebly.com","blogspot.com","wordpress.com",
"business.site","sites.google.com","carrd.co"
]

HDR={
"User-Agent":"Mozilla/5.0"
}

def _cls(url):
    if not url:
        return "none"

    u=url.lower()

    for d in BAD_DOMAINS:
        if d in u:
            return "bad"

    return "ok"

# ---------------------------------------------------------
# GOOGLE MAPS SEARCH WITH PAGINATION
# ---------------------------------------------------------

def _search_maps(city,cat,key,max_r=30):

    import requests

    results=[]
    token=None

    for _ in range(3):

        params={
        "query":f"{cat} in {city} India",
        "key":key
        }

        if token:
            params["pagetoken"]=token

        r=requests.get(
        "https://maps.googleapis.com/maps/api/place/textsearch/json",
        params=params,
        timeout=12
        )

        if r.status_code!=200:
            break

        data=r.json()

        results+=data.get("results",[])

        token=data.get("next_page_token")

        if not token:
            break

        time.sleep(2)

    pids=[p["place_id"] for p in results[:max_r]]

    detailed=[]

    with ThreadPoolExecutor(max_workers=6) as ex:
        for d in ex.map(lambda pid:_maps_detail(pid,key),pids):
            if d:
                detailed.append(d)

    return detailed

def _maps_detail(pid,key):

    import requests

    try:

        r=requests.get(
        "https://maps.googleapis.com/maps/api/place/details/json",
        params={
        "place_id":pid,
        "fields":"name,formatted_address,formatted_phone_number,website,rating,user_ratings_total,url",
        "key":key
        },
        timeout=8
        )

        if r.status_code!=200:
            return None

        res=r.json().get("result",{})

        w=res.get("website","")

        return {

        "name":res.get("name",""),
        "address":res.get("formatted_address",""),
        "phone":res.get("formatted_phone_number",""),
        "website":w,
        "website_status":_cls(w),
        "rating":res.get("rating",0),
        "reviews":res.get("user_ratings_total",0),
        "maps_url":res.get("url",""),
        "source":"Google Maps"
        }

    except:
        return None

# ---------------------------------------------------------
# LINKEDIN FINDER
# ---------------------------------------------------------

def _find_linkedin(name,city):

    import requests,re

    try:

        r=requests.get(
        "https://www.google.com/search",
        params={"q":f'"{name}" "{city}" site:linkedin.com/company'},
        headers=HDR,
        timeout=5
        )

        m=re.search(r"https://www.linkedin.com/company/[a-zA-Z0-9\-_/]+",r.text)

        if m:
            return m.group()

    except:
        pass

    return ""

# ---------------------------------------------------------
# EMAIL FINDER
# ---------------------------------------------------------

def _find_email(name,city,website,cfg):

    import requests,re

    ER=re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

    if website:

        try:
            r=requests.get(website,timeout=4)

            emails=ER.findall(r.text)

            if emails:
                return emails[0]

        except:
            pass

    return ""

# ---------------------------------------------------------
# INSTAGRAM FINDER
# ---------------------------------------------------------

def _find_instagram(name,city,website):

    import requests,re

    try:

        r=requests.get(
        "https://www.google.com/search",
        params={"q":f'site:instagram.com "{name}" {city}'},
        headers=HDR,
        timeout=5
        )

        m=re.search(r"instagram\.com/[a-zA-Z0-9_.]+",r.text)

        if m:
            return "@"+m.group().split("/")[-1]

    except:
        pass

    return ""

# ---------------------------------------------------------
# SCORING
# ---------------------------------------------------------

def _score(lead):

    s=0

    ws=lead.get("website_status")

    if ws=="none":
        s+=50

    elif ws=="bad":
        s+=25

    if lead.get("phone"):
        s+=15

    if lead.get("email"):
        s+=20

    if lead.get("instagram"):
        s+=10

    if lead.get("linkedin"):
        s+=10

    try:
        if float(lead.get("rating",0))>=4.3:
            s+=10
    except:
        pass

    return min(s,100)

# ---------------------------------------------------------
# PIPELINE
# ---------------------------------------------------------

def _run_pipeline():

    s=load_state()
    s["running"]=True
    s["progress"]="Starting..."
    save_state(s)

    try:

        cfg=load_settings()

        cities=cfg["active_cities"]
        cats=cfg["active_cats"]

        target=cfg["leads_per_run"]

        key=cfg["google_maps_key"]

        raw=[]

        combos=[(c,k) for c in cities for k in cats]

        with ThreadPoolExecutor(max_workers=16) as ex:

            futures=[ex.submit(_search_maps,c,k,key) for c,k in combos]

            for fut in as_completed(futures):

                raw+=fut.result()

                if len(raw)>=target*3:
                    break

        _log(f"raw pool {len(raw)}")

        enriched=[]

        def enrich(l):

            l["email"]=_find_email(l["name"],"",l.get("website",""),cfg)

            l["instagram"]=_find_instagram(l["name"],"",l.get("website",""))

            l["linkedin"]=_find_linkedin(l["name"],"")

            l["score"]=_score(l)

            return l

        with ThreadPoolExecutor(max_workers=20) as ex:

            futures=[ex.submit(enrich,l) for l in raw[:target*2]]

            for fut in as_completed(futures):
                enriched.append(fut.result())

        enriched=sorted(enriched,key=lambda x:x["score"],reverse=True)

        final=enriched[:target]

        _log(f"final leads {len(final)}")

        s=load_state()

        s["running"]=False
        s["last_run"]=datetime.now().isoformat()
        s["total_leads"]+=len(final)
        s["progress"]="Done"

        save_state(s)

    except Exception as e:

        err=traceback.format_exc()

        _log(err)

        s=load_state()

        s["running"]=False
        s["last_error"]=str(e)

        save_state(s)

# ---------------------------------------------------------
# SERVE FRONTEND
# ---------------------------------------------------------

@app.route("/")
def index():

    try:
        with open(FRONTEND) as f:
            return f.read()

    except:
        return "<h1>Frontend missing</h1>"

@app.route("/health")
def health():
    return jsonify({"status":"ok","time":datetime.now().isoformat()})

# ---------------------------------------------------------
# RUN
# ---------------------------------------------------------

if __name__=="__main__":

    print("\nAuren Leads running on http://localhost:5000\n")

    app.run(host="0.0.0.0",port=5000)
