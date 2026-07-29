"""
╔══════════════════════════════════════════════════════════════════════════════╗
║   CANTINA IoT — AUGMENTATION + M2M INTERACTION (Legge 2 Vezzani)           ║
║                                                                              ║
║   PATTERN M2M IMPLEMENTATI:                                                 ║
║                                                                              ║
║   [GEO] Stesso luogo geografico, produttori diversi                        ║
║         Ogni ciclo i twin della stessa zona si scambiano la temp_est.       ║
║         Se uno devia > 8°C dalla media degli altri → sensore sospetto.     ║
║         Topic output: cantine/zona/<luogo>/sensore_sospetto                 ║
║                                                                              ║
║   [PROD] Stesso produttore, sedi diverse                                   ║
║          Se una sede supera 1000 ppm CO₂, avvisa le altre sedi             ║
║          dello stesso produttore di abbassare la soglia a 800 ppm.         ║
║          Topic output: cantine/<produttore>/allerta_co2                     ║
║                                                                              ║
║   [PROD-INT] Stesso produttore, temperatura interna anomala                ║
║              Se la temp_int di una sede devia > 6°C dalla media            ║
║              delle altre sedi dello stesso produttore → anomalia interna.  ║
║              Topic output: cantine/<produttore>/anomalia_temp_int           ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import json
import random
import paho.mqtt.client as mqtt

# ── CONFIGURAZIONE ─────────────────────────────────────────────────────────────
MQTT_BROKER     = 'localhost'
MQTT_PORT       = 1883
MQTT_USER       = None
MQTT_PASSWORD   = None

PRODUTTORE_SEED = 'urbani'
LUOGO_SEED      = 'pievepelago'
TOPIC_SEED_IN   = f"cantine/{PRODUTTORE_SEED}/{LUOGO_SEED}/sensori"

ANOMALY_PROB              = 1 / 80   # probabilità anomalia per singola lettura
SOGLIA_DEVIAZIONE_EST     = 8.0      # °C — scarto temp_est per considerare sensore sospetto
SOGLIA_DEVIAZIONE_INT     = 6.0      # °C — scarto temp_int per anomalia interna produttore
SOGLIA_CO2_ALLERTA        = 1000     # ppm — soglia CO₂ che scatena il Pattern PROD

# ── MATRICI FISICHE ───────────────────────────────────────────────────────────
PRODUTTORI = {
    'urbani':  {'offset_int': 0.0,  'offset_umid_int':  0.0},
    'rossi':   {'offset_int': +2.0, 'offset_umid_int': -2.0},
    'bianchi': {'offset_int': -1.5, 'offset_umid_int': +3.0},
}

LUOGHI = {
    'pievepelago': {'offset_est': 0.0,  'offset_umid_est':   0.0},
    'vignola':     {'offset_est': +4.0, 'offset_umid_est':  -5.0},
    'carpi':       {'offset_est': +6.5, 'offset_umid_est': -10.0},
}

client_pub = mqtt.Client(client_id="cantina_augmentation_pub")


# ── HELPERS ────────────────────────────────────────────────────────────────────

def rumore_gaussiano(valore: float, std: float) -> float:
    return valore + random.gauss(0, std)


def inietta_anomalia(dati: dict, produttore: str, luogo: str) -> tuple[dict, str]:
    """Inietta un'anomalia casuale con probabilità ANOMALY_PROB."""
    if random.random() > ANOMALY_PROB:
        return dati, ''

    tipo = random.choice(['AC_GUASTO', 'CO2_SPIKE', 'FREDDO_IMPROVVISO', 'SENSORE_EST_GUASTO'])
    d = dict(dati)

    if tipo == 'AC_GUASTO':
        # Impianto di condizionamento guasto: temp interna sale molto
        d['temp_int'] += random.uniform(6.0, 12.0)
    elif tipo == 'CO2_SPIKE':
        # Picco CO2: fermentazione intensa o guasto areazione
        d['co2'] += random.uniform(800, 2500)
    elif tipo == 'FREDDO_IMPROVVISO':
        # Guasto impianto riscaldamento: temp interna scende
        d['temp_int'] -= random.uniform(4.0, 8.0)
    elif tipo == 'SENSORE_EST_GUASTO':
        # Sensore esterno legge molto meno del reale
        d['temp_est'] -= random.uniform(12.0, 20.0)

    print(f"   💥 Anomalia [{tipo}] su {produttore}/{luogo}")
    return d, tipo


