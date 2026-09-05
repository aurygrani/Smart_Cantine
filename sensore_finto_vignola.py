"""
╔══════════════════════════════════════════════════════════════════════════╗
║   SENSORE FINTO — urbani/vignola (SOLO PER LA DEMO D'ESAME)               ║
║                                                                            ║
║   In aula avremo solo pievepelago (hardware reale): i dati saranno quasi  ║
║   sempre gli stessi, quindi mostrano poco delle funzionalità implementate.║
║   Questo script pubblica su cantine/urbani/vignola/sensori valori         ║
║   volutamente estremi e ballerini — cambia scenario ("caldo estremo",     ║
║   "freddo estremo", "CO2 alta", "umidità alta", "tutto normale") a ogni   ║
║   ciclo — per far scattare davanti al prof tutti i LED, gli allarmi e i  ║
║   timer ML che con l'aula normale non si vedrebbero mai.                  ║
║                                                                            ║
║   NB: urbani/vignola è ESCLUSA dal ciclo di generazione automatica in    ║
║   simulatore_cantine.py (vedi commento lì) proprio per lasciare questo   ║
║   topic libero per questo script — se li fai girare insieme non c'è      ║
║   competizione. Tutte le altre sedi/produttori restano generati da       ║
║   simulatore_cantine.py come prima.                                       ║
║                                                                            ║
║   Manda un payload COMPLETO in un solo messaggio (temp_int, umid_int,     ║
║   temp_est, umid_est, co2 tutti insieme): server.py tratta urbani/vignola ║
║   come sede simulata "a messaggio singolo", esattamente come rossi/       ║
║   bianchi — nessuna fusione a due processori necessaria, perché non c'è  ║
║   nessun hardware reale dietro.                                           ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import json
import random
import time
import paho.mqtt.client as mqtt

# ── CONFIGURAZIONE ──────────────────────────────────────────────────────────
MQTT_BROKER   = 'localhost'
MQTT_PORT     = 1883
MQTT_USER     = None
MQTT_PASSWORD = None

PRODUTTORE      = 'urbani'
SEDE            = 'vignola'
TOPIC_OUT       = f"cantine/{PRODUTTORE}/{SEDE}/sensori"

INTERVALLO_SECONDI = 8  # ogni quanto pubblica un nuovo ciclo di dati

# ── SCENARI ESTREMI ──────────────────────────────────────────────────────────
# Ogni scenario è deliberatamente poco realistico (salti bruschi, non una
# curva graduale) proprio per mostrare in pochi minuti tutto quello che con
# l'aula reale (temperatura pressoché costante) non si vedrebbe mai:
# LED temperatura/umidità/CO2, buzzer, timer AC/riscaldamento/umidificatore
# consigliati dai modelli ML, trend "minuti alla soglia" sul vino.
SCENARI = {
    'FREDDO_ESTREMO': {
        'temp_int': (2.0, 8.0),      # sotto SOGLIA_TEMP_BASSA (10°C) → LED temp + riscaldamento ML
        'umid_int': (45.0, 65.0),
        'temp_est': (-5.0, 4.0),
        'umid_est': (55.0, 85.0),
        'co2':      (400.0, 650.0),
    },
    'CALDO_ESTREMO': {
        'temp_int': (30.0, 38.0),    # sopra SOGLIA_TEMP_ALTA (26°C) e sopra 30°C → LED temp + buzzer
        'umid_int': (25.0, 50.0),
        'temp_est': (28.0, 40.0),
        'umid_est': (15.0, 40.0),
        'co2':      (500.0, 900.0),
    },
    'CO2_ALTA': {
        'temp_int': (16.0, 20.0),    # temperatura nella norma, solo la CO2 va fuori scala
        'umid_int': (55.0, 68.0),
        'temp_est': (12.0, 22.0),
        'umid_est': (45.0, 70.0),
        'co2':      (1200.0, 3000.0),  # sopra SOGLIA_CO2_ALTA (1000) → LED CO2 + buzzer
    },
    'UMIDITA_ALTA': {
        'temp_int': (14.0, 18.0),
        'umid_int': (85.0, 98.0),    # sopra SOGLIA_UMID_ALTA (80%) → LED umidità
        'temp_est': (10.0, 20.0),
        'umid_est': (70.0, 95.0),
        'co2':      (450.0, 700.0),
    },
    'TUTTO_NORMALE': {
        'temp_int': (13.0, 15.0),    # vicino al target di 'urbani' (14°C, vedi produttori.py)
        'umid_int': (58.0, 66.0),    # vicino al target (62%)
        'temp_est': (8.0, 18.0),
        'umid_est': (50.0, 75.0),
        'co2':      (400.0, 550.0),
    },
}


def genera_dati_scenario(nome_scenario: str) -> dict:
    range_scenario = SCENARI[nome_scenario]
    return {
        'temp_int': round(random.uniform(*range_scenario['temp_int']), 2),
        'umid_int': round(random.uniform(*range_scenario['umid_int']), 2),
        'temp_est': round(random.uniform(*range_scenario['temp_est']), 2),
        'umid_est': round(random.uniform(*range_scenario['umid_est']), 2),
        'co2':      round(random.uniform(*range_scenario['co2']), 1),
    }


def main():
    print("=" * 65)
    print("  SENSORE FINTO — urbani/vignola (demo d'esame)")
    print(f"  Pubblica su: {TOPIC_OUT}")
    print(f"  Intervallo:  ogni {INTERVALLO_SECONDI}s")
    print("  Scenari: " + ", ".join(SCENARI.keys()))
    print("=" * 65)

    client = mqtt.Client(client_id="sensore_finto_vignola",
                          callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

    try:
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    except ConnectionRefusedError:
        print("❌ Impossibile connettersi a Mosquitto. Avvialo prima!")
        return

    client.loop_start()

    try:
        while True:
            nome_scenario = random.choice(list(SCENARI.keys()))
            dati = genera_dati_scenario(nome_scenario)

            client.publish(TOPIC_OUT, json.dumps(dati), qos=0)
            print(f"\n🎭 Scenario: {nome_scenario}")
            print(f"   ✅ {PRODUTTORE}/{SEDE} → "
                  f"T_int:{dati['temp_int']}°C  T_est:{dati['temp_est']}°C  "
                  f"U_int:{dati['umid_int']}%  U_est:{dati['umid_est']}%  "
                  f"CO₂:{dati['co2']}ppm")

            time.sleep(INTERVALLO_SECONDI)
    except KeyboardInterrupt:
        print("\n🛑 Sensore finto fermato.")
        client.loop_stop()
        client.disconnect()


if __name__ == '__main__':
    main()