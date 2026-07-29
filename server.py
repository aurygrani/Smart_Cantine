from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from sqlalchemy import func
from collections import deque
import os, json, math
import paho.mqtt.client as mqtt

# ── ML: importa i modelli quando disponibili ──────────────────────────────────
# La collega depositerà i file .pkl nella cartella ml_models/.
# Ogni import è protetto da try/except: se il file non c'è ancora il server
# funziona comunque, le funzioni ML restituiscono None e vengono saltate.
ML_DISPONIBILE = {}

try:
    import pickle, numpy as np
    ML_DISPONIBILE['numpy'] = True
except ImportError:
    ML_DISPONIBILE['numpy'] = False

try:
    import joblib
    ML_DISPONIBILE['joblib'] = True
except ImportError:
    ML_DISPONIBILE['joblib'] = False

# Percorso cartella modelli
ML_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'ml_models')
os.makedirs(ML_DIR, exist_ok=True)

def carica_modello(nome_file):
    """Carica un modello .pkl dalla cartella ml_models/. Ritorna None se non esiste."""
    path = os.path.join(ML_DIR, nome_file)
    if not os.path.exists(path):
        return None
    try:
        import joblib
        return joblib.load(path)
    except Exception as e:
        print(f"⚠️  Impossibile caricare {nome_file}: {e}")
        return None

# Carica i modelli all'avvio (None se non ancora disponibili)
_modello_efficienza   = carica_modello('modello_efficienza.pkl')
_modello_timer_ac     = carica_modello('modello_timer_multioutput.pkl')
_modello_anomalia_int = carica_modello('modello_anomalia_interna.pkl')

app = Flask(__name__)
app.config['SECRET_KEY'] = 'chiave-segreta-urbani'
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'database_cantine.db')

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Buffer allarmi recenti (in memoria) — max 50, usato per il polling sonoro del frontend
allarmi_recenti = deque(maxlen=50)

# ── BUFFER DI FUSIONE ESP32 + ESP8266 ────────────────────────────────────────
# L'ESP32 interno pubblica: temp_int, umid_int, co2
# L'ESP8266 esterno pubblica: temp_est, umid_est
# Il server aspetta di avere ENTRAMBI prima di salvare nel DB
# e di inviare i dati completi al simulatore (che li usa come seed).
buffer_fusione = {
    "temp_int": None,
    "umid_int": None,
    "co2":      None,
    "temp_est": None,
    "umid_est": None,
}

# Tiene traccia dell ultimo payload salvato per urbani/pievepelago.
# Evita righe duplicate quando il simulatore ripubblica sullo stesso
# topic /sensori per il consenso geografico GEO.
_ultimo_payload_salvato: dict = {}

# Soglie per i comandi agli attuatori ESP32
SOGLIA_TEMP_ALTA = 26.0  # °C → accende LED_TEMPERATURA (raffrescamento)
SOGLIA_TEMP_BASSA = 10.0  # °C → accende LED riscaldamento (puoi usare LED_UMIDITA come secondo led)
SOGLIA_UMID_ALTA = 80.0  # %  → accende LED_UMIDITA
SOGLIA_CO2_ALTA = 1000  # ppm → accende LED_CO2 + BUZZER


def calcola_e_invia_comandi(mqtt_client, temp_int, umid_int, co2):
    """
    Calcola lo stato degli attuatori in base alle soglie e
    invia il comando all'ESP32 nel formato che si aspetta:
    TEMP=1;UMID=0;CO2=1;BUZZER=0

    TEMP=1  → LED giallo acceso  = impianto raffrescamento attivo
    TEMP=0  → LED giallo spento  = temperatura ok
    UMID=1  → LED blu acceso     = umidità alta (o riscaldamento se temp bassa)
    CO2=1   → LED rosso acceso   = CO2 elevata
    BUZZER=1→ buzzer attivo      = allarme critico
    """
    led_temp = 1 if (temp_int is not None and temp_int > SOGLIA_TEMP_ALTA) else 0
    led_umid = 1 if (umid_int is not None and umid_int > SOGLIA_UMID_ALTA) else 0
    led_co2 = 1 if (co2 is not None and co2 > SOGLIA_CO2_ALTA) else 0
    buzzer = 1 if (co2 is not None and co2 > SOGLIA_CO2_ALTA) else 0

    # Buzzer anche per temperatura critica (>30°C)
    if temp_int is not None and temp_int > 30.0:
        buzzer = 1

    comando = f"TEMP={led_temp};UMID={led_umid};CO2={led_co2};BUZZER={buzzer}"
    mqtt_client.publish("cantine/urbani/pievepelago/comandi", comando, qos=1)
    print(f"   📡 Comando → ESP32: {comando}")


# ── MODELLI ───────────────────────────────────────────────────────────────────

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True)
    password_hash = db.Column(db.String(200))
    ruolo = db.Column(db.String(50), default='urbani')  # admin|urbani|rossi|bianchi


