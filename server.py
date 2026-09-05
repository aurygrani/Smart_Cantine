from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import func
from collections import deque
import os, json, math, time, random
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

# ── IMPORTA I MODULI ML DELLA COLLEGA ────────────────────────────────────────
# I file devono essere nella stessa cartella di server.py:
#   modelli_ml.py, logica_business.py, produttori.py, config_sedi.py
# Se mancano il server funziona ugualmente (graceful degradation).
try:
    from logica_business import (
        GestoreAllarmiIntelligente,
        verifica_ambiente        as lb_verifica_ambiente,
        avvia_cicli_smart_sistemi,
        recupera_profilo_produttore,
        recupera_dati_sede,
    )
    from modelli_ml import (
        calcola_vino_virtuale,
        rileva_anomalie_sensori,
        ml_prevedi_minuti_rimasti_finestra,
        prevedi_minuti_sistemi,
        prevedi_fascia_sede,
    )
    _MODULI_ML_DISPONIBILI = True
    _gestore = GestoreAllarmiIntelligente()
    print("✅ Moduli ML della collega caricati correttamente")
except ImportError as e:
    _MODULI_ML_DISPONIBILI = False
    _gestore = None
    print(f"⚠️  Moduli ML non disponibili: {e} — il server funziona in modalità stub")

app = Flask(__name__)
app.config['SECRET_KEY'] = 'chiave-segreta-urbani'
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'database_cantine.db')

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# ── GESTIONE FUSO ORARIO ──────────────────────────────────────────────────────
# Tutti i timestamp nel DB sono salvati in UTC (datetime.utcnow(), naive).
# Per la visualizzazione li convertiamo esplicitamente al fuso italiano, e per
# le API JSON serializziamo sempre con l'offset UTC esplicito (+00:00) così il
# browser li interpreta correttamente indipendentemente dal proprio fuso.
FUSO_LOCALE = ZoneInfo("Europe/Rome")


