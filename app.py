"""
Auren Leads Backend
Stable production version
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import os, json, threading, traceback, time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SETTINGS_FILE = os.path.join(BASE_DIR,"settings.json")
STATE_FILE = os.path.join(BASE_DIR,"state.json")
LOG_FILE = os.path.join(BASE_DIR,"run.log")

FRONTEND = os.path.join(BASE_DIR,"frontend","index.html")

# ---------------------------------------------------
# DEFAULT SETTINGS
# ---------------------------------------------------

DEFAULTS = {
"google_maps_key":"",
"hunter_key":"",
"sheet_id":"",
"creds_json":"",
"schedule_time":"09:00",
"leads_per_run":50,
"quality_split":{"no":70,"bad":20,"ok":10},
"active_cities":["Mumbai","Delhi","Bangalore","Pune","Hyderabad"],
"active_cats":["cafe","restaurant","gym","salon","real estate agent"]
}

# ---------------------------------------------------
# SETTINGS
# ---------------------------------------------------

def load_settings():

    s=dict(DEFAULTS)

    if os.path.exists(SETTINGS_FILE):

        try:
            with open(SETTINGS_FILE) as f:
                s.update(json.load(f))
        except:
            pass

    return s


def save_settings(data):

    s=load_settings()
    s.update(data)

    with open(SETTINGS_FILE,"w") as f:
        json.dump(s,f,indent=2)


# ---------------------------------------------------
# STATE
# ---------------------------------------------------

def load_state():

    if os.path.exists(STATE_FILE):

        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except:
            pass

    return {
        "running":False,
        "progress":"",
        "progress_pct":0,
        "last_error":"",
        "total_leads":0,
        "last_run":None
    }


def save_state(s):

    with open(STATE_FILE,"w") as f:
        json.dump(s,f,indent=2)


# ---------------------------------------------------
# LOGGING
# ---------------------------------------------------

def log(msg):

    print(msg)

    try:
        with open(LOG_FILE,"a") as f:
            f.write(msg+"\n")
    except:
        pass


# ---------------------------------------------------
# API ROUTES (REQUIRED BY FRONTEND)
# ---------------------------------------------------

@app.route("/api/settings",methods=["GET"])
def api_settings():

    s=load_settings()

    safe={k:v for k,v in s.items() if k!="creds_json"}

    safe["has_creds"]=bool(s.get("creds_json"))

    return jsonify(safe)


@app.route("/api/settings",methods=["POST"])
def api_settings_save():

    data=request.get_json(force=True,silent=True) or {}

    save_settings(data)

    return jsonify({"ok":True})


@app.route("/api/state")
def api_state():

    return jsonify(load_state())


@app.route("/api/run-now",methods=["POST"])
def api_run_now():

    s=load_state()

    if s.get("running"):
        return jsonify({"ok":False,"msg":"Already running"})

    threading.Thread(target=run_pipeline,daemon=True).start()

    return jsonify({"ok":True})


@app.route("/api/logs")
def api_logs():

    try:

        with open(LOG_FILE) as f:
            lines=f.readlines()[-120:]

        return jsonify({"log":"".join(lines)})

    except:
        return jsonify({"log":""})


@app.route("/api/leads")
def api_leads():

    return jsonify({"leads":[]})


@app.route("/api/verify-keys",methods=["POST"])
def verify_keys():

    import requests

    cfg=load_settings()

    res={}

    key=cfg.get("google_maps_key","")

    if key:

        try:

            r=requests.get(
            "https://maps.googleapis.com/maps/api/place/textsearch/json",
            params={"query":"cafe in Mumbai","key":key},
            timeout=10
            )

            st=r.json().get("status","ERROR")

            res["google_maps"]="ok" if st in ("OK","ZERO_RESULTS") else st

        except Exception as e:

            res["google_maps"]=str(e)

    else:

        res["google_maps"]="not set"

    return jsonify(res)


# ---------------------------------------------------
# SCRAPING HELPERS
# ---------------------------------------------------

BAD_DOMAINS=["wix","weebly","blogspot","wordpress","sites.google"]


def website_status(url):

    if not url:
        return "none"

    u=url.lower()

    for d in BAD_DOMAINS:

        if d in u:
            return "bad"

    return "ok"


# ---------------------------------------------------
# GOOGLE MAPS SEARCH (PAGINATED)
# ---------------------------------------------------

def search_maps(city,cat,key):

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
        timeout=10
        )

        if r.status_code!=200:
            break

        data=r.json()

        results+=data.get("results",[])

        token=data.get("next_page_token")

        if not token:
            break

        time.sleep(2)

    return results


# ---------------------------------------------------
# LINKEDIN FINDER
# ---------------------------------------------------

def find_linkedin(name,city):

    import requests,re

    try:

        r=requests.get(
        "https://www.google.com/search",
        params={"q":f'"{name}" "{city}" site:linkedin.com/company'},
        timeout=5
        )

        m=re.search(r"https://www.linkedin.com/company/[a-zA-Z0-9\-_/]+",r.text)

        if m:
            return m.group()

    except:
        pass

    return ""


# ---------------------------------------------------
# SCORING
# ---------------------------------------------------

def score(lead):

    s=0

    ws=lead.get("website_status")

    if ws=="none":
        s+=50
    elif ws=="bad":
        s+=25

    if lead.get("phone"):
        s+=15

    if lead.get("linkedin"):
        s+=10

    try:
        if float(lead.get("rating",0))>=4.2:
            s+=10
    except:
        pass

    return min(s,100)


# ---------------------------------------------------
# PIPELINE
# ---------------------------------------------------

def run_pipeline():

    s=load_state()

    s["running"]=True
    s["progress"]="Starting"
    s["progress_pct"]=0

    save_state(s)

    try:

        cfg=load_settings()

        key=cfg.get("google_maps_key")

        cities=cfg.get("active_cities")
        cats=cfg.get("active_cats")

        target=cfg.get("leads_per_run")

        raw=[]

        combos=[(c,k) for c in cities for k in cats]

        with ThreadPoolExecutor(max_workers=12) as ex:

            futures=[ex.submit(search_maps,c,k,key) for c,k in combos]

            for fut in as_completed(futures):

                raw+=fut.result()

                if len(raw)>=target*3:
                    break

        enriched=[]

        for r in raw[:target*2]:

            lead={
            "name":r.get("name"),
            "rating":r.get("rating",0),
            "website":r.get("website",""),
            "phone":r.get("formatted_phone_number",""),
            }

            lead["website_status"]=website_status(lead["website"])

            lead["linkedin"]=find_linkedin(lead["name"],"")

            lead["score"]=score(lead)

            enriched.append(lead)

        enriched.sort(key=lambda x:x["score"],reverse=True)

        final=enriched[:target]

        log(f"Generated {len(final)} leads")

        s=load_state()

        s["running"]=False
        s["last_run"]=datetime.now().isoformat()
        s["total_leads"]+=len(final)
        s["progress"]="Done"

        save_state(s)

    except Exception as e:

        log(traceback.format_exc())

        s=load_state()

        s["running"]=False
        s["last_error"]=str(e)

        save_state(s)


# ---------------------------------------------------
# FRONTEND
# ---------------------------------------------------

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


# ---------------------------------------------------
# RUN
# ---------------------------------------------------

if __name__=="__main__":

    print("Auren backend running")

    app.run(host="0.0.0.0",port=5000)
    if __name__=="__main__":

    port=int(os.environ.get("PORT",5000))

    print("Auren backend running")

    app.run(host="0.0.0.0",port=port)
