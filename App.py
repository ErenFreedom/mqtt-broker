import threading
import traceback
import requests
import urllib3
import os
import json
import time
import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, request, redirect, session, flash, jsonify
CLOUD_URL = "https://34.14.145.160/api/edge"
from database_service import (
    create_sensor, save_sensor_data, get_latest_sensor_value, save_client_info
)
from mqtt_service import CLIENT_INFO, publish_sensor_data
APP_NAME = "MyDesktopApp"
 
BASE_DIR = os.path.join(os.getenv("LOCALAPPDATA") or ".", APP_NAME)
os.makedirs(BASE_DIR, exist_ok=True)
 
DEVICE_FILE = os.path.join(BASE_DIR, "device.json")
LOGIN_FILE  = os.path.join(BASE_DIR, "login.json")
API_FILE    = os.path.join(BASE_DIR, "apis.json")
LOG_FILE    = os.path.join(BASE_DIR, "app.log")
DEVICE_STATE = {
    "verified": False
}

import hashlib
import uuid
import platform

import hmac

DEVICE_FILE = os.path.join(BASE_DIR, "device.json")
TOKEN_FILE = os.path.join(BASE_DIR, "token.json")

def clear_token_on_start():
    if os.path.exists(TOKEN_FILE):
        data = load_token()

        # Keep creds, remove token
        data["access_token"] = None

        save_token(data)
        log.info("[TOKEN] Cleared access token on app start")

def save_token(data):
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f)

def load_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            return json.load(f)
    return {}

def save_device(data):
    with open(DEVICE_FILE, "w") as f:
        json.dump(data, f)

def load_device():
    if os.path.exists(DEVICE_FILE):
        with open(DEVICE_FILE, "r") as f:
            return json.load(f)
    return {}

def generate_signature(email, device_secret):
    timestamp = int(time.time() * 1000)
    payload = f"{email}:{timestamp}"

    signature = hmac.new(
        device_secret.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()

    return signature, timestamp

def generate_fingerprint():
    raw = f"{platform.node()}-{uuid.getnode()}-{platform.system()}"
    return hashlib.sha256(raw.encode()).hexdigest()

APP_NAME = "MyDesktopApp"
BASE_DIR = os.path.join(os.getenv("LOCALAPPDATA"), APP_NAME)
os.makedirs(BASE_DIR, exist_ok=True)
LOGIN_FILE = os.path.join(BASE_DIR, "login.json")
API_FILE   = os.path.join(BASE_DIR, "apis.json")
LOG_FILE   = os.path.join(BASE_DIR, "app.log")

# ✅ File logging
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger()

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = "mydesktopapp-secret-key-2026"

api_store       = {}
running_threads = {}
api_status      = {}

executor = ThreadPoolExecutor(max_workers=200)

# ============================================================
# ✅ SHARED TOKEN — Sirf ek token, sab APIs ke liye
# ============================================================
shared_token      = None
shared_token_lock = threading.Lock()
token_api_url     = None
token_username    = None
token_password    = None

def fetch_new_token():
    global shared_token

    try:
        r = requests.post(
            token_api_url,
            data=f"grant_type=password&username={token_username}&password={token_password}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            verify=False,
            timeout=5
        )

        r.raise_for_status()

        token = r.json().get("access_token")

        if token:
            shared_token = token

            # 🔥 SAVE UPDATED TOKEN
            save_token({
                "access_token": token,
                "api_url": token_api_url,
                "username": token_username,
                "password": token_password,
                "last_updated": int(time.time())
            })

            log.info("[TOKEN] ✅ Refreshed & saved new token")

        return token

    except Exception as e:
        log.error(f"[TOKEN ERROR] {e}")
        return None
    
    
    
def get_shared_token():
    global shared_token

    with shared_token_lock:

        # in-memory
        if shared_token:
            return shared_token

        token_data = load_token()

        # file token
        if token_data.get("access_token"):
            shared_token = token_data["access_token"]
            return shared_token

        # 🔥 NO TOKEN → FETCH USING STORED CREDS
        if token_data.get("api_url") and token_data.get("username") and token_data.get("password"):
            set_token_credentials(
                token_data.get("api_url"),
                token_data.get("username"),
                token_data.get("password")
            )
            return fetch_new_token()

        log.error("[TOKEN] No credentials available")
        return None
    
    
def invalidate_token():
    """Token expire hone pe clear karo — agli request pe ek baar refresh hoga"""
    global shared_token
    with shared_token_lock:
        shared_token = None
        log.info("[TOKEN] Token invalidated — will refresh on next use")

def set_token_credentials(api_url, username, password):
    """Login ke waqt credentials save karo"""
    global token_api_url, token_username, token_password, shared_token
    token_api_url  = api_url
    token_username = username
    token_password = password
    shared_token   = None  # Naya credentials — token reset
    log.info("[TOKEN] Credentials updated")

# ============================================================

# ---------------- JSON ----------------
def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=4)

