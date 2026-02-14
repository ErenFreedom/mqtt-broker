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

# ---------- STORAGE ----------
APP_NAME = "MyDesktopApp"
BASE_DIR = os.path.join(os.getenv("LOCALAPPDATA"), APP_NAME)
os.makedirs(BASE_DIR, exist_ok=True)

LOGIN_FILE = os.path.join(BASE_DIR, "login.json")
API_FILE = os.path.join(BASE_DIR, "apis.json")

# ---------- FLASK ----------
def resource_path(relative):
    try:
        base = sys._MEIPASS
    except Exception:
        base = os.path.abspath(".")
    return os.path.join(base, relative)

app = Flask(__name__, template_folder=resource_path("templates"))
app.secret_key = "secret_key_change_me"

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------- GLOBALS ----------
api_store = {}
running_threads = {}
current_token = None

# ---------- LOAD STORED DATA ----------
def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)

def load_json(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

api_store = load_json(API_FILE)

# ---------- MQTT CONFIG ----------
MQTT_BROKER = "34.14.145.160"
MQTT_PORT = 1883
MQTT_USER = "mqttuser"
MQTT_PASS = "123456"

mqtt_client = mqtt.Client()
mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("✅ MQTT Connected")
    else:
        print("❌ MQTT Connection Failed", rc)

def on_disconnect(client, userdata, rc):
    print("⚠ MQTT Disconnected")

mqtt_client.on_connect = on_connect
mqtt_client.on_disconnect = on_disconnect
mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
mqtt_client.loop_start()

# ---------- TOKEN ----------
def request_token(url, user, pwd):
    global current_token
    payload = f"grant_type=password&username={user}&password={pwd}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        r = requests.post(url, data=payload, headers=headers, verify=False)
        j = r.json()
        token = j.get("access_token") or j.get("token")
        current_token = token
        return token
    except:
        return None

# ---------- VALUE EXTRACT ----------
def extract_value(response_json):
    try:
        return float(response_json[0]["Value"]["Value"])
    except:
        return None

# ---------- BACKGROUND PUBLISHER ----------
def api_publisher(api_name, api_url):
    topic = f"building/{api_name}"
    print(f"🚀 Started publishing {api_name}")

    while api_name in running_threads:
        try:
            headers = {"Authorization": f"Bearer {current_token}"}

            r = requests.get(api_url, headers=headers, verify=False)

            if r.status_code == 401:
                print("⚠ Token expired")
                time.sleep(5)
                continue

            value = extract_value(r.json())

            payload = {
                "device": api_name,
                "value": value,
                "timestamp": int(time.time())
            }

            mqtt_client.publish(topic, json.dumps(payload), qos=1, retain=True)
            print(f"📡 Published to {topic}:", payload)

        except Exception as e:
            print("Publish Error:", e)

        time.sleep(5)

    print(f"🛑 Stopped publishing {api_name}")

# ---------- ROUTES ----------
@app.route("/")
def index():
    login = load_json(LOGIN_FILE)
    if login:
        session.update(login)
        session["logged_in"] = True
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

        session.update({
            "logged_in": True,
            "username": request.form["username"],
            "token": token
        })

        save_json(LOGIN_FILE, session)
        return redirect("/welcome")

    return render_template("login.html")

@app.route("/welcome")
def welcome():
    if not session.get("logged_in"):
        return redirect("/login")
    return render_template("welcome.html", apis=api_store)

@app.route("/activation", methods=["POST"])
def activation():
    api_name = request.form["api_name"]
    api_url = request.form["api_url"]

    api_store[api_name] = api_url
    save_json(API_FILE, api_store)

    running_threads[api_name] = True

    t = threading.Thread(
        target=api_publisher,
        args=(api_name, api_url),
        daemon=True
    )
    t.start()

    flash("API Activated & Publishing Started", "success")
    return redirect("/welcome")

@app.route("/delete/<api_name>", methods=["POST"])
def delete_api(api_name):
    api_store.pop(api_name, None)
    save_json(API_FILE, api_store)
    running_threads.pop(api_name, None)
    flash(f"{api_name} Deleted", "info")
    return redirect("/welcome")

@app.route("/live/<api_name>")
def live(api_name):
    return render_template("live_data.html", api_name=api_name)

@app.route("/fetch/<api_name>")
def fetch(api_name):
    r = requests.get(
        api_store[api_name],
        headers={"Authorization": f"Bearer {current_token}"},
        verify=False
    )
    return jsonify(r.json())

@app.route("/logout")
def logout():
    global current_token
    current_token = None
    session.clear()
    if os.path.exists(LOGIN_FILE):
        os.remove(LOGIN_FILE)
    return redirect("/login")

# ---------- DESKTOP ----------
def start_flask():
    app.run("127.0.0.1", 5000, debug=False, use_reloader=False)

if __name__ == "__main__":
    threading.Thread(target=start_flask, daemon=True).start()
    webview.create_window("My Desktop App", "http://127.0.0.1:5000", width=1200, height=800)
    webview.start()
