"""
╔══════════════════════════════════════════════════════════════════╗
║         CANTINA REALE IoT — DATA AUGMENTATION (EVENT-DRIVEN)    ║
║  Si attiva SOLO quando arriva il "seed" reale da Pievepelago.   ║
║  Calcola e pubblica i dati per le altre 8 combinazioni (3x3).   ║
╚══════════════════════════════════════════════════════════════════╝
"""

import json
import random
from datetime import datetime
import paho.mqtt.client as mqtt

# ──────────────────────────────────────────────
#  CONFIGURAZIONE
MQTT_BROKER        = 'localhost'
MQTT_PORT          = 1883
MQTT_USER          = None
MQTT_PASSWORD      = None

PRODUTTORE_SEED    = 'urbani'
LUOGO_SEED         = 'pievepelago'
TOPIC_SEED_IN      = f"cantine/{PRODUTTORE_SEED}/{LUOGO_SEED}/sensori"

ANOMALY_PROB       = 1/80   # probabilità anomalia per singola lettura

# ──────────────────────────────────────────────
#  MATRICE DELLE REGOLE FISICHE
# ──────────────────────────────────────────────
# Offset interni determinati dalle policy del PRODUTTORE
PRODUTTORI = {
    'urbani':  {'offset_int': 0.0,  'offset_umid_int': 0.0},   # Seme base
    'rossi':   {'offset_int': +2.0, 'offset_umid_int': -2.0},  # Vini Rossi (più caldo)
    'bianchi': {'offset_int': -1.5, 'offset_umid_int': +3.0}   # Vini Bianchi (più freddo, più umido)
}

# Offset esterni determinati dalla geografia del LUOGO
LUOGHI = {
    'pievepelago': {'offset_est': 0.0,  'offset_umid_est': 0.0},   # Seme base (Montagna)
    'vignola':     {'offset_est': +4.0, 'offset_umid_est': -5.0},  # Collina
    'carpi':       {'offset_est': +6.5, 'offset_umid_est': -10.0}  # Pianura (molto più caldo e secco d'estate)
}

client_pub  = mqtt.Client(client_id="cantina_augmentation_pub")

# ──────────────────────────────────────────────
#  FUNZIONI DI SUPPORTO
# ──────────────────────────────────────────────
def rumore_gaussiano(valore: float, std: float) -> float:
    return valore + random.gauss(0, std)

def inietta_anomalia(dati: dict, produttore: str, luogo: str) -> tuple[dict, str]:
    if random.random() > ANOMALY_PROB:
        return dati, ''

    tipo = random.choice(['AC_GUASTO', 'CO2_SPIKE', 'FREDDO_IMPROVVISO'])
    d = dict(dati)

    if tipo == 'AC_GUASTO':
        d['temp_int'] += random.uniform(5.0, 10.0)
    elif tipo == 'CO2_SPIKE':
        d['co2'] += random.uniform(800, 2500)
    elif tipo == 'FREDDO_IMPROVVISO':
        d['temp_int'] -= random.uniform(4.0, 8.0)

    print(f"   💥 Anomalia iniettata per [{produttore} - {luogo}]: {tipo}")
    return d, tipo

# ──────────────────────────────────────────────
#  IL MOTORE EVENT-DRIVEN (Si attiva al volo!)
# ──────────────────────────────────────────────
def on_message_sub(client, userdata, msg):
    try:
        seed = json.loads(msg.payload.decode('utf-8'))

        # Validazione base
        required = {'temp_int', 'temp_est', 'umid_int', 'umid_est', 'co2'}
        if not required.issubset(seed.keys()):
            return

        print(f"\n📥 RICEVUTO DATO REALE DA PIEVEPELAGO! Avvio generazione 8 sedi virtuali...")
        print(f"   Seed: T_int={seed['temp_int']}°C, T_est={seed['temp_est']}°C")

        # Iteriamo su tutte le 9 combinazioni possibili
        for prod, conf_prod in PRODUTTORI.items():
            for luogo, conf_luogo in LUOGHI.items():

                # Saltiamo la combinazione reale che ha già trasmesso i dati!
                if prod == PRODUTTORE_SEED and luogo == LUOGO_SEED:
                    continue

                # 1. Applicazione Regole Fisiche + Rumore Naturale
                # Temp Interna (dipende dal Produttore)
                temp_int = rumore_gaussiano(seed['temp_int'] + conf_prod['offset_int'], 0.2)
                umid_int = rumore_gaussiano(seed['umid_int'] + conf_prod['offset_umid_int'], 1.0)

                # Temp Esterna (dipende dal Luogo geografico)
                temp_est = rumore_gaussiano(seed['temp_est'] + conf_luogo['offset_est'], 0.4)
                umid_est = rumore_gaussiano(seed['umid_est'] + conf_luogo['offset_umid_est'], 2.0)

                co2 = rumore_gaussiano(seed['co2'], 8.0)

                dati_virtuali = {
                    'temp_int' : round(max(-5.0, min(50.0, temp_int)), 2),
                    'temp_est' : round(max(-20.0, min(50.0, temp_est)), 2),
                    'umid_int' : round(max(0.0, min(100.0, umid_int)), 2),
                    'umid_est' : round(max(0.0, min(100.0, umid_est)), 2),
                    'co2'      : round(max(350.0, min(5000.0, co2)), 1),
                }

                # 2. Iniettiamo eventuali anomalie
                dati_virtuali, _ = inietta_anomalia(dati_virtuali, prod, luogo)

                # 3. Pubblicazione Immediata sul canale specifico
                topic_out = f"cantine/{prod}/{luogo}/sensori"
                client_pub.publish(topic_out, json.dumps(dati_virtuali), qos=0)

                print(f"   ✅ Generato e Inviato -> {prod} a {luogo} | "
                      f"T_int: {dati_virtuali['temp_int']}°C | "
                      f"T_est: {dati_virtuali['temp_est']}°C")

    except Exception as e:
        print(f"❌ Errore durante l'augmentation: {e}")

# ──────────────────────────────────────────────
#  AVVIO E CONNESSIONI
# ──────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  CANTINA REALE — Data Augmentation (Event-Driven)")
    print(f"  In attesa di dati su: {TOPIC_SEED_IN}")
    print("=" * 60)

    client_sub = mqtt.Client(client_id="cantina_seed_listener")

    if MQTT_USER:
        client_sub.username_pw_set(MQTT_USER, MQTT_PASSWORD)
        client_pub.username_pw_set(MQTT_USER, MQTT_PASSWORD)

    def on_connect_sub(client, userdata, flags, rc):
        if rc == 0:
            client.subscribe(TOPIC_SEED_IN, qos=0)
            print("✅ Connesso! In ascolto silenzioso...\n")
        else:
            print(f"❌ Connessione fallita (rc={rc})")

    client_sub.on_connect = on_connect_sub
    client_sub.on_message = on_message_sub

    try:
        client_sub.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        client_pub.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    except ConnectionRefusedError:
        print("❌ Impossibile connettersi a Mosquitto. Assicurati che sia acceso!")
        return

    # Manteniamo vivo il listener
    try:
        client_sub.loop_forever()
    except KeyboardInterrupt:
        print("\n🛑 Augmentation fermato.")
        client_sub.disconnect()
        client_pub.disconnect()

if __name__ == '__main__':
    main()