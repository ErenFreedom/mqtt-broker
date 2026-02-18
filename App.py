import requests
import urllib3
import threading
import webview
import os
import sys
import json
import time
import paho.mqtt.client as mqtt

from flask import Flask, render_template, request, redirect, session, flash, jsonify

# ================= STORAGE =================
APP_NAME = "MyDesktopApp"
BASE_DIR = os.path.join(os.getenv("LOCALAPPDATA"), APP_NAME)
os.makedirs(BASE_DIR, exist_ok=True)

LOGIN_FILE = os.path.join(BASE_DIR, "login.json")
API_FILE = os.path.join(BASE_DIR, "apis.json")

# ================= FLASK =================
def resource_path(relative):
    try:
        base = sys._MEIPASS
    except Exception:
        base = os.path.abspath(".")
    return os.path.join(base, relative)

app = Flask(__name__, template_folder=resource_path("templates"))
app.secret_key = "super_secret_key"

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================= GLOBALS =================
api_store = {}
running_threads = {}
current_token = None
api_status = {}

# ================= LOAD / SAVE =================
def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=4)

def load_json(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

api_store = load_json(API_FILE)

# ================= MQTT (v5 - No Warning) =================
MQTT_BROKER = "34.14.145.160"
MQTT_PORT = 1883
MQTT_USER = "mqttuser"
MQTT_PASS = "123456"

mqtt_client = mqtt.Client(protocol=mqtt.MQTTv5)
mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)

def on_connect(client, userdata, flags, reasonCode, properties):
    if reasonCode == 0:
        print("✅ MQTT Connected")
    else:
        print("❌ MQTT Connection Failed:", reasonCode)

def on_disconnect(client, userdata, reasonCode, properties):
    print("⚠ MQTT Disconnected")

mqtt_client.on_connect = on_connect
mqtt_client.on_disconnect = on_disconnect
mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
mqtt_client.loop_start()

# ================= TOKEN =================
def request_token(url, user, pwd):
    global current_token

    payload = f"grant_type=password&username={user}&password={pwd}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        r = requests.post(url, data=payload, headers=headers, verify=False, timeout=5)
        r.raise_for_status()
        token = r.json().get("access_token") or r.json().get("token")
        current_token = token
        return token
    except Exception as e:
        print("❌ Token request failed:", e)
        return None

# ================= VALUE EXTRACT =================
def extract_value(response_json):
    try:
        return float(response_json[0]["Value"]["Value"])
    except:
        return None

# ================= BACKGROUND PUBLISHER =================
def api_publisher(api_name, api_url, interval, token_api, username, password):

    topic = f"building/{api_name}"
    api_status[api_name] = "offline"

    print(f"🚀 Started {api_name} every {interval}s")

    global current_token

    while running_threads.get(api_name):

        try:
            # Get token if missing
            if not current_token:
                token = request_token(token_api, username, password)
                if not token:
                    time.sleep(interval)
                    continue

            headers = {"Authorization": f"Bearer {current_token}"}

            r = requests.get(api_url, headers=headers, verify=False, timeout=5)

            # Token expired
            if r.status_code == 401:
                print("⚠ Token expired. Refreshing...")
                current_token = None
                time.sleep(2)
                continue

            r.raise_for_status()

            value = extract_value(r.json())

            payload = {
                "device": api_name,
                "value": value,
                "timestamp": int(time.time())
            }

            mqtt_client.publish(topic, json.dumps(payload), qos=1, retain=True)

            api_status[api_name] = "online"
            print(f"📡 Published {api_name}:", payload)

        except Exception as e:
            api_status[api_name] = "offline"
            print(f"⚠ {api_name} error:", e)

        time.sleep(interval)

    print(f"🛑 Stopped {api_name}")

# ================= ROUTES =================
@app.route("/")
def index():

    login_data = load_json(LOGIN_FILE)

    if (
        login_data
        and login_data.get("username")
        and login_data.get("password")
        and login_data.get("token_api")
    ):

        session["logged_in"] = True
        session["username"] = login_data["username"]
        session["password"] = login_data["password"]
        session["token_api"] = login_data["token_api"]

        # Start saved APIs
        for name, data in api_store.items():
            if not running_threads.get(name):
                running_threads[name] = True
                threading.Thread(
                    target=api_publisher,
                    args=(
                        name,
                        data["url"],
                        data["interval"],
                        login_data["token_api"],
                        login_data["username"],
                        login_data["password"],
                    ),
                    daemon=True
                ).start()

        return redirect("/welcome")

    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    global current_token

    if request.method == "POST":

        token = request_token(
            request.form["token_api"],
            request.form["username"],
            request.form["password"]
        )

        if not token:
            flash("Login Failed", "danger")
            return render_template("login.html")

        login_data = {
            "username": request.form["username"],
            "password": request.form["password"],
            "token_api": request.form["token_api"]
        }

        save_json(LOGIN_FILE, login_data)

        session["logged_in"] = True
        session["username"] = login_data["username"]
        session["password"] = login_data["password"]
        session["token_api"] = login_data["token_api"]

        return redirect("/welcome")

    return render_template("login.html")


@app.route("/welcome")
def welcome():
    if not session.get("logged_in"):
        return redirect("/login")

    return render_template("welcome.html", apis=api_store, api_status=api_status)


@app.route("/activation", methods=["POST"])
def activation():

    api_name = request.form["api_name"]
    api_url = request.form["api_url"]
    interval = int(request.form["interval"])

    api_store[api_name] = {
        "url": api_url,
        "interval": interval
    }

    save_json(API_FILE, api_store)

    if not running_threads.get(api_name):
        running_threads[api_name] = True
        threading.Thread(
            target=api_publisher,
            args=(
                api_name,
                api_url,
                interval,
                session["token_api"],
                session["username"],
                session["password"]
            ),
            daemon=True
        ).start()

    flash("API Activated", "success")
    return redirect("/welcome")


@app.route("/delete/<api_name>", methods=["POST"])
def delete_api(api_name):

    running_threads.pop(api_name, None)
    api_store.pop(api_name, None)
    api_status.pop(api_name, None)

    save_json(API_FILE, api_store)

    flash("API Deleted", "info")
    return redirect("/welcome")


# -------- LIVE PAGE --------
@app.route("/live/<api_name>")
def live(api_name):
    if api_name not in api_store:
        return "API Not Found", 404
    return render_template("live_data.html", api_name=api_name)


@app.route("/fetch/<api_name>")
def fetch(api_name):

    if api_name not in api_store:
        return jsonify({"error": "API not found"})

    try:
        headers = {"Authorization": f"Bearer {current_token}"}

        r = requests.get(
            api_store[api_name]["url"],
            headers=headers,
            verify=False,
            timeout=5
        )

        if r.status_code == 401:
            return jsonify({"error": "Token expired"})

        r.raise_for_status()
        return jsonify(r.json())

    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/logout")
def logout():

    global current_token
    current_token = None

    session.clear()

    if os.path.exists(LOGIN_FILE):
        os.remove(LOGIN_FILE)

    return redirect("/login")


# ================= DESKTOP =================
def start_flask():
    app.run("127.0.0.1", 5000, debug=False, use_reloader=False)

if __name__ == "__main__":
    threading.Thread(target=start_flask, daemon=True).start()
    webview.create_window("My Desktop App", "http://127.0.0.1:5000", width=1200, height=800)
    webview.start()
