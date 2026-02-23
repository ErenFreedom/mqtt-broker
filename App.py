import threading
import requests
import urllib3
import os
import json
import time
from datetime import datetime, timezone
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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = os.urandom(24)

api_store       = {}
running_threads = {}
api_status      = {}

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

# ---------------- TOKEN ----------------
def request_token(url, user, pwd):
    try:
        r = requests.post(
            url,
            data=f"grant_type=password&username={user}&password={pwd}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            verify=False,
            timeout=5
        )
        r.raise_for_status()
        return r.json().get("access_token")
    except:
        return None

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

# ---------------- API WORKER ----------------
def api_worker(api_name, data, token_api, username, password):
    location  = data["location"]
    api_url   = data["url"]
    interval  = data["interval"]
    sensor_id = data["sensor_id"]

    token = None
    api_status[api_name] = "offline"

    while running_threads.get(api_name):
        try:
            if not token:
                token = request_token(token_api, username, password)

            headers = {"Authorization": f"Bearer {token}"}
            r = requests.get(api_url, headers=headers, verify=False, timeout=5)

            if r.status_code == 401:
                token = None
                continue

            r.raise_for_status()
            response_json = r.json()

            value, quality, quality_good = extract_data(response_json)

            if value is not None:
                # ✅ Fetch hone ke waqt UTC+0 timestamp lock karo
                utc_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

                # ✅ Database mein save karo
                save_sensor_data(sensor_id, value, response_json)

                # ✅ MQTT broker pe same timestamp ke saath publish karo
                publish_sensor_data(
                    sensor_id    = sensor_id,
                    sensor_name  = api_name,
                    location     = location,
                    value        = value,
                    quality      = quality,
                    quality_good = quality_good,
                    timestamp    = utc_timestamp
                )

            api_status[api_name] = "online"

        except Exception as e:
            api_status[api_name] = "offline"
            print(f"[API Error] {api_name}: {e}")

        time.sleep(interval)

    print(f"[THREAD STOPPED] {api_name}")

# ---------------- START THREADS ----------------
def start_api_threads(token_api, username, password):
    """Saved APIs ke liye threads start karo — duplicate check ke saath"""
    if not api_store:
        print("[THREADS] No saved APIs found.")
        return

    for api_name, data in api_store.items():
        # ✅ Agar thread pehle se chal raha hai to dobara mat chalao
        if running_threads.get(api_name):
            print(f"[THREADS] Already running: {api_name}")
            continue

        running_threads[api_name] = True
        threading.Thread(
            target=api_worker,
            args=(api_name, data, token_api, username, password),
            daemon=True
        ).start()
        print(f"[THREADS] Started: {api_name}")

# ---------------- STOP ALL THREADS ----------------
def stop_all_threads():
    """Sab running threads band karo"""
    for api_name in list(running_threads.keys()):
        running_threads[api_name] = False
    running_threads.clear()
    api_status.clear()
    print("[THREADS] All threads stopped.")

# ---------------- AUTO RESTART ON APP STARTUP ----------------
def restart_saved_apis():
    """App open hone par saved login se threads restart karo"""
    login_data = load_json(LOGIN_FILE)
    if not login_data:
        print("[AUTO RESTART] No login data found, skipping.")
        return

    token_api = login_data.get("token_api")
    username  = login_data.get("username")
    password  = login_data.get("password")

    print("[AUTO RESTART] Starting saved API threads...")
    start_api_threads(token_api, username, password)

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
        token = request_token(request.form["token_api"], request.form["username"], request.form["password"])
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

        # ✅ Login ke baad threads start karo
        # Case 1: App close → reopen → login (restart_saved_apis already chal chuka hai startup pe)
        # Case 2: Logout → login (threads band the, ab restart karo)
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

        CLIENT_INFO["client_id"] = client_id
        CLIENT_INFO["site_id"]   = site_id

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
    threading.Thread(
        target=api_worker,
        args=(api_name, api_store[api_name], session["token_api"], session["username"], session["password"]),
        daemon=True
    ).start()

    return redirect("/welcome")

@app.route("/live/<api_name>")
def live(api_name):
    return render_template("live.html", api_name=api_name)

@app.route("/fetch/<api_name>")
def fetch(api_name):
    if api_name not in api_store:
        return jsonify({"value": None})
    sensor_id = api_store[api_name]["sensor_id"]
    value = get_latest_sensor_value(sensor_id)
    return jsonify({"value": value})

@app.route("/delete/<api_name>", methods=["POST"])
def delete_api(api_name):
    running_threads[api_name] = False
    api_store.pop(api_name, None)
    api_status.pop(api_name, None)
    save_json(API_FILE, api_store)
    return redirect("/welcome")

@app.route("/logout")
def logout():
    # ✅ Pehle sab threads band karo
    stop_all_threads()

    session.clear()
    if os.path.exists(LOGIN_FILE):
        os.remove(LOGIN_FILE)
    return redirect("/login")

# ---------------- DESKTOP ----------------
def start_flask():
    app.run("127.0.0.1", 5000, debug=False, use_reloader=False)

if __name__ == "__main__":
    import webview

    # ✅ App open hone par saved APIs automatically restart hongi
    restart_saved_apis()

    threading.Thread(target=start_flask, daemon=True).start()
    webview.create_window("My Desktop App", "http://127.0.0.1:5000", width=1200, height=800)
    webview.start()