def load_json(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

api_store = load_json(API_FILE)

# ---------------- VALUE + QUALITY EXTRACT ----------------
def extract_data(response_json):
    try:
        value_obj    = response_json[0]["Value"]
        value        = float(value_obj["Value"])
        quality      = value_obj.get("Quality", "")
        quality_good = value_obj.get("QualityGood", False)
        return value, quality, quality_good
    except:
        return None, None, False

# ---------------- FIND API KEY ----------------
def find_api_key(api_name):
    api_name = api_name.strip()
    if api_name in api_store:
        return api_name
    for key in api_store.keys():
        if key.strip() == api_name:
            return key
    return None

# ---------------- API WORKER ----------------
def api_worker(api_name, data):
    location  = data["location"]
    api_url   = data["url"]
    interval  = data["interval"]
    sensor_id = data["sensor_id"]

    api_status[api_name] = "offline"
    log.info(f"[THREAD STARTED] {api_name} | sensor_id={sensor_id}")

    while running_threads.get(api_name):

        if not DEVICE_STATE["verified"]:
           log.warning("❌ Device not activated, skipping publish")
           time.sleep(interval)
           continue

# 🔥 NEW CHECK (DESIGO TOKEN)
        token = get_shared_token()

        if not token:
          log.error(f"[API] No token for {api_name}, retrying in 10s")
          time.sleep(10)
          continue

        try:
            token = get_shared_token()
            if not token:
                log.error(f"[API] No token for {api_name}, retrying in 10s")
                time.sleep(10)
                continue

            headers = {"Authorization": f"Bearer {token}"}
            r = requests.get(api_url, headers=headers, verify=False, timeout=5)

            if r.status_code == 401:
                log.warning(f"[API] Token expired, refreshing...")
                invalidate_token()
                
                token = get_shared_token()
                
                if not token:
                  log.error(f"[API] Token refresh failed for {api_name}")
                  time.sleep(10)
                  continue
                
                headers = {"Authorization": f"Bearer {token}"}
                r = requests.get(api_url, headers=headers, verify=False, timeout=5)

            r.raise_for_status()
            response_json = r.json()

            value, quality, quality_good = extract_data(response_json)

            if value is not None:
                utc_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

                save_sensor_data(sensor_id, value, response_json)

                publish_sensor_data(
                    sensor_id    = sensor_id,
                    sensor_name  = api_name,
                    location     = location,
                    value        = value,
                    quality      = quality,
                    quality_good = quality_good,
                    timestamp    = utc_timestamp
                )

                log.info(f"[DATA] {api_name} | value={value} | ts={utc_timestamp}")

            api_status[api_name] = "online"

        except Exception as e:
            api_status[api_name] = "offline"
            log.error(f"[API Error] {api_name}: {e}")

        time.sleep(interval)

    log.info(f"[THREAD STOPPED] {api_name}")
    
    
    
    
# ---------------- START THREADS ----------------
def start_api_threads(t_api, username, password):
    set_token_credentials(t_api, username, password)

    if not api_store:
        log.info("[THREADS] No saved APIs found.")
        return

    for api_name, data in api_store.items():
        if running_threads.get(api_name):
            log.info(f"[THREADS] Already running: {api_name}")
            continue
        running_threads[api_name] = True
        executor.submit(api_worker, api_name, data)
        log.info(f"[THREADS] Started: {api_name} | Total: {len(running_threads)}")

# ---------------- AUTO RESTART ----------------
def restart_saved_apis():

    # 🔥 LOAD DEVICE (activation state)
    device_data = load_device()
    device_secret = device_data.get("device_secret")

    if not device_secret:
        log.info("[AUTO RESTART] Device not activated yet.")
        return

    # 🔥 LOAD DESIGO TOKEN DATA
    token_data = load_token()

    if not token_data.get("access_token"):
        log.info("[AUTO RESTART] No Desigo token found.")
        return

    # 🔥 SET TOKEN CONFIG (IMPORTANT)
    set_token_credentials(
        token_data.get("api_url"),
        token_data.get("username"),
        token_data.get("password")
    )

    log.info("[AUTO RESTART] Restarting APIs...")

    DEVICE_STATE["verified"] = True

    # 🔥 START THREADS
    start_api_threads(
        token_api_url,
        token_username,
        token_password
    )
 
# ---------------- ROUTES ----------------
@app.route("/")
def index():
    """
     Entry point of app

    NEW FLOW:
    - If user already logged in → go to dashboard
    - Else → go to login
    """

    if session.get("logged_in"):
        return redirect("/welcome")

    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":

        email = request.form["email"]
        password = request.form["password"]

        fingerprint = generate_fingerprint()

        try:
            device_data = load_device()
            device_secret = device_data.get("device_secret")

            payload = {
                "email": email,
                "password": password,
                "machine_fingerprint": fingerprint
            }

            if device_secret:
                signature, timestamp = generate_signature(email, device_secret)
                payload["signature"] = signature
                payload["timestamp"] = timestamp

            res = requests.post(
                f"{CLOUD_URL}/login-site-admin",
                json=payload,
                verify=False
            )

            data = res.json()

            if res.status_code != 200:
                flash(data.get("message", "Login failed"), "danger")
                return render_template("login.html")

            # ACTIVATION FLOW
            if data.get("activation_required"):
                session["email"] = email
                session["password"] = password
                session["site_id"] = data["site_id"]
                session["organization_id"] = data["organization_id"]
                return redirect("/activate_client")

            # SUCCESS LOGIN
            session["logged_in"] = True
            session["client_verified"] = True
            DEVICE_STATE["verified"] = True 

            session["client_id"] = data["organization_id"]
            session["site_id"] = data["site_id"]

            CLIENT_INFO["client_id"] = data["organization_id"]
            CLIENT_INFO["site_id"] = data["site_id"]

            # STORE DEVICE SECRET
            if data.get("device_secret"):
                save_device({
                    "device_secret": data["device_secret"]
                })
                log.info("✅ Device secret saved locally")

            # 🔥 IMPORTANT CHANGE
            global shared_token
            shared_token = None  # force fresh desigo login

            # 🔥 ALWAYS GO TO DESIGO LOGIN
            return redirect("/desigo_login")

        except Exception as e:
            log.error(f"[LOGIN ERROR] {e}")
            log.error(traceback.format_exc())
            flash("Server error", "danger")

    return render_template("login.html")


@app.route("/activate_client", methods=["GET", "POST"])
def activate_client():
    if not session.get("email"):
        return redirect("/login")

    if request.method == "POST":

        fingerprint = generate_fingerprint()
        site_id = session.get("site_id")

        try:
            res = requests.post(
                f"{CLOUD_URL}/request-activation",
                json={
                    "site_id": site_id,
                    "machine_fingerprint": fingerprint
                },
                verify=False
            )

            data = res.json()

            if res.status_code != 200:
                flash(data.get("message", "Activation failed"), "danger")
                return redirect("/activate_client")

            # ❌ DO NOT MARK VERIFIED
            # ❌ DO NOT STORE device_secret

            flash("✅ Activation request sent. Waiting for admin approval.", "warning")

            return redirect("/login")

        except Exception as e:
            log.error(f"[ACTIVATION ERROR] {e}")
            flash("Activation error", "danger")

    return render_template("client_activation.html")


@app.route("/desigo_login", methods=["GET", "POST"])
def desigo_login():

    if not session.get("logged_in"):
        return redirect("/login")

    if request.method == "POST":

        api_url = request.form["api_url"]
        username = request.form["username"]
        password = request.form["password"]

        try:
            res = requests.post(
                api_url,
                data=f"grant_type=password&username={username}&password={password}",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                verify=False,
                timeout=5
            )

            if res.status_code != 200:
                flash(f"Invalid Desigo credentials | {res.text}", "danger")
                return render_template("desigo_login.html")

            token = res.json().get("access_token")

            if not token:
                flash("Token not received from Desigo", "danger")
                return render_template("desigo_login.html")

            # ✅ SAVE EVERYTHING (IMPORTANT)
            save_token({
                "access_token": token,
                "api_url": api_url,
                "username": username,
                "password": password,
                "last_updated": int(time.time())
            })

            # ✅ SET GLOBAL TOKEN CONFIG
            set_token_credentials(api_url, username, password)

            flash("✅ Desigo connected successfully", "success")

            return redirect("/welcome")

        except Exception as e:
            log.error(f"[DESIGO LOGIN ERROR] {e}")
            log.error(traceback.format_exc())

            flash("Connection error", "danger")

    return render_template("desigo_login.html")


@app.route("/welcome")
def welcome():
    if not session.get("logged_in"):
        return redirect("/login")
    if not session.get("client_verified"):
        return redirect("/activate_client")
    return render_template("welcome.html", apis=api_store, api_status=api_status)

@app.route("/activation", methods=["POST"])
def activation():
    location  = request.form["location"]
    api_name  = request.form["api_name"]
    api_url   = request.form["api_url"]
    interval  = int(request.form["interval"])
    site_id   = session.get("site_id")
    sensor_id = create_sensor(site_id, api_name, location, api_url, interval)
    api_store[api_name] = {
        "location":  location,
        "url":       api_url,
        "interval":  interval,
        "sensor_id": sensor_id
    }
    save_json(API_FILE, api_store)
    running_threads[api_name] = True
    executor.submit(api_worker, api_name, api_store[api_name])
    log.info(f"[ACTIVATED] {api_name} | sensor_id={sensor_id} | total={len(api_store)}")
    return redirect("/welcome")

@app.route("/live/<path:api_name>")
def live(api_name):
    return render_template("live.html", api_name=api_name)

@app.route("/fetch/<path:api_name>")
def fetch(api_name):
    matched_key = find_api_key(api_name)
    if not matched_key:
        return jsonify({"value": None})
    sensor_id = api_store[matched_key]["sensor_id"]
    value = get_latest_sensor_value(sensor_id)
    return jsonify({"value": value})

@app.route("/delete/<path:api_name>", methods=["POST"])
def delete_api(api_name):
    matched_key = find_api_key(api_name)
    if matched_key:
        running_threads[matched_key] = False
        api_store.pop(matched_key, None)
        api_status.pop(matched_key, None)
        save_json(API_FILE, api_store)
        log.info(f"[DELETE] {matched_key}")
    return redirect("/welcome")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ---------------- DESKTOP ----------------
def start_flask():
    app.run("127.0.0.1", 5000, debug=False, use_reloader=False)

if __name__ == "__main__":
    import webview

    clear_token_on_start()   
    restart_saved_apis()

    threading.Thread(target=start_flask, daemon=True).start()
    webview.create_window("Edge_connector", "http://127.0.0.1:5000", width=1200, height=800)
    webview.start()
