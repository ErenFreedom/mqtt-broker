import threading
import requests
import urllib3
import os
import json
import time
import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, request, redirect, session, flash, jsonify

from database_service import (
    create_sensor, save_sensor_data, get_latest_sensor_value, save_client_info
)
from mqtt_service import CLIENT_INFO, publish_sensor_data

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
    """Server se ek naya token lo"""
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
            log.info("[TOKEN] ✅ New token fetched successfully")
        else:
            log.error("[TOKEN] ❌ Token not found in response")
        return token
    except Exception as e:
        log.error(f"[TOKEN ERROR] {e}")
        return None

def get_shared_token():
    """
    Shared token return karo.
    Agar token nahi hai to sirf ek baar fetch karo — lock se ensure karo
    ki ek saath sirf ek request jaaye.
    """
    global shared_token
    with shared_token_lock:
        if not shared_token:
            fetch_new_token()
        return shared_token

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
        try:
            # ✅ Shared token use karo — koi naya token request nahi
            token = get_shared_token()
            if not token:
                log.error(f"[API] No token for {api_name}, retrying in 10s")
                time.sleep(10)
                continue

            headers = {"Authorization": f"Bearer {token}"}
            r = requests.get(api_url, headers=headers, verify=False, timeout=5)

            if r.status_code == 401:
                # ✅ Token expire hua — sirf ek baar invalidate karo
                # Lock ensure karega ki sirf ek thread token refresh kare
                invalidate_token()
                log.warning(f"[API] Token expired, refreshing...")
                continue

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
    login_data = load_json(LOGIN_FILE)
    if not login_data:
        log.info("[AUTO RESTART] No login data found.")
        return
    t_api    = login_data.get("token_api")
    username = login_data.get("username")
    password = login_data.get("password")
    log.info("[AUTO RESTART] Starting saved API threads...")
    start_api_threads(t_api, username, password)

# ---------------- ROUTES ----------------
@app.route("/")
def index():
    login_data = load_json(LOGIN_FILE)
    if login_data:
        session.update(login_data)
        session["logged_in"] = True
        return redirect("/activate_client")
    return redirect("/login")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        # ✅ Login ke liye ek baar token verify karo
        try:
            r = requests.post(
                request.form["token_api"],
                data=f"grant_type=password&username={request.form['username']}&password={request.form['password']}",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                verify=False,
                timeout=5
            )
            r.raise_for_status()
            token = r.json().get("access_token")
        except:
            token = None

        if not token:
            flash("Login Failed", "danger")
            return render_template("login.html")

        data = {
            "username":  request.form["username"],
            "password":  request.form["password"],
            "token_api": request.form["token_api"]
        }
        save_json(LOGIN_FILE, data)
        session.update(data)
        session["logged_in"] = True

        # ✅ Credentials set karo aur shared token save karo
        set_token_credentials(data["token_api"], data["username"], data["password"])
        shared_token_ref = token  # Login ka token directly use karo
        global shared_token
        shared_token = token
        log.info("[LOGIN] Token saved from login response")

        start_api_threads(data["token_api"], data["username"], data["password"])
        return redirect("/activate_client")
    return render_template("login.html")

@app.route("/activate_client", methods=["GET", "POST"])
def activate_client():
    if not session.get("logged_in"):
        return redirect("/login")
    if request.method == "POST":
        client_id = request.form["client_id"]
        site_id   = request.form["site_id"]
        save_client_info(client_id, [site_id])
        session["client_verified"] = True
        session["client_id"]       = client_id
        session["site_id"]         = site_id
        CLIENT_INFO["client_id"]   = client_id
        CLIENT_INFO["site_id"]     = site_id
        return redirect("/welcome")
    return render_template("client_activation.html")

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
    restart_saved_apis()
    threading.Thread(target=start_flask, daemon=True).start()
    webview.create_window("Edge_connector", "http://127.0.0.1:5000", width=1200, height=800)
    webview.start()
