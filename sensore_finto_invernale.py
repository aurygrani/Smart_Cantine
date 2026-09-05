"""
Simula l'ESP32 (interno) e l'ESP8266 (esterno) di Pievepelago.
Pubblica ogni 30 secondi sul topic cantine/urbani/pievepelago/sensori.
"""

import json
import time
import random
import paho.mqtt.client as mqtt

BROKER = 'localhost'
PORT   = 1883
TOPIC  = 'cantine/urbani/pievepelago/sensori'

client = mqtt.Client(client_id="sensore_finto-invernale")
client.connect(BROKER, PORT, 60)
client.loop_start()

# Valori base realistici per una cantina invernale
temp_int = 16.0
temp_est = 10.0
umid_int = 55.0
umid_est = 45.0
co2      = 450.0

print(f"🟢 Sensore finto avviato — pubblicando su {TOPIC} ogni 10 secondi")
print("   Premi CTRL+C per fermare\n")

while True:
    # Varia leggermente i valori ad ogni ciclo (simula sensori reali)
    temp_int += random.uniform(-0.3, 0.3)
    temp_est += random.uniform(-0.2, 0.2)
    umid_int += random.uniform(-0.5, 0.5)
    umid_est += random.uniform(-0.5, 0.5)
    co2      += random.uniform(-15,  15)

    # Mantieni valori in range realistico
    temp_int = max(10.0, min(35.0, temp_int))
    temp_est = max(5.0,  min(40.0, temp_est))
    umid_int = max(40.0, min(90.0, umid_int))
    umid_est = max(30.0, min(85.0, umid_est))
    co2      = max(400.0, min(2000.0, co2))

    # Pubblica tutto in un unico messaggio (come farebbe la fusione ESP32+ESP8266)
    payload = {
        "temp_int": round(temp_int, 1),
        "temp_est": round(temp_est, 1),
        "umid_int": round(umid_int, 1),
        "umid_est": round(umid_est, 1),
        "co2":      round(co2, 0),
    }

    client.publish(TOPIC, json.dumps(payload), qos=0)
    print(f"📤 Inviato: {payload}")

    time.sleep(20)