def iso_utc(dt):
    """Serializza un datetime naive (salvato in UTC) in ISO-8601 con offset
    esplicito, es. '2026-09-03T10:15:30+00:00', invece di una stringa 'nuda'
    che il client non può distinguere da un orario già locale."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).isoformat()


def ora_locale(dt, fmt='%d/%m %H:%M:%S'):
    """Converte un datetime naive UTC nel fuso orario italiano (gestisce
    automaticamente ora solare/legale) per la visualizzazione lato server."""
    if dt is None:
        return '—'
    return dt.replace(tzinfo=timezone.utc).astimezone(FUSO_LOCALE).strftime(fmt)


app.jinja_env.filters['ora_locale'] = ora_locale

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
SOGLIA_TEMP_ALTA = 26.0  # °C → accende LED_TEMPERATURA (serve raffrescamento)
SOGLIA_TEMP_BASSA = 10.0  # °C → accende LED_TEMPERATURA (serve riscaldamento)
SOGLIA_UMID_ALTA = 80.0  # %  → accende LED_UMIDITA
SOGLIA_CO2_ALTA = 1000  # ppm → accende LED_CO2 + BUZZER


def calcola_e_invia_comandi(mqtt_client, temp_int, umid_int, co2, forza_temp=None):
    """
    Calcola lo stato degli attuatori in base alle soglie e
    invia il comando all'ESP32 nel formato che si aspetta:
    TEMP=1;UMID=0;CO2=1;BUZZER=0

    TEMP=1  → LED temperatura acceso  = serve un intervento sulla temperatura
              (raffrescamento SE temp_int troppo alta, riscaldamento SE troppo
              bassa — è un unico LED/pin sul firmware ESP32, non distingue le
              due direzioni: vedi ESP32_interno.ino, un solo statoTemperatura)
    TEMP=0  → LED temperatura spento  = temperatura ok
    UMID=1  → LED umidità acceso      = umidità fuori soglia (SOLO umidità,
              nessuna condivisione con la temperatura)
    CO2=1   → LED CO2 acceso          = CO2 elevata
    BUZZER=1→ buzzer attivo           = allarme critico

    forza_temp: se non None (True/False), sovrascrive la decisione su TEMP
    calcolata dalla sola soglia fissa. Usato dal blocco ML (vedi on_message)
    per far arrivare davvero all'ESP32 la raccomandazione del modello
    (es. "accendi il condizionatore" o "accendi il riscaldamento") anche
    quando la temperatura istantanea non ha ancora superato/ceduto la soglia
    fissa — prima questo override non esisteva e il consiglio ML restava
    solo un numero in dashboard, senza mai tradursi in un comando reale.
    """
    led_temp = 1 if (temp_int is not None and
                      (temp_int > SOGLIA_TEMP_ALTA or temp_int < SOGLIA_TEMP_BASSA)) else 0
    if forza_temp is not None:
        led_temp = 1 if forza_temp else 0
    led_umid = 1 if (umid_int is not None and umid_int > SOGLIA_UMID_ALTA) else 0
    led_co2 = 1 if (co2 is not None and co2 > SOGLIA_CO2_ALTA) else 0
    buzzer = 1 if (co2 is not None and co2 > SOGLIA_CO2_ALTA) else 0

    # Buzzer anche per temperatura critica (>30°C)
    if temp_int is not None and temp_int > 30.0:
        buzzer = 1

    comando = f"TEMP={led_temp};UMID={led_umid};CO2={led_co2};BUZZER={buzzer}"
    mqtt_client.publish("cantine/urbani/pievepelago/comandi", comando, qos=1)
    print(f"   📡 Comando → ESP32: {comando}")


def calcola_stato_attuatori(temp_int, umid_int, co2, cfg=None):
    """
    Calcola quali "sistemi" risultano attivi secondo le soglie della sede,
    per la rappresentazione visiva in dashboard (grafico "Sistemi attivi").

    Questa funzione gira per OGNI sede (reale e simulata) e distingue 'ac' e
    'riscaldamento' come due indicatori SEPARATI, più leggibili per chi
    guarda la dashboard — ma è solo una distinzione software/informativa.
    Sull'hardware fisico (solo urbani/pievepelago) le due condizioni pilotano
    lo STESSO LED_TEMPERATURA (vedi calcola_e_invia_comandi): il firmware
    ESP32 ha un solo pin per la temperatura, che si accende sia per troppo
    caldo sia per troppo freddo, e un pin separato e indipendente per
    l'umidità (nessuna condivisione tra i due).

    Ritorna un dizionario con lo stato (0/1) di ogni sistema + le soglie usate.
    """
    soglia_temp_alta  = cfg.soglia_temp_alta  if cfg and cfg.soglia_temp_alta  is not None else SOGLIA_TEMP_ALTA
    soglia_temp_bassa = cfg.soglia_temp_bassa if cfg and cfg.soglia_temp_bassa is not None else SOGLIA_TEMP_BASSA
    soglia_umid_alta  = cfg.soglia_umid_alta  if cfg and cfg.soglia_umid_alta  is not None else SOGLIA_UMID_ALTA
    soglia_co2        = cfg.soglia_co2        if cfg and cfg.soglia_co2        is not None else SOGLIA_CO2_ALTA

    ac            = 1 if (temp_int is not None and temp_int > soglia_temp_alta)  else 0
    riscaldamento = 1 if (temp_int is not None and temp_int < soglia_temp_bassa) else 0
    umidita       = 1 if (umid_int is not None and umid_int > soglia_umid_alta)  else 0
    co2_alto      = 1 if (co2 is not None and co2 > soglia_co2) else 0
    buzzer        = 1 if (co2_alto or (temp_int is not None and temp_int > 30.0)) else 0

    return {
        'ac': ac,
        'riscaldamento': riscaldamento,
        'umidita': umidita,
        'co2': co2_alto,
        'buzzer': buzzer,
        'soglie': {
            'temp_alta':  soglia_temp_alta,
            'temp_bassa': soglia_temp_bassa,
            'umid_alta':  soglia_umid_alta,
            'co2':        soglia_co2,
        }
    }


def calcola_stato_sede(temp_int, umid_int, co2, minuti_alla_soglia, trend_pendenza,
                        cfg=None, fascia_efficienza=None):
    """
    Punteggio sintetico (0-100) dello "stato di salute" della sede, per il
    grafico "Stato della sede" in dashboard. Combina i principali segnali
    fisici (temperatura/umidità vs target, CO2 vs soglia) con gli output dei
    modelli ML (trend previsto della temperatura del vino, fascia di
    efficienza energetica), pesati per importanza.

    Ritorna punteggio, etichetta (Buona/Media/Cattiva), colore e il dettaglio
    dei singoli fattori (per mostrare le barre di scomposizione in UI).
    """
    target_temp = cfg.target_temp if cfg and cfg.target_temp is not None else 18.0
    target_umid = cfg.target_umid if cfg and cfg.target_umid is not None else 65.0
    soglia_co2  = cfg.soglia_co2  if cfg and cfg.soglia_co2  is not None else SOGLIA_CO2_ALTA

    fattori = []

    # 1. Temperatura interna vs target sede (max 30 punti, -6 per grado di scarto)
    if temp_int is not None:
        punti_t = max(0.0, 30.0 - abs(temp_int - target_temp) * 6.0)
    else:
        punti_t = 15.0  # dato mancante → punteggio neutro, non penalizzante
    fattori.append({'nome': 'Temperatura', 'punti': round(punti_t, 1), 'max': 30})

    # 2. Umidità interna vs target sede (max 20 punti, -1 per punto % di scarto)
    if umid_int is not None:
        punti_u = max(0.0, 20.0 - abs(umid_int - target_umid) * 1.0)
    else:
        punti_u = 10.0
    fattori.append({'nome': 'Umidità', 'punti': round(punti_u, 1), 'max': 20})

    # 3. CO2 vs soglia sede (max 25 punti; sotto metà soglia = punteggio pieno,
    #    sopra soglia crolla rapidamente)
    if co2 is not None and soglia_co2:
        rapporto = co2 / soglia_co2
        punti_co2 = max(0.0, 25.0 - max(0.0, rapporto - 0.5) * 40.0)
    else:
        punti_co2 = 12.5
    fattori.append({'nome': 'CO₂', 'punti': round(punti_co2, 1), 'max': 25})

    # 4. Trend vino / minuti alla soglia critica, dal modello ML (max 15 punti;
    #    60+ minuti di margine = punteggio pieno)
    if minuti_alla_soglia is not None:
        punti_vino = min(15.0, max(0.0, minuti_alla_soglia) / 4.0)
    elif trend_pendenza is not None and trend_pendenza <= 0:
        punti_vino = 15.0  # temperatura del vino stabile o in calo: nessun rischio imminente
    else:
        punti_vino = 8.0   # nessun dato ML disponibile → punteggio neutro
    fattori.append({'nome': 'Trend vino (ML)', 'punti': round(punti_vino, 1), 'max': 15})

    # 5. Efficienza energetica stimata dal modello ML (max 10 punti)
    punti_eff = {'A': 10.0, 'B': 6.0, 'C': 2.0}.get(fascia_efficienza, 5.0)
    fattori.append({'nome': 'Efficienza energetica (ML)', 'punti': round(punti_eff, 1), 'max': 10})

    punteggio = round(min(100.0, max(0.0, sum(f['punti'] for f in fattori))), 1)

    if punteggio >= 75:
        stato, colore = 'Buona', 'verde'
    elif punteggio >= 45:
        stato, colore = 'Media', 'giallo'
    else:
        stato, colore = 'Cattiva', 'rosso'

    return {'punteggio': punteggio, 'stato': stato, 'colore': colore, 'fattori': fattori}


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
    timer_risc_minuti    = db.Column(db.Float)   # minuti consigliati di riscaldamento (stessa fonte del climatizzatore, smistata per 'modalita')

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


class ConfigurazioneSede(db.Model):
    """
    Parametri fisici fissi per ogni sede — inseriti una volta dall'operatore.
    Usati dai modelli ML della collega che richiedono volume, isolamento e target.

    prevedi_minuti_sistemi() vuole: temp_est, temp_int, umid_est, umid_int,
                                    target_temp, target_umid, isolamento, volume
    prevedi_fascia_sede()    vuole: temp_est, umidita_est, target_int, isolamento, volume
    """
    id            = db.Column(db.Integer, primary_key=True)
    produttore    = db.Column(db.String(50), nullable=False)
    sede          = db.Column(db.String(50), nullable=False)
    # Parametri fisici della cantina
    volume_m3     = db.Column(db.Float, default=500.0)   # m³ — volume interno cantina
    isolamento    = db.Column(db.Float, default=1.0)     # W/m²K — trasmittanza termica pareti
    # Target ambientali (soglie ottimali per il vino)
    target_temp   = db.Column(db.Float, default=18.0)   # °C target temperatura interna
    target_umid   = db.Column(db.Float, default=65.0)   # % target umidità interna
    # Soglie di allarme personalizzate per sede (sovrascrivono i default globali)
    soglia_co2    = db.Column(db.Float, default=1000.0)  # ppm — può essere abbassata da M2M
    soglia_temp_alta  = db.Column(db.Float, default=26.0)
    soglia_temp_bassa = db.Column(db.Float, default=10.0)
    soglia_umid_alta  = db.Column(db.Float, default=80.0)
    # Metadati
    note          = db.Column(db.String(200))
    aggiornato_il = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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
        "ALTER TABLE dato_sensore ADD COLUMN timer_risc_minuti FLOAT",
        "ALTER TABLE dato_sensore ADD COLUMN minuti_alla_soglia FLOAT",
        "ALTER TABLE dato_sensore ADD COLUMN trend_vino_pendenza FLOAT",
    ]
    for sql in migrazioni:
        try:
            db.session.execute(db.text(sql))
            db.session.commit()
        except Exception:
            pass  # Colonna già presente

    # Seed configurazioni di default per tutte le 9 sedi se non esistono ancora
    # I valori sono modificabili dall'operatore via API /api/configurazione/<prod>/<sede>
    _sedi_default = [
        ('urbani',  'pievepelago', 400.0, 0.8,  16.0, 70.0),  # montagna: target più basso, umidità alta
        ('urbani',  'vignola',     500.0, 1.0,  18.0, 65.0),
        ('urbani',  'carpi',       600.0, 1.2,  18.0, 65.0),  # pianura: volume maggiore
        ('rossi',   'pievepelago', 350.0, 0.8,  17.0, 68.0),  # rossi: target leggermente più caldo
        ('rossi',   'vignola',     450.0, 1.0,  19.0, 62.0),
        ('rossi',   'carpi',       550.0, 1.2,  19.0, 62.0),
        ('bianchi', 'pievepelago', 380.0, 0.8,  14.0, 72.0),  # bianchi: target più freddo, più umido
        ('bianchi', 'vignola',     480.0, 1.0,  15.0, 70.0),
        ('bianchi', 'carpi',       580.0, 1.2,  15.0, 70.0),
    ]
    for prod, sede, vol, isol, t_temp, t_umid in _sedi_default:
        esiste = ConfigurazioneSede.query.filter_by(produttore=prod, sede=sede).first()
        if not esiste:
            db.session.add(ConfigurazioneSede(
                produttore=prod, sede=sede,
                volume_m3=vol, isolamento=isol,
                target_temp=t_temp, target_umid=t_umid
            ))
    db.session.commit()


# ── HELPERS ───────────────────────────────────────────────────────────────────

def produttori_autorizzati():
    """
    Ritorna la lista dei produttori che l'utente corrente può vedere.

    NB: il nome del produttore viene sempre normalizzato in minuscolo qui,
    perché sul lato MQTT (on_message) il produttore viene salvato nel DB
    forzato in minuscolo (parti[1].lower()). Se il campo `ruolo` dell'utente
    fosse salvato con una maiuscola diversa (es. "Bianchi" invece di
    "bianchi") o spazi extra, un confronto case-sensitive tipo
    `DatoSensore.produttore.in_(['Bianchi'])` non troverebbe MAI le righe
    salvate come 'bianchi' — sembrerebbe che "i dati non arrivano" quando in
    realtà arrivano e vengono solo filtrati via silenziosamente.
    """
    ruolo_normalizzato = (current_user.ruolo or '').strip().lower()
    if ruolo_normalizzato == 'admin':
        return ['urbani', 'rossi', 'bianchi']
    return [ruolo_normalizzato]


def get_config_sede(produttore: str, sede: str) -> ConfigurazioneSede:
    """
    Restituisce la configurazione fisica di una sede.
    Usato dalle funzioni ML per passare volume, isolamento e target
    ai modelli della collega senza doverli passare manualmente ogni volta.

    Uso tipico in on_message:
        cfg = get_config_sede(produttore, sede)
        minuti_clima, minuti_umid = prevedi_minuti_sistemi(
            temp_est, temp_int, umid_est, umid_int,
            cfg.target_temp, cfg.target_umid,
            cfg.isolamento, cfg.volume_m3
        )
    """
    cfg = ConfigurazioneSede.query.filter_by(produttore=produttore, sede=sede).first()
    if cfg is None:
        # Fallback con valori di default se la sede non è ancora configurata
        cfg = ConfigurazioneSede(
            produttore=produttore, sede=sede,
            volume_m3=500.0, isolamento=1.0,
            target_temp=18.0, target_umid=65.0
        )
    return cfg


def recupera_ultima_fascia(produttore: str, sede: str):
    """Ritorna la lettera 'A'/'B'/'C' dell'ultima fascia di efficienza energetica
    calcolata per la sede (modello ML di Alessia), oppure None se non è mai
    stata calcolata — usata da calcola_stato_sede() come uno dei fattori del
    punteggio "Stato della sede"."""
    ultima = (FasciaEfficienza.query
              .filter_by(produttore=produttore, sede=sede)
              .order_by(FasciaEfficienza.timestamp.desc())
              .first())
    return ultima.fascia if ultima else None


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


def ml_timer_impianti(temp_int, umid_int, temp_est, umid_est=None,
                       temp_target=18.0, umid_target=65.0,
                       isolamento=1.0, volume=500.0):
    """
    [STUB → usato SOLO quando i moduli della collega non sono importabili]
    Firma aggiornata per corrispondere esattamente a prevedi_minuti_sistemi():
        prevedi_minuti_sistemi(temp_est, temp_int, umid_est, umid_int,
                               target_temp, target_umid, isolamento, volume)

    Come avvia_cicli_smart_sistemi() (logica_business.py), il "climatizzatore"
    è UN SOLO sistema che copre sia il raffreddamento che il riscaldamento:
    qui distinguiamo la direzione dal segno di (temp_int - temp_target),
    esattamente come fa lei con 'delta_temp' e 'modalita'.

    Ritorna: {'timer_ac_minuti': float, 'timer_risc_minuti': float, 'timer_umid_minuti': float}
    """
    delta_temp = (temp_int or 0) - temp_target
    delta_umid = (umid_int or 0) - umid_target

    # ── Usa il modello reale quando disponibile ──────────────────────────────
    if _modello_timer_ac is not None and ML_DISPONIBILE.get('numpy'):
        import numpy as np
        # Ordine feature ESATTO richiesto da prevedi_minuti_sistemi():
        # [temp_est, temp_int, umid_est, umid_int, target_temp, target_umid, isolamento, volume]
        X = np.array([[
            temp_est or 0,
            temp_int or 0,
            umid_est or 0,
            umid_int or 0,
            temp_target,
            umid_target,
            isolamento,
            volume
        ]])
        pred = _modello_timer_ac.predict(X)[0]
        minuti_clima = round(float(pred[0]), 1)
        minuti_umid = round(float(pred[1]), 1)
        if delta_temp < 0:
            return {'timer_ac_minuti': 0.0, 'timer_risc_minuti': minuti_clima, 'timer_umid_minuti': minuti_umid}
        return {'timer_ac_minuti': minuti_clima, 'timer_risc_minuti': 0.0, 'timer_umid_minuti': minuti_umid}

    # ── Fallback proporzionale (stub attivo finché il .pkl non arriva) ───────
    timer_umid = max(0.0, round(abs(delta_umid) * 1.5, 1)) if delta_umid > 5 else 0.0
    if delta_temp < 0:
        return {'timer_ac_minuti': 0.0,
                'timer_risc_minuti': max(0.0, round(-delta_temp * 3.0, 1)),
                'timer_umid_minuti': timer_umid}
    return {'timer_ac_minuti': max(0.0, round(delta_temp * 3.0, 1)),
            'timer_risc_minuti': 0.0,
            'timer_umid_minuti': timer_umid}


def ml_fascia_efficienza(temp_int_media, temp_est_media,
                          volume_cantina, valore_isolamento,
                          produttore, sede):
    """
    [STUB] Predice la fascia energetica della cantina.
    In produzione: sostituire con il modello pre-trainato di Alessia.

    Ritorna: {'fascia': 'A'|'B'|'C', 'colore': 'verde'|'giallo'|'rosso', 'score': float}
    """
    # Usa prevedi_fascia_sede() della collega se disponibile
    # Firma: prevedi_fascia_sede(temp_est, umidita_est, target_int, isolamento, volume)
    if _MODULI_ML_DISPONIBILI:
        try:
            profilo = recupera_profilo_produttore(produttore) or {}
            target_int = profilo.get('target_ambiente_temp', 18.0)
            # temp_est_media usata come proxy di umidita_est (non disponibile come media)
            # La collega potrà affinare quando avrà la media umidità esterna
            fascia_raw = prevedi_fascia_sede(
                temp_est_media,
                temp_est_media,   # placeholder umidita_est
                target_int,
                valore_isolamento,
                volume_cantina
            )
            fascia = str(fascia_raw)
            colore = 'verde' if fascia == 'A' else ('giallo' if fascia == 'B' else 'rosso')
            return {'fascia': fascia, 'colore': colore, 'score': 0.5}
        except Exception as e:
            print(f"⚠️  prevedi_fascia_sede errore: {e}")

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

# Ultimo stato calcolato degli attuatori per ogni sede (per la dashboard) —
# { 'urbani/pievepelago': {'ac':1,'riscaldamento':0,'umidita':0,'co2':1,'buzzer':1, ...} }
# Calcolato per TUTTE le sedi (reali e simulate), a differenza del comando MQTT
# che oggi viene inviato solo al twin fisico urbani/pievepelago.
_stato_attuatori: dict[str, dict] = {}

# Ultimo punteggio "Stato della sede" calcolato (Buona/Media/Cattiva) —
# { 'urbani/pievepelago': {'punteggio':82.5,'stato':'Buona','colore':'verde','fattori':[...]} }
_stato_sede: dict[str, dict] = {}

# Tiene traccia di quali sedi hanno l'allarme CO2 già "attivo", per generare
# l'evento in allarmi_recenti (che fa suonare il beep sul frontend) solo al
# momento del superamento soglia (fronte di salita) — non ad ogni singola
# lettura CO2 finché il valore resta alto, altrimenti il beep suonerebbe in
# continuazione ogni ~5-10s per tutta la durata del picco.
_stato_allarme_co2: dict[str, bool] = {}
_stato_allarme_co2_m2m: dict[str, bool] = {}

# Contatore cicli per ogni sede — usato per eseguire il GestoreAllarmiIntelligente
# solo ogni N cicli (evita di appesantire il flusso MQTT con query DB ad ogni messaggio)
_contatore_cicli: dict[str, int] = {}


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

            # ── BLOCCO ML + SALVATAGGIO DB (tutto dentro app_context) ─────────
            with app.app_context():
                try:
                    # Piccolo sleep randomico per sfalsare le scritture su SQLite
                    # ed evitare Database Lock dovuti ai burst del simulatore
                    if produttore != 'urbani':
                        time.sleep(random.uniform(0.01, 0.1))

                    # ML 1: Temperatura vino con smorzamento fisico
                    storico = _storico_vino.setdefault(chiave_sede, [])
                    temp_vino_precedente = storico[-1] if storico else None

                    if _MODULI_ML_DISPONIBILI:
                        temp_vino_smorzata = calcola_vino_virtuale(temp_aria, temp_vino_precedente, alfa=0.02)
                    else:
                        temp_vino_smorzata = ml_temp_vino_smorzata(temp_aria, temp_vino_precedente)

                    temp_vino_calc = temp_vino_smorzata

                    # ML 2: Trend temperatura vino
                    if temp_vino_smorzata is not None:
                        storico.append(temp_vino_smorzata)
                        if len(storico) > MAX_STORICO_TREND:
                            storico.pop(0)

                    if _MODULI_ML_DISPONIBILI and len(storico) >= 4:
                        minuti_trascorsi_storico = [i * (10 / 60.0) for i in range(len(storico))]
                        profilo_trend = recupera_profilo_produttore(produttore) or {}
                        target_vino = profilo_trend.get('target_vino_temp', 18.0)
                        tolleranza = profilo_trend.get('tolleranza_vino_temp', 1.5)
                        minuti_rimasti = ml_prevedi_minuti_rimasti_finestra(
                            minuti_trascorsi_storico, storico,
                            target_vino, tolleranza, finestra=5
                        )
                        trend = {
                            'pendenza': None,
                            'minuti_alla_soglia': float(minuti_rimasti) if minuti_rimasti is not None else None
                        }
                    else:
                        trend = ml_trend_vino(storico) if len(storico) >= 3 else {'pendenza': None,
                                                                                  'minuti_alla_soglia': None}

                    if (trend['minuti_alla_soglia'] is not None and
                            trend['minuti_alla_soglia'] < 30 and
                            trend['minuti_alla_soglia'] >= 0):
                        print(f"⏱️  PREAVVISO {chiave_sede}: vino raggiungerà soglia in "
                              f"{trend['minuti_alla_soglia']:.0f} minuti")
                        allarmi_recenti.append({
                            "tipo": "PREAVVISO_VINO",
                            "produttore": produttore,
                            "sede": sede,
                            "valore": trend['minuti_alla_soglia'],
                            "ts": iso_utc(datetime.utcnow())
                        })

                    # ML 3: Verifica ambiente
                    if _MODULI_ML_DISPONIBILI:
                        profilo_amb = recupera_profilo_produttore(produttore) or {}
                        comandi_ambiente = lb_verifica_ambiente(
                            temp_int or 0, umid_int or 0,
                            target_temp=profilo_amb.get('target_ambiente_temp', 18.0),
                            target_umidita=profilo_amb.get('target_ambiente_umid', 65.0),
                            tolleranza_t=profilo_amb.get('tolleranza_temp_ambiente', 2.0),
                            tolleranza_u=profilo_amb.get('tolleranza_umid_ambiente', 5.0)
                        )
                        ha_problemi = any(v != 'OFF' for v in comandi_ambiente.values())
                        ris_ambiente = {
                            'esito': 'warn' if ha_problemi else 'ok',
                            'dettaglio': f"Clima: {comandi_ambiente['climatizzatore']} | Umid: {comandi_ambiente['sistema_umidita']}",
                            'modello': 'verifica_ambiente',
                            'comandi': comandi_ambiente
                        }
                    else:
                        ris_ambiente = ml_verifica_ambiente(temp_int, umid_int, valore_co2)

                    # ML 4: Timer impianti
                    cfg = get_config_sede(produttore, sede)

                    # ── Stato attuatori per la dashboard ("Sistemi attivi") ──────
                    # Calcolato per ogni sede (reale o simulata), non solo per il
                    # twin fisico che riceve realmente il comando MQTT.
                    _stato_attuatori[chiave_sede] = {
                        **calcola_stato_attuatori(temp_int, umid_int, valore_co2, cfg),
                        'timestamp': iso_utc(datetime.utcnow())
                    }

                    # ── Stato di salute della sede ("Buona"/"Media"/"Cattiva") ────
                    # Combina soglie fisiche + output dei modelli ML (trend vino,
                    # fascia di efficienza energetica) in un punteggio unico.
                    fascia_ml = recupera_ultima_fascia(produttore, sede)
                    _stato_sede[chiave_sede] = calcola_stato_sede(
                        temp_int, umid_int, valore_co2,
                        trend.get('minuti_alla_soglia'), trend.get('pendenza'),
                        cfg=cfg, fascia_efficienza=fascia_ml
                    )

                    if _MODULI_ML_DISPONIBILI:
                        profilo_timer = recupera_profilo_produttore(produttore) or {}
                        config_fisica = recupera_dati_sede(produttore, sede)
                        target_temp_timer = profilo_timer.get('target_ambiente_temp', cfg.target_temp)
                        comandi_motori = avvia_cicli_smart_sistemi(
                            temp_est or 0, temp_int or 0,
                            payload.get('umid_est') or 0, umid_int or 0,
                            target_temp=target_temp_timer,
                            target_umid=profilo_timer.get('target_ambiente_umid', cfg.target_umid),
                            isolamento=config_fisica.get('isolamento', cfg.isolamento),
                            volume=config_fisica.get('volume', cfg.volume_m3)
                        )

                        # ── 'climatizzatore' è UN solo sistema che copre sia il
                        # raffreddamento che il riscaldamento (vedi 'modalita' in
                        # avvia_cicli_smart_sistemi, logica_business.py): prima
                        # prendevamo solo 'timer_minuti' e lo mostravamo sempre
                        # come "AC", quindi quando il modello raccomandava
                        # riscaldamento (modalita 'RISCALDAMENTO_*') il numero
                        # finiva comunque nel riquadro AC — o peggio, veniva
                        # interpretato come "accendi il condizionatore".
                        # Qui leggiamo la modalita per smistare il valore nel
                        # riquadro giusto (AC vs riscaldamento).
                        info_clima = comandi_motori['climatizzatore']
                        modalita_clima = (info_clima.get('modalita') or '').upper()
                        minuti_clima = info_clima.get('timer_minuti') or 0

                        if minuti_clima <= 0:
                            timer_ac_min, timer_risc_min = 0.0, 0.0
                        elif 'RISCALDAMENTO' in modalita_clima:
                            timer_ac_min, timer_risc_min = 0.0, float(minuti_clima)
                        elif 'RAFFREDDAMENTO' in modalita_clima:
                            timer_ac_min, timer_risc_min = float(minuti_clima), 0.0
                        else:
                            # Fallback quando il .pkl non è disponibile e
                            # avvia_cicli_smart_sistemi() ritorna 'STANDARD':
                            # decidiamo la direzione dal segno dello scarto
                            # dalla temperatura target (stessa logica che usa
                            # lei internamente per calcolare 'modalita').
                            if (temp_int or 0) < target_temp_timer:
                                timer_ac_min, timer_risc_min = 0.0, float(minuti_clima)
                            else:
                                timer_ac_min, timer_risc_min = float(minuti_clima), 0.0

                        ris_timer = {
                            'timer_ac_minuti': timer_ac_min,
                            'timer_risc_minuti': timer_risc_min,
                            'timer_umid_minuti': comandi_motori['sistema_umidita']['timer_minuti'],
                        }
                    else:
                        ris_timer = ml_timer_impianti(
                            temp_int, umid_int, temp_est,
                            umid_est=payload.get('umid_est'),
                            temp_target=cfg.target_temp,
                            umid_target=cfg.target_umid,
                            isolamento=cfg.isolamento,
                            volume=cfg.volume_m3
                        )

                    # ── Aggiorna lo stato attuatori (LED "Sistemi attivi") con l'output ML ──
                    # calcola_stato_attuatori() più sopra (riga ~901) è stato calcolato
                    # PRIMA che il timer ML fosse pronto, usando SOLO le soglie fisse.
                    # Per questo, anche quando il pannello ML consigliava "accendi il
                    # condizionatore per 7 minuti", il LED "Aria condizionata" restava
                    # spento: il consiglio del modello non arrivava mai a questo stato —
                    # né per il twin fisico, né (soprattutto) per le sedi SIMULATE, che
                    # non hanno alcun ESP32 reale e dipendono SOLO da questo dizionario
                    # per mostrare qualcosa in dashboard. Qui uniamo (OR logico) la
                    # soglia fissa con il consiglio ML, per ogni sede — reale o virtuale.
                    _stato_attuatori[chiave_sede]['ac'] = 1 if (
                        _stato_attuatori[chiave_sede]['ac'] or (ris_timer.get('timer_ac_minuti') or 0) > 0
                    ) else 0
                    _stato_attuatori[chiave_sede]['riscaldamento'] = 1 if (
                        _stato_attuatori[chiave_sede]['riscaldamento'] or (ris_timer.get('timer_risc_minuti') or 0) > 0
                    ) else 0
                    _stato_attuatori[chiave_sede]['umidita'] = 1 if (
                        _stato_attuatori[chiave_sede]['umidita'] or (ris_timer.get('timer_umid_minuti') or 0) > 0
                    ) else 0

                    # ── Comando ML → ESP32 (SOLO twin fisico urbani/pievepelago) ──
                    # calcola_e_invia_comandi() è già stato chiamato più sopra
                    # (fast-path, appena arriva un dato interno) ma usa SOLO le
                    # soglie fisse: se il modello ML raccomanda di intervenire
                    # sulla temperatura (timer_ac_minuti > 0 per il freddo,
                    # timer_risc_minuti > 0 per il caldo — es. perché il trend
                    # sta peggiorando anche se la soglia non è ancora superata)
                    # quel consiglio restava solo in dashboard e non veniva MAI
                    # inviato all'attuatore reale. Qui, ora che il timer ML è
                    # pronto, rimandiamo il comando forzando il LED_TEMPERATURA
                    # (un solo pin per caldo e freddo, vedi ESP32_interno.ino)
                    # in base al consiglio ML.
                    if produttore == 'urbani' and sede == 'pievepelago':
                        temp_richiede_intervento_ml = (
                            (ris_timer.get('timer_ac_minuti') or 0) > 0 or
                            (ris_timer.get('timer_risc_minuti') or 0) > 0
                        )
                        calcola_e_invia_comandi(
                            client, temp_int, umid_int, valore_co2,
                            forza_temp=temp_richiede_intervento_ml
                        )

                    # Allarmi tradizionali
                    if temp_vino_calc and temp_vino_calc > 24.0:
                        print(f"🚨 Vino surriscaldato a {sede} ({produttore}) → {temp_vino_calc:.2f}°C")
                        allarmi_recenti.append({
                            "tipo": "VINO_CALDO", "produttore": produttore,
                            "sede": sede, "valore": temp_vino_calc,
                            "ts": iso_utc(datetime.utcnow())
                        })

                    # Allarme CO2: la soglia è quella specifica della sede (cfg.soglia_co2,
                    # che l'M2M può abbassare per le sedi "gemelle" di un produttore in
                    # allerta) e non un valore fisso uguale per tutti.
                    allarme_attivo = valore_co2 > cfg.soglia_co2

                    # L'evento che fa scattare il BEEP sul frontend viene generato solo
                    # al momento in cui la sede *entra* in allarme (fronte di salita),
                    # non ad ogni singola lettura finché il valore resta sopra soglia —
                    # altrimenti il suono ripartirebbe ad ogni ciclo (~ogni 5-10s).
                    era_gia_in_allarme = _stato_allarme_co2.get(chiave_sede, False)
                    if allarme_attivo and not era_gia_in_allarme:
                        allarmi_recenti.append({
                            "tipo": "CO2_ALTA", "produttore": produttore,
                            "sede": sede, "valore": valore_co2,
                            "ts": iso_utc(datetime.utcnow())
                        })
                    _stato_allarme_co2[chiave_sede] = allarme_attivo
                    if not allarme_attivo:
                        # Rientrata sotto soglia: sblocca anche la deduplica dell'eco
                        # M2M (chiave_sede == "produttore/sede", stesso formato usato
                        # per l'evento CO2_ALTA_M2M), pronta a un nuovo allarme futuro.
                        _stato_allarme_co2_m2m.pop(chiave_sede, None)

                    # Salva nel DB
                    db.session.add(DatoSensore(
                        produttore=produttore, sede=sede,
                        temp_int=temp_int, temp_est=temp_est,
                        umid_int=umid_int, umid_est=payload.get('umid_est'),
                        co2=valore_co2, allarme_co2=allarme_attivo,
                        temp_vino_proiettata=temp_vino_calc,
                        temp_vino_smorzata=temp_vino_smorzata,
                        timer_ac_minuti=ris_timer.get('timer_ac_minuti'),
                        timer_umid_minuti=ris_timer.get('timer_umid_minuti'),
                        timer_risc_minuti=ris_timer.get('timer_risc_minuti'),
                        minuti_alla_soglia=trend.get('minuti_alla_soglia'),
                        trend_vino_pendenza=trend.get('pendenza'),
                    ))

                    if ris_ambiente and ris_ambiente['esito'] != 'ok':
                        db.session.add(RisultatoML(
                            produttore=produttore, sede=sede,
                            modello=ris_ambiente['modello'],
                            esito=ris_ambiente['esito'],
                            valore=temp_int,
                            dettaglio=ris_ambiente['dettaglio']
                        ))

                    db.session.commit()

                    # ML 6: GestoreAllarmiIntelligente ogni 5 cicli
                    if _MODULI_ML_DISPONIBILI and _gestore is not None:
                        try:
                            _contatore_cicli[chiave_sede] = _contatore_cicli.get(chiave_sede, 0) + 1
                            if _contatore_cicli[chiave_sede] % 5 == 0:
                                rows = db.session.execute(db.text(
                                    "SELECT id, timestamp, produttore, sede, temp_int, temp_est, "
                                    "umid_int, umid_est, co2, allarme_co2, temp_vino_smorzata "
                                    "FROM dato_sensore ORDER BY id DESC LIMIT 30"
                                )).fetchall()
                                if rows:
                                    report = _gestore.analizza(list(reversed(rows)))
                                    for anomalia in report.get('anomalie', []):
                                        db.session.add(RisultatoML(
                                            produttore=produttore, sede=sede,
                                            modello='anomalia_sensore_zscore',
                                            esito='danger', valore=None,
                                            dettaglio=str(anomalia)
                                        ))
                                    for prod_k, sedi_allarme in report.get('stato_sedi', {}).items():
                                        for sede_k, problemi in sedi_allarme.items():
                                            for problema in problemi:
                                                db.session.add(RisultatoML(
                                                    produttore=prod_k, sede=sede_k,
                                                    modello='conformita_sede',
                                                    esito='warn', valore=None,
                                                    dettaglio=str(problema)
                                                ))
                                    qualita = report.get('qualita_vino', '')
                                    if qualita and ('ALLARME' in str(qualita) or 'PREAVVISO' in str(qualita)):
                                        allarmi_recenti.append({
                                            "tipo": "QUALITA_VINO_ML",
                                            "produttore": produttore, "sede": sede,
                                            "valore": 0, "messaggio": str(qualita),
                                            "ts": iso_utc(datetime.utcnow())
                                        })
                                    db.session.commit()
                                    print(f"🤖 [ML] {chiave_sede} → {report.get('stato_globale', '?')}")
                        except Exception as e_ml:
                            print(f"⚠️  Gestore ML errore: {e_ml}")
                except Exception as e_db:
                    db.session.rollback()
                    print(f"❌ Errore salvataggio DB per {produttore}/{sede}: {e_db}")

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

            # Segna nel buffer allarmi per il suono frontend — solo al fronte di
            # salita (difesa in profondità: il simulatore ormai pubblica questo
            # evento una sola volta per superamento soglia, ma teniamo comunque
            # la deduplica lato server nel caso arrivasse più di un messaggio).
            chiave_m2m = f"{produttore}/{sede_origine}"
            if not _stato_allarme_co2_m2m.get(chiave_m2m, False):
                allarmi_recenti.append({
                    "tipo": "CO2_ALTA_M2M",
                    "produttore": produttore,
                    "sede": sede_origine,
                    "valore": payload.get('valore_co2', 0),
                    "ts": iso_utc(datetime.utcnow())
                })
            _stato_allarme_co2_m2m[chiave_m2m] = True

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
        'timestamp': ora_locale(d.timestamp, '%H:%M') if d.timestamp else None,
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
    # --- Controllo sugli autorizzati
    prod_req = request.args.get('prod')
    if prod_req and prod_req.lower() in autorizzati:
        autorizzati = [prod_req.lower()]
    # -------------------------------
    dati_sede = (DatoSensore.query
                 .filter(DatoSensore.sede == nome_sede,
                         DatoSensore.produttore.in_(autorizzati))
                 .order_by(DatoSensore.timestamp.desc()).limit(20).all())

    # Versione serializzabile in JSON per i grafici lato client (incluse le
    # proiezioni ML: temp_vino_proiettata, trend_vino_pendenza, timer_*, ecc.).
    # NB: gli oggetti DatoSensore (SQLAlchemy) non sono serializzabili
    # direttamente con |tojson, servono dizionari semplici.
    dati_sede_json = [{
        'produttore':           d.produttore,
        'sede':                 d.sede,
        'timestamp':            iso_utc(d.timestamp),
        'temp_int':             float(d.temp_int) if d.temp_int is not None else None,
        'temp_est':             float(d.temp_est) if d.temp_est is not None else None,
        'umid_int':             float(d.umid_int) if d.umid_int is not None else None,
        'umid_est':             float(d.umid_est) if d.umid_est is not None else None,
        'co2':                  float(d.co2) if d.co2 is not None else None,
        'allarme_co2':          bool(d.allarme_co2),
        'temp_vino_proiettata': float(d.temp_vino_proiettata) if d.temp_vino_proiettata is not None else None,
        'temp_vino_smorzata':   float(d.temp_vino_smorzata) if d.temp_vino_smorzata is not None else None,
        'timer_ac_minuti':      float(d.timer_ac_minuti) if d.timer_ac_minuti is not None else None,
        'timer_umid_minuti':    float(d.timer_umid_minuti) if d.timer_umid_minuti is not None else None,
        'timer_risc_minuti':    float(d.timer_risc_minuti) if d.timer_risc_minuti is not None else None,
        'minuti_alla_soglia':   float(d.minuti_alla_soglia) if d.minuti_alla_soglia is not None else None,
        'trend_vino_pendenza':  float(d.trend_vino_pendenza) if d.trend_vino_pendenza is not None else None,
    } for d in dati_sede]

    # Stato attuatori (per il grafico "Sistemi attivi") del campionamento più recente.
    # Preferisce lo stato "live" già calcolato dal loop MQTT (_stato_attuatori);
    # se il server è appena partito e non è ancora arrivato nessun messaggio,
    # lo ricalcola al volo dall'ultima riga salvata nel DB.
    attuatori_iniziali = None
    stato_sede_iniziale = None
    if dati_sede:
        ultimo_db = dati_sede[0]
        chiave = f"{ultimo_db.produttore}/{ultimo_db.sede}"
        cfg_iniziale = get_config_sede(ultimo_db.produttore, ultimo_db.sede)

        attuatori_iniziali = _stato_attuatori.get(chiave)
        if attuatori_iniziali is None:
            attuatori_iniziali = calcola_stato_attuatori(
                ultimo_db.temp_int, ultimo_db.umid_int, ultimo_db.co2, cfg_iniziale)
            # Stesso merge con l'output ML applicato nel loop MQTT (vedi on_message):
            # senza questo, al riavvio del server il pannello "Sistemi attivi" mostra
            # di nuovo solo le soglie fisse finché non arriva un nuovo messaggio.
            attuatori_iniziali['ac'] = 1 if (
                attuatori_iniziali['ac'] or (ultimo_db.timer_ac_minuti or 0) > 0
            ) else 0
            attuatori_iniziali['riscaldamento'] = 1 if (
                attuatori_iniziali['riscaldamento'] or (ultimo_db.timer_risc_minuti or 0) > 0
            ) else 0
            attuatori_iniziali['umidita'] = 1 if (
                attuatori_iniziali['umidita'] or (ultimo_db.timer_umid_minuti or 0) > 0
            ) else 0

        # Stato di salute della sede ("Buona"/"Media"/"Cattiva"), stessa logica
        # di preferenza: stato live se già calcolato, altrimenti ricalcolato
        # al volo dall'ultima riga DB + ultima fascia di efficienza nota.
        stato_sede_iniziale = _stato_sede.get(chiave)
        if stato_sede_iniziale is None:
            fascia_iniziale = recupera_ultima_fascia(ultimo_db.produttore, ultimo_db.sede)
            stato_sede_iniziale = calcola_stato_sede(
                ultimo_db.temp_int, ultimo_db.umid_int, ultimo_db.co2,
                ultimo_db.minuti_alla_soglia, ultimo_db.trend_vino_pendenza,
                cfg=cfg_iniziale, fascia_efficienza=fascia_iniziale)

    return render_template('index.html',
                           nome=current_user.username, ruolo=current_user.ruolo,
                           produttori_visibili=autorizzati, dati=dati_sede,
                           dati_json=dati_sede_json, sede=nome_sede,
                           attuatori=attuatori_iniziali,
                           stato_sede=stato_sede_iniziale)


@app.route('/allerte-zona')
@login_required
def allerte_zona():
    """
    Pagina che mostra le comunicazioni M2M tra twin — dimostra Legge 2 Vezzani.

    Regola di visibilità per i produttori (non-admin):
    - pattern 'GEO'  → consenso sulla temperatura ESTERNA tra twin della stessa
                        zona geografica, produttori diversi. È un dato pubblico/
                        ambientale (non riguarda l'interno della cantina di
                        nessuno), quindi visibile a chiunque sia autenticato.
    - pattern 'PROD' → allerta CO₂ (dato INTERNO) scambiata solo tra le sedi
                        dello stesso produttore. Un produttore non deve vedere
                        i livelli di CO₂ di un produttore diverso: l'evento è
                        visibile solo se il produttore che lo ha generato è tra
                        quelli autorizzati per l'utente corrente.
    """
    autorizzati = produttori_autorizzati()
    query = EventoM2M.query.order_by(EventoM2M.timestamp.desc()).limit(100)
    eventi = query.all()

    if (current_user.ruolo or '').strip().lower() != 'admin':
        def visibile(e):
            if e.pattern == 'GEO':
                return True  # informazione esterna, condivisa per definizione
            # Pattern 'PROD' (e simili, futuri): dato interno, mittente nel
            # formato "produttore/sede" — verifichiamo che il produttore che
            # ha generato l'evento sia uno di quelli dell'utente corrente.
            produttore_evento = (e.mittente or '').split('/')[0]
            return produttore_evento in autorizzati
        eventi = [e for e in eventi if visibile(e)]

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
    prod_req = request.args.get('prod')
    if prod_req and prod_req.lower() in autorizzati:
        autorizzati = [prod_req.lower()]
    subq = (db.session.query(func.max(DatoSensore.id))
            .filter(DatoSensore.sede == nome_sede,
                    DatoSensore.produttore.in_(autorizzati))
            .group_by(DatoSensore.produttore).subquery())
    ultimi = DatoSensore.query.filter(DatoSensore.id.in_(subq)).all()
    return jsonify([{
        'produttore': d.produttore, 'sede': d.sede,
        'timestamp': iso_utc(d.timestamp),
        'temp_int': d.temp_int, 'temp_est': d.temp_est,
        'umid_int': d.umid_int, 'umid_est': d.umid_est,
        'co2': d.co2, 'allarme_co2': d.allarme_co2,
        'temp_vino_proiettata': d.temp_vino_proiettata,
        # ── Campi ML aggiunti per l'aggiornamento live del grafico/pannello proiezioni ──
        'temp_vino_smorzata': d.temp_vino_smorzata,
        'timer_ac_minuti': d.timer_ac_minuti,
        'timer_umid_minuti': d.timer_umid_minuti,
        'timer_risc_minuti': d.timer_risc_minuti,
        'minuti_alla_soglia': d.minuti_alla_soglia,
        'trend_vino_pendenza': d.trend_vino_pendenza,
        # ── Stato attuatori (Sistemi attivi) — dal buffer in memoria, non dal DB ──
        'attuatori': _stato_attuatori.get(f"{d.produttore}/{d.sede}"),
        # ── Stato di salute della sede (Buona/Media/Cattiva) — idem, in memoria ──
        'stato_sede': _stato_sede.get(f"{d.produttore}/{d.sede}"),
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
            'timestamp':            iso_utc(d.timestamp),
            'temp_int':             float(d.temp_int)             if d.temp_int             is not None else None,
            'temp_est':             float(d.temp_est)             if d.temp_est             is not None else None,
            'umid_int':             float(d.umid_int)             if d.umid_int             is not None else None,
            'co2':                  float(d.co2)                  if d.co2                  is not None else None,
            'allarme_co2':          bool(d.allarme_co2),
            'temp_vino_proiettata': float(d.temp_vino_proiettata) if d.temp_vino_proiettata is not None else None,
            'temp_vino_smorzata':   float(d.temp_vino_smorzata)   if d.temp_vino_smorzata   is not None else None,
            'timer_ac_minuti':      float(d.timer_ac_minuti)      if d.timer_ac_minuti      is not None else None,
            'timer_umid_minuti':    float(d.timer_umid_minuti)    if d.timer_umid_minuti    is not None else None,
            'timer_risc_minuti':    float(d.timer_risc_minuti)    if d.timer_risc_minuti    is not None else None,
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
        'id': r.id, 'timestamp': iso_utc(r.timestamp),
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
        'timestamp': iso_utc(f.timestamp),
    } for f in fasce])


@app.route('/api/eventi-m2m/recenti')
@login_required
def api_eventi_m2m():
    eventi = (EventoM2M.query
              .order_by(EventoM2M.timestamp.desc()).limit(20).all())
    return jsonify([{
        'id': e.id, 'timestamp': iso_utc(e.timestamp),
        'pattern': e.pattern, 'tipo': e.tipo,
        'mittente': e.mittente, 'destinatari': e.destinatari,
        'valore': float(e.valore) if e.valore is not None else None,
        'messaggio': e.messaggio,
    } for e in eventi])


@app.route('/api/configurazione')
@login_required
def api_config_lista():
    """Lista configurazioni sedi autorizzate."""
    autorizzati = produttori_autorizzati()
    configs = ConfigurazioneSede.query.filter(
        ConfigurazioneSede.produttore.in_(autorizzati)
    ).order_by(ConfigurazioneSede.produttore, ConfigurazioneSede.sede).all()
    return jsonify([{
        'produttore':      c.produttore,
        'sede':            c.sede,
        'volume_m3':       c.volume_m3,
        'isolamento':      c.isolamento,
        'target_temp':     c.target_temp,
        'target_umid':     c.target_umid,
        'soglia_co2':      c.soglia_co2,
        'soglia_temp_alta':  c.soglia_temp_alta,
        'soglia_temp_bassa': c.soglia_temp_bassa,
        'soglia_umid_alta':  c.soglia_umid_alta,
        'note':            c.note,
        'aggiornato_il':   iso_utc(c.aggiornato_il),
    } for c in configs])


@app.route('/api/configurazione/<produttore>/<sede>', methods=['GET', 'PATCH'])
@login_required
def api_config_sede(produttore, sede):
    """
    GET   → legge configurazione di una sede specifica.
    PATCH → aggiorna uno o più campi (solo i campi presenti nel body vengono modificati).

    Esempio body PATCH:
        { "target_temp": 16.0, "volume_m3": 450, "note": "Botti da 500L, pareti in tufo" }
    """
    autorizzati = produttori_autorizzati()
    if produttore not in autorizzati:
        return jsonify({'error': 'Non autorizzato'}), 403

    cfg = ConfigurazioneSede.query.filter_by(produttore=produttore, sede=sede).first()
    if not cfg:
        return jsonify({'error': 'Sede non trovata'}), 404

    if request.method == 'PATCH':
        body = request.get_json() or {}
        campi_modificabili = [
            'volume_m3', 'isolamento', 'target_temp', 'target_umid',
            'soglia_co2', 'soglia_temp_alta', 'soglia_temp_bassa',
            'soglia_umid_alta', 'note'
        ]
        for campo in campi_modificabili:
            if campo in body:
                setattr(cfg, campo, body[campo])
        cfg.aggiornato_il = datetime.utcnow()
        db.session.commit()
        print(f"⚙️  Configurazione {produttore}/{sede} aggiornata: {body}")

    return jsonify({
        'produttore':      cfg.produttore,
        'sede':            cfg.sede,
        'volume_m3':       cfg.volume_m3,
        'isolamento':      cfg.isolamento,
        'target_temp':     cfg.target_temp,
        'target_umid':     cfg.target_umid,
        'soglia_co2':      cfg.soglia_co2,
        'soglia_temp_alta':  cfg.soglia_temp_alta,
        'soglia_temp_bassa': cfg.soglia_temp_bassa,
        'soglia_umid_alta':  cfg.soglia_umid_alta,
        'note':            cfg.note,
        'aggiornato_il':   iso_utc(cfg.aggiornato_il),
    })


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