# ── PATTERN GEO: consenso temperatura esterna ─────────────────────────────────

def verifica_consenso_geo(dati_per_luogo: dict):
    """
    [GEO — M2M]
    Per ogni zona geografica, confronta le temp_est di tutti i produttori.
    Se un twin devia > SOGLIA_DEVIAZIONE_EST dalla media → pubblica avviso.

    dati_per_luogo = {
        'carpi': [
            {'produttore': 'urbani', 'temp_est': 32.1},
            {'produttore': 'rossi',  'temp_est': 31.8},
            {'produttore': 'bianchi','temp_est': 14.2},  ← sospetto!
        ],
        ...
    }
    """
    for luogo, letture in dati_per_luogo.items():
        if len(letture) < 2:
            continue

        media = sum(l['temp_est'] for l in letture) / len(letture)

        for lettura in letture:
            deviazione = abs(lettura['temp_est'] - media)
            if deviazione > SOGLIA_DEVIAZIONE_EST:
                payload = {
                    "tipo":                "SENSORE_EST_SOSPETTO",
                    "zona":                luogo,
                    "mittente_sospetto":   lettura['produttore'],
                    "temp_est_segnalata":  lettura['temp_est'],
                    "media_zona":          round(media, 2),
                    "deviazione":          round(deviazione, 2),
                    "messaggio": (
                        f"Il twin {lettura['produttore']}/{luogo} segnala "
                        f"{lettura['temp_est']}°C vs media zona {round(media, 1)}°C "
                        f"(scarto {round(deviazione, 1)}°C > soglia {SOGLIA_DEVIAZIONE_EST}°C). "
                        f"Probabile guasto al sensore esterno."
                    )
                }
                topic = f"cantine/zona/{luogo}/sensore_sospetto"
                client_pub.publish(topic, json.dumps(payload), qos=1)
                print(f"   🌡️  [GEO M2M] Sensore sospetto: {lettura['produttore']}/{luogo} "
                      f"→ {lettura['temp_est']}°C vs media {round(media, 1)}°C "
                      f"(scarto {round(deviazione, 1)}°C)")


# ── PATTERN PROD: allerta CO₂ intra-produttore ────────────────────────────────

def verifica_allerta_co2(dati_per_produttore: dict):
    """
    [PROD — M2M]
    Per ogni produttore, se una sede supera SOGLIA_CO2_ALLERTA ppm
    pubblica un avviso a tutte le altre sedi dello stesso produttore.

    dati_per_produttore = {
        'rossi': [
            {'sede': 'pievepelago', 'co2': 650},
            {'sede': 'vignola',     'co2': 1450},  ← supera soglia!
            {'sede': 'carpi',       'co2': 720},
        ],
        ...
    }
    """
    for produttore, letture in dati_per_produttore.items():
        for lettura in letture:
            if lettura['co2'] > SOGLIA_CO2_ALLERTA:
                altre_sedi = [l['sede'] for l in letture if l['sede'] != lettura['sede']]
                payload = {
                    "tipo":         "ALLERTA_CO2_PRODUTTORE",
                    "produttore":   produttore,
                    "sede_origine": lettura['sede'],
                    "valore_co2":   round(lettura['co2'], 1),
                    "istruzione":   "ABBASSA_SOGLIA_800",
                    "messaggio": (
                        f"La sede {lettura['sede']} di {produttore} ha rilevato "
                        f"CO₂ a {round(lettura['co2'], 0):.0f} ppm (soglia {SOGLIA_CO2_ALLERTA}). "
                        f"Sedi {', '.join(altre_sedi)}: abbassate la soglia a 800 ppm "
                        f"ed eseguite un doppio controllo immediato."
                    )
                }
                topic = f"cantine/{produttore}/allerta_co2"
                client_pub.publish(topic, json.dumps(payload), qos=1)
                print(f"   🏭 [PROD M2M] CO₂ {round(lettura['co2'],0):.0f} ppm "
                      f"da {produttore}/{lettura['sede']} → avviso a: {altre_sedi}")