class DatoSensore(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    produttore = db.Column(db.String(50))
    sede = db.Column(db.String(50))
    temp_int = db.Column(db.Float)
    temp_est = db.Column(db.Float)
    umid_int = db.Column(db.Float)
    umid_est = db.Column(db.Float)
    co2 = db.Column(db.Float)
    allarme_co2 = db.Column(db.Boolean, default=False)
    temp_vino_proiettata = db.Column(db.Float)

    # ── Campi aggiunti per i modelli ML ──────────────────────────────────────
    # Modellazione fisica vino (inerzia termica — formula smorzamento)
    temp_vino_smorzata   = db.Column(db.Float)   # temperatura vino con smorzamento fisico

    # Regressore multi-output: timer impianti
    timer_ac_minuti      = db.Column(db.Float)   # minuti consigliati di attivazione AC
    timer_umid_minuti    = db.Column(db.Float)   # minuti consigliati di umidificatore

    # Trend temperatura vino (regressione lineare sullo storico)
    minuti_alla_soglia   = db.Column(db.Float)   # minuti stimati prima di superare soglia critica
    trend_vino_pendenza  = db.Column(db.Float)   # °C/min — pendenza della regressione lineare


class RisultatoML(db.Model):
    """
    Salva i risultati dei modelli ML ciclo per ciclo.
    Usato per: verifica ambiente, conformità sedi, anomalie sensori.
    """
    id          = db.Column(db.Integer, primary_key=True)
    timestamp   = db.Column(db.DateTime, default=datetime.utcnow)
    produttore  = db.Column(db.String(50))
    sede        = db.Column(db.String(50))
    modello     = db.Column(db.String(50))   # 'verifica_ambiente' | 'conformita_sede' | 'anomalia_sensore'
    esito       = db.Column(db.String(20))   # 'ok' | 'warn' | 'danger'
    valore      = db.Column(db.Float)        # valore numerico rilevante
    dettaglio   = db.Column(db.String(300))  # messaggio leggibile


class FasciaEfficienza(db.Model):
    """
    Output del modello pre-trainato (Alessia) per la fascia energetica della cantina.
    Fascia A = perfetto, B = nella media, C = inefficiente.
    """
    id              = db.Column(db.Integer, primary_key=True)
    timestamp       = db.Column(db.DateTime, default=datetime.utcnow)
    produttore      = db.Column(db.String(50))
    sede            = db.Column(db.String(50))
    fascia          = db.Column(db.String(5))    # 'A' | 'B' | 'C'
    colore          = db.Column(db.String(10))   # 'verde' | 'giallo' | 'rosso'
    score           = db.Column(db.Float)        # score numerico del modello (0-1)
    # Input usati per la predizione
    volume_cantina  = db.Column(db.Float)        # m³ (inserito dall'operatore)
    valore_isolamento = db.Column(db.Float)      # W/m²K (inserito dall'operatore)
    temp_int_media  = db.Column(db.Float)
    temp_est_media  = db.Column(db.Float)
    delta_temp      = db.Column(db.Float)        # differenza int-est


class EventoM2M(db.Model):
    """
    Registra tutti gli eventi di comunicazione tra digital twin (Legge 2 Vezzani).

    pattern='GEO'  → consenso temperatura esterna tra twin della stessa zona geografica.
                     Un twin segnala agli altri che un sensore esterno è probabilmente guasto
                     perché la sua lettura devia troppo dalla media di zona.

    pattern='PROD' → allerta CO₂ intra-produttore: una sede avvisa le altre sedi
                     dello stesso produttore di abbassare la soglia di allarme
                     ed eseguire un doppio controllo.
    """
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    pattern = db.Column(db.String(10))  # 'GEO' | 'PROD'
    tipo = db.Column(db.String(60))
    mittente = db.Column(db.String(100))
    destinatari = db.Column(db.String(200))
    valore = db.Column(db.Float)
    messaggio = db.Column(db.String(400))


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


with app.app_context():
    db.create_all()
    # Migrazione colonne esistenti
    migrazioni = [
        "ALTER TABLE user ADD COLUMN ruolo VARCHAR(50) DEFAULT 'urbani'",
        # Nuove colonne ML su dato_sensore
        "ALTER TABLE dato_sensore ADD COLUMN temp_vino_smorzata FLOAT",
        "ALTER TABLE dato_sensore ADD COLUMN timer_ac_minuti FLOAT",
        "ALTER TABLE dato_sensore ADD COLUMN timer_umid_minuti FLOAT",
        "ALTER TABLE dato_sensore ADD COLUMN minuti_alla_soglia FLOAT",
        "ALTER TABLE dato_sensore ADD COLUMN trend_vino_pendenza FLOAT",
    ]
    for sql in migrazioni:
        try:
            db.session.execute(db.text(sql))
            db.session.commit()
        except Exception:
            pass  # Colonna già presente


# ── HELPERS ───────────────────────────────────────────────────────────────────

def produttori_autorizzati():
    if current_user.ruolo == 'admin':
        return ['urbani', 'rossi', 'bianchi']
    return [current_user.ruolo]


# ── MQTT ──────────────────────────────────────────────────────────────────────

# ── FUNZIONI ML (stub — la collega sostituirà il corpo con i modelli reali) ────
#
# Ogni funzione segue lo stesso contratto:
#   - Riceve i dati del sensore come dizionario
#   - Ritorna un dizionario con i risultati, oppure None se il modello non è disponibile
#   - Il chiamante (on_message) salva il risultato nel DB e lo usa per i comandi
#
# Per integrare un modello reale: sostituire il corpo della funzione
# mantenendo la stessa firma e lo stesso formato del dizionario di ritorno.

def ml_verifica_ambiente(temp_int, umid_int, co2,
                          soglia_temp_alta=26.0, soglia_temp_bassa=10.0,
                          soglia_umid_alta=80.0, soglia_umid_bassa=30.0,
                          soglia_co2=1000):
    """
    [STUB] Verifica ambiente — controlla parametri vs target e calcola comandi attuatori.
    In produzione: sostituire con il modello ML di Alessia.
    Attualmente usa soglie fisse (identico alla logica calcola_e_invia_comandi).
    """
    esito, dettaglio = 'ok', 'Tutti i parametri nella norma'

    if temp_int is not None and temp_int > soglia_temp_alta:
        esito, dettaglio = 'danger', f'Temperatura alta: {temp_int:.1f}°C (soglia {soglia_temp_alta}°C)'
    elif temp_int is not None and temp_int < soglia_temp_bassa:
        esito, dettaglio = 'warn', f'Temperatura bassa: {temp_int:.1f}°C (soglia {soglia_temp_bassa}°C)'
    elif umid_int is not None and umid_int > soglia_umid_alta:
        esito, dettaglio = 'warn', f'Umidità alta: {umid_int:.0f}% (soglia {soglia_umid_alta}%)'
    elif co2 is not None and co2 > soglia_co2:
        esito, dettaglio = 'danger', f'CO₂ elevata: {co2:.0f} ppm (soglia {soglia_co2})'

    return {'esito': esito, 'dettaglio': dettaglio, 'modello': 'verifica_ambiente'}


def ml_temp_vino_smorzata(temp_aria, temp_vino_precedente=None,
                           costante_smorzamento=0.05):
    """
    [IMPLEMENTATO] Modellazione fisica vino — formula di smorzamento termico.
    Simula l'inerzia termica del liquido: il vino non segue istantaneamente
    la temperatura dell'aria ma si avvicina gradualmente con una costante τ.

    Formula: T_vino(t) = T_aria + (T_vino(t-1) - T_aria) * e^(-k)
    dove k = costante_smorzamento (default 0.05 → smorzamento del 5% per ciclo)

    In produzione: la collega può affinare k con dati reali di una botte.
    """
    if temp_aria is None:
        return None
    if temp_vino_precedente is None:
        # Prima lettura: stima iniziale con offset fisso (come nel codice originale)
        return round(temp_aria * 0.95, 4)
    # Formula smorzamento
    smorzamento = math.exp(-costante_smorzamento)
    t_vino = temp_aria + (temp_vino_precedente - temp_aria) * smorzamento
    return round(t_vino, 4)


def ml_trend_vino(storico_temp_vino: list[float], soglia_critica=24.0,
                  intervallo_secondi=10):
    """
    [STUB] Regressione lineare sullo storico temp vino → stima minuti alla soglia.
    Nessun pre-training: usa regressione lineare sui dati in arrivo (online learning).

    storico_temp_vino: lista degli ultimi N valori di temp_vino_smorzata
    Ritorna: {'pendenza': °C/min, 'minuti_alla_soglia': float | None}

    In produzione: la collega può sostituire con un modello ARIMA o Prophet.
    """
    if len(storico_temp_vino) < 3:
        return {'pendenza': None, 'minuti_alla_soglia': None}

    n = len(storico_temp_vino)
    # Regressione lineare semplice (minimi quadrati)
    # x = tempo in minuti, y = temperatura
    intervallo_min = intervallo_secondi / 60.0
    x = [i * intervallo_min for i in range(n)]
    x_mean = sum(x) / n
    y_mean = sum(storico_temp_vino) / n
    num = sum((x[i] - x_mean) * (storico_temp_vino[i] - y_mean) for i in range(n))
    den = sum((x[i] - x_mean) ** 2 for i in range(n))

    if den == 0:
        return {'pendenza': 0.0, 'minuti_alla_soglia': None}

    pendenza = num / den  # °C/min
    t_attuale = storico_temp_vino[-1]

    # Stima minuti alla soglia critica
    if pendenza <= 0 or t_attuale >= soglia_critica:
        minuti = None  # in raffreddamento o già oltre soglia
    else:
        minuti = (soglia_critica - t_attuale) / pendenza

    return {'pendenza': round(pendenza, 4), 'minuti_alla_soglia': round(minuti, 1) if minuti else None}


def ml_timer_impianti(temp_int, umid_int, temp_est, temp_target=18.0, umid_target=65.0):
    """
    [STUB] Regressore multi-output: calcola timer AC e umidificatore.
    In produzione: sostituire con il modello .pkl di Alessia.
    Attualmente usa una formula proporzionale semplice.

    Ritorna: {'timer_ac_minuti': float, 'timer_umid_minuti': float}
    """
    if _modello_timer_ac is not None and ML_DISPONIBILE.get('numpy'):
        # ── Usa il modello reale quando disponibile ──────────────────────────
        import numpy as np
        X = np.array([[temp_int, umid_int, temp_est, temp_target, umid_target]])
        pred = _modello_timer_ac.predict(X)[0]
        return {'timer_ac_minuti': round(float(pred[0]), 1),
                'timer_umid_minuti': round(float(pred[1]), 1)}

    # ── Fallback proporzionale (stub) ────────────────────────────────────────
    delta_temp = (temp_int or 0) - temp_target
    delta_umid = (umid_int or 0) - umid_target

    timer_ac   = max(0.0, round(delta_temp * 3.0, 1))   # 3 min per ogni °C oltre target
    timer_umid = max(0.0, round(abs(delta_umid) * 1.5, 1)) if delta_umid > 5 else 0.0

    return {'timer_ac_minuti': timer_ac, 'timer_umid_minuti': timer_umid}


def ml_fascia_efficienza(temp_int_media, temp_est_media,
                          volume_cantina, valore_isolamento,
                          produttore, sede):
    """
    [STUB] Predice la fascia energetica della cantina.
    In produzione: sostituire con il modello pre-trainato di Alessia.

    Ritorna: {'fascia': 'A'|'B'|'C', 'colore': 'verde'|'giallo'|'rosso', 'score': float}
    """
    if _modello_efficienza is not None and ML_DISPONIBILE.get('numpy'):
        import numpy as np
        delta = abs(temp_int_media - temp_est_media)
        X = np.array([[temp_int_media, temp_est_media, delta,
                        volume_cantina, valore_isolamento]])
        pred  = _modello_efficienza.predict(X)[0]
        score = float(_modello_efficienza.predict_proba(X).max()) if hasattr(_modello_efficienza, 'predict_proba') else 0.5
        fascia = str(pred)
        colore = 'verde' if fascia == 'A' else ('giallo' if fascia == 'B' else 'rosso')
        return {'fascia': fascia, 'colore': colore, 'score': round(score, 3)}

    # ── Stub basato su delta termico e isolamento ─────────────────────────────
    if temp_int_media is None or temp_est_media is None:
        return None
    delta = abs(temp_int_media - temp_est_media)
    # Heuristica semplice: più alto è il delta con buon isolamento, più efficiente
    score = min(1.0, (valore_isolamento or 1.0) / (delta + 1))
    if score > 0.7:
        return {'fascia': 'A', 'colore': 'verde',  'score': round(score, 3)}
    elif score > 0.4:
        return {'fascia': 'B', 'colore': 'giallo', 'score': round(score, 3)}
    else:
        return {'fascia': 'C', 'colore': 'rosso',  'score': round(score, 3)}


# Buffer storico temperature vino per il trend (per sede)
# { 'urbani/pievepelago': [22.1, 22.3, 22.5, ...] }
_storico_vino: dict[str, list[float]] = {}
MAX_STORICO_TREND = 30  # ultimi 30 campionamenti (~5 minuti con ciclo 10s)


def on_connect(client, userdata, flags, rc):
    print("✅ Connesso al Broker MQTT!")
    client.subscribe("cantine/#")  # sensori + allerte zona


def on_message(client, userdata, msg):
    topic = msg.topic

    # Ignora i topic di comando: non sono JSON, li pubblichiamo noi stessi
    if topic.endswith('/comandi'):
        return

    try:
        payload = json.loads(msg.payload.decode('utf-8'))
        parti = topic.split('/')

        # ── 1. Dati sensore: cantine/<prod>/<sede>/sensori
        if len(parti) >= 4 and parti[3] == 'sensori':
            produttore = parti[1].lower()
            sede = parti[2].lower()

            # ── FUSIONE ESP32 (interno) + ESP8266 (esterno) ──────────────────
            # Solo urbani/pievepelago è reale — gli altri sono già completi dal simulatore
            if produttore == 'urbani' and sede == 'pievepelago':

                # Aggiorna il buffer con i campi presenti nel payload
                if 'temp_int' in payload: buffer_fusione['temp_int'] = payload['temp_int']
                if 'umid_int' in payload: buffer_fusione['umid_int'] = payload['umid_int']
                if 'co2' in payload: buffer_fusione['co2'] = payload['co2']
                if 'temp_est' in payload: buffer_fusione['temp_est'] = payload['temp_est']
                if 'umid_est' in payload: buffer_fusione['umid_est'] = payload['umid_est']

                # Calcola e invia comandi agli attuatori ESP32 ogni volta che arriva un dato interno
                if 'temp_int' in payload or 'co2' in payload:
                    calcola_e_invia_comandi(
                        client,
                        buffer_fusione['temp_int'],
                        buffer_fusione['umid_int'],
                        buffer_fusione['co2']
                    )

                # Se non abbiamo ancora entrambi i sensori, aspettiamo
                if None in buffer_fusione.values():
                    campi_mancanti = [k for k, v in buffer_fusione.items() if v is None]
                    print(f"⏳ Fusione incompleta per {produttore}/{sede} — in attesa di: {campi_mancanti}")
                    return

                # Abbiamo tutto: usa il buffer come payload completo
                payload = dict(buffer_fusione)

                # Anti-duplicato: se il payload è identico all ultimo salvato, scartiamo.
                # Succede quando il simulatore ripubblica su /sensori per il consenso GEO.
                global _ultimo_payload_salvato
                if payload == _ultimo_payload_salvato:
                    return
                _ultimo_payload_salvato = dict(payload)

                print(f"✅ Fusione completa ESP32+ESP8266 per {produttore}/{sede}: {payload}")

            # ── Da qui in poi identico per tutti i twin ───────────────────────
            temp_aria = payload.get('temp_int')
            temp_int  = payload.get('temp_int')
            umid_int  = payload.get('umid_int')
            temp_est  = payload.get('temp_est')
            valore_co2 = payload.get('co2', 0)
            chiave_sede = f"{produttore}/{sede}"

            # ── ML 1: Temperatura vino con smorzamento fisico ─────────────────
            # Recupera l'ultimo valore di temp_vino dal buffer storico
            storico = _storico_vino.setdefault(chiave_sede, [])
            temp_vino_precedente = storico[-1] if storico else None
            temp_vino_smorzata = ml_temp_vino_smorzata(temp_aria, temp_vino_precedente)

            # Mantieni compatibilità con il campo originale
            temp_vino_calc = temp_vino_smorzata

            # ── ML 2: Trend temperatura vino (regressione lineare) ────────────
            if temp_vino_smorzata is not None:
                storico.append(temp_vino_smorzata)
                if len(storico) > MAX_STORICO_TREND:
                    storico.pop(0)

            trend = ml_trend_vino(storico) if len(storico) >= 3 else {'pendenza': None, 'minuti_alla_soglia': None}

            # Allarme preavviso: meno di 30 minuti alla soglia critica
            if (trend['minuti_alla_soglia'] is not None and
                    trend['minuti_alla_soglia'] < 30 and
                    trend['pendenza'] > 0):
                print(f"⏱️  PREAVVISO {chiave_sede}: vino raggiungerà 24°C in "
                      f"{trend['minuti_alla_soglia']:.0f} minuti")
                allarmi_recenti.append({
                    "tipo": "PREAVVISO_VINO",
                    "produttore": produttore,
                    "sede": sede,
                    "valore": trend['minuti_alla_soglia'],
                    "ts": datetime.utcnow().isoformat()
                })

            # ── ML 3: Verifica ambiente ───────────────────────────────────────
            ris_ambiente = ml_verifica_ambiente(temp_int, umid_int, valore_co2)

            # ── ML 4: Timer impianti (regressore multi-output) ────────────────
            ris_timer = ml_timer_impianti(temp_int, umid_int, temp_est)

            # Allarmi tradizionali
            if temp_vino_calc and temp_vino_calc > 24.0:
                print(f"🚨 Vino surriscaldato a {sede} ({produttore}) → {temp_vino_calc:.2f}°C")
                allarmi_recenti.append({
                    "tipo": "VINO_CALDO",
                    "produttore": produttore,
                    "sede": sede,
                    "valore": temp_vino_calc,
                    "ts": datetime.utcnow().isoformat()
                })

            allarme_attivo = valore_co2 > 1000
            if allarme_attivo:
                allarmi_recenti.append({
                    "tipo": "CO2_ALTA",
                    "produttore": produttore,
                    "sede": sede,
                    "valore": valore_co2,
                    "ts": datetime.utcnow().isoformat()
                })

            # ── ML 5: Risultato verifica ambiente → RisultatoML ───────────────
            with app.app_context():
                # Salva dato sensore con campi ML
                db.session.add(DatoSensore(
                    produttore=produttore, sede=sede,
                    temp_int=temp_int, temp_est=temp_est,
                    umid_int=umid_int, umid_est=payload.get('umid_est'),
                    co2=valore_co2, allarme_co2=allarme_attivo,
                    temp_vino_proiettata=temp_vino_calc,
                    temp_vino_smorzata=temp_vino_smorzata,
                    timer_ac_minuti=ris_timer.get('timer_ac_minuti'),
                    timer_umid_minuti=ris_timer.get('timer_umid_minuti'),
                    minuti_alla_soglia=trend.get('minuti_alla_soglia'),
                    trend_vino_pendenza=trend.get('pendenza'),
                ))

                # Salva risultato verifica ambiente se c'è qualcosa da segnalare
                if ris_ambiente and ris_ambiente['esito'] != 'ok':
                    db.session.add(RisultatoML(
                        produttore=produttore, sede=sede,
                        modello=ris_ambiente['modello'],
                        esito=ris_ambiente['esito'],
                        valore=temp_int,
                        dettaglio=ris_ambiente['dettaglio']
                    ))

                db.session.commit()

        # ── 2. [GEO M2M] Sensore esterno sospetto: cantine/zona/<zona>/sensore_sospetto
        elif len(parti) >= 4 and parti[1] == 'zona' and parti[3] == 'sensore_sospetto':
            zona = parti[2]
            print(f"🌡️  [GEO M2M] Sensore sospetto in zona {zona}: "
                  f"{payload.get('mittente_sospetto')} devia {payload.get('deviazione')}°C dalla media")
            with app.app_context():
                db.session.add(EventoM2M(
                    pattern='GEO',
                    tipo='SENSORE_EST_SOSPETTO',
                    mittente=payload.get('mittente_sospetto', ''),
                    destinatari=f"tutti i twin di zona {zona}",
                    valore=payload.get('deviazione'),
                    messaggio=payload.get('messaggio', '')
                ))
                db.session.commit()

        # ── 3. [PROD M2M] Allerta CO₂ intra-produttore: cantine/<prod>/allerta_co2
        elif len(parti) >= 3 and parti[2] == 'allerta_co2':
            produttore = parti[1]
            sede_origine = payload.get('sede_origine', '')
            print(f"🏭 [PROD M2M] Allerta CO₂ da {produttore}/{sede_origine}: "
                  f"{payload.get('valore_co2')} ppm")

            # Segna nel buffer allarmi per il suono frontend
            allarmi_recenti.append({
                "tipo": "CO2_ALTA_M2M",
                "produttore": produttore,
                "sede": sede_origine,
                "valore": payload.get('valore_co2', 0),
                "ts": datetime.utcnow().isoformat()
            })

            with app.app_context():
                # Trova le altre sedi dello stesso produttore e aggiungi il log di doppio controllo
                altre_sedi = db.session.query(DatoSensore.sede).filter(
                    DatoSensore.produttore == produttore,
                    DatoSensore.sede != sede_origine
                ).distinct().all()
                sedi_nomi = [s[0] for s in altre_sedi]

                db.session.add(EventoM2M(
                    pattern='PROD',
                    tipo='ALLERTA_CO2_PRODUTTORE',
                    mittente=f"{produttore}/{sede_origine}",
                    destinatari=", ".join(f"{produttore}/{s}" for s in sedi_nomi) or f"altre sedi {produttore}",
                    valore=payload.get('valore_co2'),
                    messaggio=payload.get('messaggio', '')
                ))
                db.session.commit()
                print(f"   💾 Doppio controllo registrato per sedi: {sedi_nomi}")

    except Exception as e:
        print(f"❌ Errore MQTT: {e}")


mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.connect("localhost", 1883, 60)  # ← Cambia con HiveMQ URL per cloud
mqtt_client.loop_start()
#in questa riga il server si mette sempre in ascolto su MQTT in attesa di messaggi


# ── ROUTES ──────────────────────────────────────────────────────────

@app.route('/')
@login_required
def home():
    autorizzati = produttori_autorizzati()
    subq = (
        db.session.query(DatoSensore.produttore, DatoSensore.sede,
                         func.max(DatoSensore.id).label('max_id'))
        .filter(DatoSensore.produttore.in_(autorizzati))
        .group_by(DatoSensore.produttore, DatoSensore.sede)
        .subquery()
    )
    ultimi = (db.session.query(DatoSensore)
              .join(subq, DatoSensore.id == subq.c.max_id)
              .order_by(DatoSensore.sede).all())

    allarmi_co2 = sum(1 for d in ultimi if d.allarme_co2)
    temp_media = (sum(d.temp_int for d in ultimi if d.temp_int) / len(ultimi)) if ultimi else 0

    # Converte gli oggetti SQLAlchemy in dizionari semplici per il template
    dati_sedi_json = [{
        'produttore': d.produttore,
        'sede': d.sede,
        'timestamp': d.timestamp.strftime('%H:%M') if d.timestamp else None,
        'temp_int': float(d.temp_int) if d.temp_int is not None else None,
        'temp_est': float(d.temp_est) if d.temp_est is not None else None,
        'umid_int': float(d.umid_int) if d.umid_int is not None else None,
        'umid_est': float(d.umid_est) if d.umid_est is not None else None,
        'co2': float(d.co2) if d.co2 is not None else None,
        'allarme_co2': bool(d.allarme_co2),
        'temp_vino_proiettata': float(d.temp_vino_proiettata) if d.temp_vino_proiettata is not None else None,
    } for d in ultimi]

    return render_template('hub.html',
                           nome=current_user.username, ruolo=current_user.ruolo,
                           produttori_visibili=autorizzati,
                           dati_sedi=dati_sedi_json,  # ← lista di dizionari, non oggetti SQLAlchemy
                           allarmi_co2=allarmi_co2, temp_media=temp_media)


@app.route('/sede/<nome_sede>')
@login_required
def vista_sede(nome_sede):
    autorizzati = produttori_autorizzati()
    dati_sede = (DatoSensore.query
                 .filter(DatoSensore.sede == nome_sede,
                         DatoSensore.produttore.in_(autorizzati))
                 .order_by(DatoSensore.timestamp.desc()).limit(20).all())
    return render_template('index.html',
                           nome=current_user.username, ruolo=current_user.ruolo,
                           produttori_visibili=autorizzati, dati=dati_sede, sede=nome_sede)


@app.route('/allerte-zona')
@login_required
def allerte_zona():
    """
    Pagina che mostra le comunicazioni M2M tra twin — dimostra Legge 2 Vezzani.
    Filtra per produttore se l utente non è admin.
    """
    autorizzati = produttori_autorizzati()
    query = EventoM2M.query.order_by(EventoM2M.timestamp.desc()).limit(100)
    eventi = query.all()

    # Filtra per produttore se non admin (mostra solo eventi che coinvolgono i propri twin)
    if current_user.ruolo != 'admin':
        eventi = [e for e in eventi if any(p in (e.mittente or '') or p in (e.destinatari or '')
                                           for p in autorizzati)]
    return render_template('allerte_zona.html',
                           nome=current_user.username, ruolo=current_user.ruolo,
                           eventi=eventi)


# ── API JSON (usate dal frontend per polling real-time) ───────────────────────

@app.route('/api/allarmi/recenti')
@login_required
def api_allarmi_recenti():
    """
    Il frontend fa polling ogni 5s su questo endpoint.
    Se ci sono nuovi allarmi, suona il buzzer via Web Audio API.
    """
    autorizzati = produttori_autorizzati()
    filtrati = [a for a in allarmi_recenti if a['produttore'] in autorizzati]
    return jsonify(filtrati)


@app.route('/api/allarmi/recenti/clear', methods=['POST'])
@login_required
def api_allarmi_clear():
    """Il frontend chiama questo dopo aver suonato, per non risuonare lo stesso allarme."""
    autorizzati = produttori_autorizzati()
    da_rimuovere = [a for a in allarmi_recenti if a['produttore'] in autorizzati]
    for a in da_rimuovere:
        try:
            allarmi_recenti.remove(a)
        except ValueError:
            pass
    return jsonify({"ok": True})


@app.route('/api/sede/<nome_sede>/latest')
@login_required
def api_latest(nome_sede):
    autorizzati = produttori_autorizzati()
    subq = (db.session.query(func.max(DatoSensore.id))
            .filter(DatoSensore.sede == nome_sede,
                    DatoSensore.produttore.in_(autorizzati))
            .group_by(DatoSensore.produttore).subquery())
    ultimi = DatoSensore.query.filter(DatoSensore.id.in_(subq)).all()
    return jsonify([{
        'produttore': d.produttore, 'sede': d.sede,
        'timestamp': d.timestamp.isoformat(),
        'temp_int': d.temp_int, 'temp_est': d.temp_est,
        'umid_int': d.umid_int, 'umid_est': d.umid_est,
        'co2': d.co2, 'allarme_co2': d.allarme_co2,
        'temp_vino_proiettata': d.temp_vino_proiettata,
    } for d in ultimi])


# ── ROUTE API ML ──────────────────────────────────────────────────────────────

@app.route('/api/ml/stato-sedi')
@login_required
def api_ml_stato_sedi():
    autorizzati = produttori_autorizzati()
    subq = (
        db.session.query(DatoSensore.produttore, DatoSensore.sede,
                         func.max(DatoSensore.id).label('max_id'))
        .filter(DatoSensore.produttore.in_(autorizzati))
        .group_by(DatoSensore.produttore, DatoSensore.sede)
        .subquery()
    )
    ultimi = (db.session.query(DatoSensore)
              .join(subq, DatoSensore.id == subq.c.max_id).all())

    risultati = []
    for d in ultimi:
        fascia = (FasciaEfficienza.query
                  .filter_by(produttore=d.produttore, sede=d.sede)
                  .order_by(FasciaEfficienza.timestamp.desc()).first())
        risultati.append({
            'produttore':           d.produttore,
            'sede':                 d.sede,
            'timestamp':            d.timestamp.isoformat() if d.timestamp else None,
            'temp_int':             float(d.temp_int)             if d.temp_int             is not None else None,
            'temp_est':             float(d.temp_est)             if d.temp_est             is not None else None,
            'umid_int':             float(d.umid_int)             if d.umid_int             is not None else None,
            'co2':                  float(d.co2)                  if d.co2                  is not None else None,
            'allarme_co2':          bool(d.allarme_co2),
            'temp_vino_proiettata': float(d.temp_vino_proiettata) if d.temp_vino_proiettata is not None else None,
            'temp_vino_smorzata':   float(d.temp_vino_smorzata)   if d.temp_vino_smorzata   is not None else None,
            'timer_ac_minuti':      float(d.timer_ac_minuti)      if d.timer_ac_minuti      is not None else None,
            'timer_umid_minuti':    float(d.timer_umid_minuti)    if d.timer_umid_minuti    is not None else None,
            'minuti_alla_soglia':   float(d.minuti_alla_soglia)   if d.minuti_alla_soglia   is not None else None,
            'trend_pendenza':       float(d.trend_vino_pendenza)  if d.trend_vino_pendenza  is not None else None,
            'fascia':               fascia.fascia  if fascia else None,
            'colore':               fascia.colore  if fascia else None,
            'score':                float(fascia.score) if fascia and fascia.score else None,
        })
    return jsonify(risultati)


@app.route('/api/ml/allarmi-ml')
@login_required
def api_ml_allarmi():
    autorizzati = produttori_autorizzati()
    risultati = (RisultatoML.query
                 .filter(RisultatoML.produttore.in_(autorizzati))
                 .order_by(RisultatoML.timestamp.desc()).limit(50).all())
    return jsonify([{
        'id': r.id, 'timestamp': r.timestamp.isoformat() if r.timestamp else None,
        'produttore': r.produttore, 'sede': r.sede,
        'modello': r.modello, 'esito': r.esito,
        'valore': float(r.valore) if r.valore is not None else None,
        'dettaglio': r.dettaglio,
    } for r in risultati])


@app.route('/api/ml/fascia-efficienza', methods=['GET', 'POST'])
@login_required
def api_ml_fascia():
    autorizzati = produttori_autorizzati()
    if request.method == 'POST':
        body = request.get_json()
        prod = body.get('produttore')
        sede = body.get('sede')
        if prod not in autorizzati:
            return jsonify({'error': 'Non autorizzato'}), 403
        ultimi = (DatoSensore.query.filter_by(produttore=prod, sede=sede)
                  .order_by(DatoSensore.timestamp.desc()).limit(20).all())
        if not ultimi:
            return jsonify({'error': 'Nessun dato disponibile'}), 404
        t_int = sum(d.temp_int for d in ultimi if d.temp_int) / len(ultimi)
        t_est = sum(d.temp_est for d in ultimi if d.temp_est) / len(ultimi)
        ris = ml_fascia_efficienza(t_int, t_est, body.get('volume_cantina', 500),
                                    body.get('valore_isolamento', 1.0), prod, sede)
        if ris:
            db.session.add(FasciaEfficienza(
                produttore=prod, sede=sede,
                fascia=ris['fascia'], colore=ris['colore'], score=ris['score'],
                volume_cantina=body.get('volume_cantina'),
                valore_isolamento=body.get('valore_isolamento'),
                temp_int_media=t_int, temp_est_media=t_est,
                delta_temp=abs(t_int - t_est)
            ))
            db.session.commit()
            return jsonify(ris)
        return jsonify({'error': 'Calcolo non riuscito'}), 500
    fasce = (FasciaEfficienza.query
             .filter(FasciaEfficienza.produttore.in_(autorizzati))
             .order_by(FasciaEfficienza.timestamp.desc()).limit(20).all())
    return jsonify([{
        'produttore': f.produttore, 'sede': f.sede,
        'fascia': f.fascia, 'colore': f.colore,
        'score': float(f.score) if f.score else None,
        'timestamp': f.timestamp.isoformat() if f.timestamp else None,
    } for f in fasce])


@app.route('/api/eventi-m2m/recenti')
@login_required
def api_eventi_m2m():
    eventi = (EventoM2M.query
              .order_by(EventoM2M.timestamp.desc()).limit(20).all())
    return jsonify([{
        'id': e.id, 'timestamp': e.timestamp.isoformat() if e.timestamp else None,
        'pattern': e.pattern, 'tipo': e.tipo,
        'mittente': e.mittente, 'destinatari': e.destinatari,
        'valore': float(e.valore) if e.valore is not None else None,
        'messaggio': e.messaggio,
    } for e in eventi])


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = False
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user and check_password_hash(user.password_hash, request.form['password']):
            login_user(user)
            return redirect(url_for('home'))
        error = True
    return render_template('login.html', error=error)


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)