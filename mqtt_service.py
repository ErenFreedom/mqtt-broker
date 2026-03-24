import json
import time
import paho.mqtt.client as mqtt

MQTT_BROKER = "34.14.145.160"
MQTT_PORT = 1883
MQTT_USER = "mqttuser"
MQTT_PASS = "123456"

CLIENT_INFO = {"client_id": None, "site_id": None}

mqtt_client = mqtt.Client()
mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("[MQTT] Connected to broker successfully")
    else:
        print(f"[MQTT] Connection failed, rc={rc}")

def on_disconnect(client, userdata, rc):
    print(f"[MQTT] Disconnected rc={rc}, reconnecting...")
    while True:
        try:
            client.reconnect()
            print("[MQTT] Reconnected!")
            break
        except Exception as e:
            print(f"[MQTT] Reconnect failed: {e}")
            time.sleep(5)

mqtt_client.on_connect = on_connect
mqtt_client.on_disconnect = on_disconnect

try:
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
    mqtt_client.loop_start()
except Exception as e:
    print(f"[MQTT] Initial connection failed: {e}")

# ---------------- PUBLISH ----------------
def publish_sensor_data(sensor_id, sensor_name, location, value, quality, quality_good, timestamp):
    """
    Sensor data ko MQTT broker pe publish karta hai.
    Topic  : client_id/site_id/sensor_id/sensor_name
    Payload: JSON format
    """
    client_id = CLIENT_INFO.get("client_id")
    site_id   = CLIENT_INFO.get("site_id")

    if not client_id or not site_id:
        print("[MQTT] client_id ya site_id set nahi hai, publish skip")
        return

    topic = f"{client_id}/{site_id}/{sensor_id}/{sensor_name}"

    payload = {
        "client_id":    client_id,
        "site_id":      site_id,
        "sensor_id":    sensor_id,
        "sensor_name":  sensor_name,
        "location":     location,
        "value":        value,
        "quality":      quality,
        "quality_good": quality_good,
        "timestamp":    timestamp
    }

    try:
        result = mqtt_client.publish(topic, json.dumps(payload), qos=1)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            print(f"[MQTT] Published → {topic} | value={value} | quality_good={quality_good} | timestamp={timestamp}")
        else:
            print(f"[MQTT] Publish failed rc={result.rc}")
    except Exception as e:
        print(f"[MQTT] Publish error: {e}")