# ── PATTERN PROD-INT: anomalia temperatura interna intra-produttore ───────────

def verifica_anomalia_temp_int(dati_per_produttore: dict):
    """
    [PROD-INT — M2M]
    Per ogni produttore, confronta le temp_int tra le sue sedi.
    Se una sede devia > SOGLIA_DEVIAZIONE_INT dalla media delle altre
    → pubblica avviso: possibile guasto AC o riscaldamento.

    Motivazione: cantine dello stesso produttore usano gli stessi
    impianti e policy di climatizzazione, quindi le temp interne
    dovrebbero essere simili. Una deviazione forte indica un guasto.
    """
    for produttore, letture in dati_per_produttore.items():
        if len(letture) < 2:
            continue

        media = sum(l['temp_int'] for l in letture) / len(letture)

        for lettura in letture:
            deviazione = abs(lettura['temp_int'] - media)
            if deviazione > SOGLIA_DEVIAZIONE_INT:
                altre_sedi = [l['sede'] for l in letture if l['sede'] != lettura['sede']]
                tipo_guasto = "AC_GUASTO" if lettura['temp_int'] > media else "RISCALDAMENTO_GUASTO"
                payload = {
                    "tipo":              "ANOMALIA_TEMP_INT",
                    "produttore":        produttore,
                    "sede_anomala":      lettura['sede'],
                    "temp_int_anomala":  round(lettura['temp_int'], 2),
                    "media_produttore":  round(media, 2),
                    "deviazione":        round(deviazione, 2),
                    "tipo_guasto":       tipo_guasto,
                    "messaggio": (
                        f"La sede {lettura['sede']} di {produttore} segnala "
                        f"temp. interna {round(lettura['temp_int'], 1)}°C vs media "
                        f"produttore {round(media, 1)}°C "
                        f"(scarto {round(deviazione, 1)}°C > soglia {SOGLIA_DEVIAZIONE_INT}°C). "
                        f"Possibile {tipo_guasto.replace('_', ' ')}. "
                        f"Sedi {', '.join(altre_sedi)}: verificate i vostri impianti."
                    )
                }
                topic = f"cantine/{produttore}/anomalia_temp_int"
                client_pub.publish(topic, json.dumps(payload), qos=1)
                print(f"   🌡️  [PROD-INT M2M] Temp anomala: {produttore}/{lettura['sede']} "
                      f"→ {round(lettura['temp_int'], 1)}°C vs media {round(media, 1)}°C")


# ── MOTORE EVENT-DRIVEN ────────────────────────────────────────────────────────

