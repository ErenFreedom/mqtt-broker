import requests
import urllib3
import time
import json
import paho.mqtt.client as mqtt

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================= CONFIG =================
TOKEN_API = "https://dell/WSI/api/token"
USERNAME = "defaultadmin"
PASSWORD = "desigo"

TEM_API = "https://dell:443/WSI/api/Values/System1.ApplicationView:ApplicationView.Logics.VirtualObjects.VA;"
RH_API  = "https://dell:443/WSI/api/Values/System1.ApplicationView:ApplicationView.Logics.VirtualObjects.rh;"

# MQTT
MQTT_BROKER = "34.14.145.160"
MQTT_PORT = 1883
MQTT_USER = "mqttuser"
MQTT_PASS = "123456"
MQTT_TOPIC = "building/temrh"

# ================= TOKEN FUNCTION =================
def get_token():
    payload = f"grant_type=password&username={USERNAME}&password={PASSWORD}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    r = requests.post(TOKEN_API, data=payload, headers=headers, verify=False)

    if r.status_code != 200:
        print("❌ Token Failed:", r.text)
        return None

    data = r.json()
    return data.get("access_token") or data.get("token")

# ================= VALUE EXTRACTOR =================
def extract_value(response_json):
    try:
        return response_json[0]["Value"]["Value"]
    except:
        return None

# ================= MQTT SETUP =================
client = mqtt.Client()
client.username_pw_set(MQTT_USER, MQTT_PASS)

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("✅ MQTT Connected")
    else:
        print("❌ MQTT Connection Failed:", rc)

client.on_connect = on_connect
client.connect(MQTT_BROKER, MQTT_PORT, 60)
client.loop_start()

# ================= MAIN =================
token = get_token()

if not token:
    print("Stopping because token not received.")
    exit()

print("✅ Token Received")
headers = {"Authorization": f"Bearer {token}"}

# ================= LIVE LOOP =================
while True:
    try:
        tem_res = requests.get(TEM_API, headers=headers, verify=False)
        rh_res  = requests.get(RH_API, headers=headers, verify=False)

        tem_value = extract_value(tem_res.json())
        rh_value  = extract_value(rh_res.json())

        payload = {
            "temperature": tem_value,
            "humidity": rh_value,
            "timestamp": time.time()
        }

        client.publish(MQTT_TOPIC, json.dumps(payload))

        print("📡 Published to Broker:")
        print(payload)

    except Exception as e:
        print("Error:", e)

    time.sleep(5)
