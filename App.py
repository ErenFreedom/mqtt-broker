import requests
import urllib3
import threading
import webview
import os
import sys
import json

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

# ---------- HELPERS ----------
def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)

def load_json(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

api_store = load_json(API_FILE)

# ---------- TOKEN ----------
def request_token(url, user, pwd):
    payload = f"grant_type=password&username={user}&password={pwd}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        r = requests.post(url, data=payload, headers=headers, verify=False)
        j = r.json()
        return j.get("access_token") or j.get("token")
    except:
        return None

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
    api_store[request.form["api_name"]] = request.form["api_url"]
    save_json(API_FILE, api_store)
    flash("API Activated", "success")
    return redirect("/welcome")

@app.route("/delete/<api_name>", methods=["POST"])
def delete_api(api_name):
    api_store.pop(api_name, None)
    save_json(API_FILE, api_store)
    flash(f"{api_name} Deleted", "info")
    return redirect("/welcome")

@app.route("/live/<api_name>")
def live(api_name):
    return render_template("live_data.html", api_name=api_name)

@app.route("/fetch/<api_name>")
def fetch(api_name):
    r = requests.get(
        api_store[api_name],
        headers={"Authorization": f"Bearer {session['token']}"},
        verify=False
    )
    return jsonify(r.json())

@app.route("/logout")
def logout():
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