def on_message_sub(client, userdata, msg):
    try:
        seed = json.loads(msg.payload.decode('utf-8'))
        required = {'temp_int', 'temp_est', 'umid_int', 'umid_est', 'co2'}
        if not required.issubset(seed.keys()):
            return

        print(f"\n📥 Seed da {LUOGO_SEED} → "
              f"T_int={seed['temp_int']}°C | T_est={seed['temp_est']}°C | CO₂={seed['co2']}ppm")

        # Buffer per raccogliere tutti i dati del ciclo prima di fare le analisi M2M
        dati_per_luogo:      dict[str, list] = {l: [] for l in LUOGHI}
        dati_per_produttore: dict[str, list] = {p: [] for p in PRODUTTORI}

        # Includi anche il seed reale nei buffer M2M
        dati_per_luogo[LUOGO_SEED].append({
            'produttore': PRODUTTORE_SEED,
            'temp_est':   seed['temp_est']
        })
        dati_per_produttore[PRODUTTORE_SEED].append({
            'sede':     LUOGO_SEED,
            'temp_int': seed['temp_int'],
            'co2':      seed['co2']
        })

        # ── Genera gli 8 twin virtuali ────────────────────────────────────────
        for prod, conf_prod in PRODUTTORI.items():
            for luogo, conf_luogo in LUOGHI.items():

                if prod == PRODUTTORE_SEED and luogo == LUOGO_SEED:
                    continue  # Il seed reale ha già pubblicato i propri dati

                # Applica offset fisici + rumore gaussiano
                temp_int = rumore_gaussiano(seed['temp_int'] + conf_prod['offset_int'],       0.2)
                umid_int = rumore_gaussiano(seed['umid_int'] + conf_prod['offset_umid_int'],  1.0)
                temp_est = rumore_gaussiano(seed['temp_est'] + conf_luogo['offset_est'],      0.4)
                umid_est = rumore_gaussiano(seed['umid_est'] + conf_luogo['offset_umid_est'], 2.0)
                co2      = rumore_gaussiano(seed['co2'], 8.0)

                dati = {
                    'temp_int': round(max(-5.0,  min(50.0,   temp_int)), 2),
                    'temp_est': round(max(-20.0, min(50.0,   temp_est)), 2),
                    'umid_int': round(max(0.0,   min(100.0,  umid_int)), 2),
                    'umid_est': round(max(0.0,   min(100.0,  umid_est)), 2),
                    'co2':      round(max(350.0, min(5000.0, co2)),      1),
                }

                # Inietta anomalia casuale
                dati, _ = inietta_anomalia(dati, prod, luogo)

                # Accumula nei buffer M2M
                dati_per_luogo[luogo].append({
                    'produttore': prod,
                    'temp_est':   dati['temp_est']
                })
                dati_per_produttore[prod].append({
                    'sede':     luogo,
                    'temp_int': dati['temp_int'],
                    'co2':      dati['co2']
                })

                # Pubblica i dati del twin sul suo topic specifico
                topic_out = f"cantine/{prod}/{luogo}/sensori"
                client_pub.publish(topic_out, json.dumps(dati), qos=0)
                print(f"   ✅ {prod}/{luogo} → "
                      f"T_int:{dati['temp_int']}°C "
                      f"T_est:{dati['temp_est']}°C "
                      f"CO₂:{dati['co2']}ppm")

        # ── Analisi M2M dopo aver generato tutti i twin del ciclo ─────────────
        print(f"\n   🔍 Avvio analisi M2M...")

        # [GEO] Controllo consenso temperatura esterna per zona
        verifica_consenso_geo(dati_per_luogo)

        # [PROD] Controllo CO₂ tra sedi dello stesso produttore
        verifica_allerta_co2(dati_per_produttore)

        # [PROD-INT] Controllo anomalia temperatura interna tra sedi dello stesso produttore
        verifica_anomalia_temp_int(dati_per_produttore)

    except Exception as e:
        print(f"❌ Errore durante l'augmentation: {e}")


# ── AVVIO ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("  CANTINA IoT — Augmentation + M2M (Legge 2 Vezzani)")
    print()
    print("  Pattern M2M attivi:")
    print("  [GEO]      cantine/zona/<luogo>/sensore_sospetto")
    print("             → temp_est anomala rispetto alla media di zona")
    print("  [PROD]     cantine/<produttore>/allerta_co2")
    print("             → CO₂ alta in una sede avvisa le altre")
    print("  [PROD-INT] cantine/<produttore>/anomalia_temp_int")
    print("             → temp_int anomala rispetto alle altre sedi")
    print(f"\n  Seed atteso su: {TOPIC_SEED_IN}")
    print("=" * 65)

    client_sub = mqtt.Client(client_id="cantina_seed_listener")

    if MQTT_USER:
        client_sub.username_pw_set(MQTT_USER, MQTT_PASSWORD)
        client_pub.username_pw_set(MQTT_USER, MQTT_PASSWORD)

    def on_connect_sub(client, userdata, flags, rc):
        if rc == 0:
            client.subscribe(TOPIC_SEED_IN, qos=0)
            print("✅ Connesso. In ascolto sul seed...\n")
        else:
            print(f"❌ Connessione fallita (rc={rc})")

    client_sub.on_connect = on_connect_sub
    client_sub.on_message = on_message_sub

    try:
        client_sub.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        client_pub.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    except ConnectionRefusedError:
        print("❌ Impossibile connettersi a Mosquitto. Avvialo prima!")
        return

    try:
        client_sub.loop_forever()
    except KeyboardInterrupt:
        print("\n🛑 Simulatore fermato.")
        client_sub.disconnect()
        client_pub.disconnect()


if __name__ == '__main__':
    main()