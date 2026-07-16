from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from sqlalchemy import func
from collections import deque
import os, json
import paho.mqtt.client as mqtt

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
    "co2": None,
    "temp_est": None,
    "umid_est": None,
}

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
    try:
        db.session.execute(db.text("ALTER TABLE user ADD COLUMN ruolo VARCHAR(50) DEFAULT 'urbani'"))
        db.session.commit()
    except Exception:
        pass


# ── HELPERS ───────────────────────────────────────────────────────────────────

def produttori_autorizzati():
    if current_user.ruolo == 'admin':
        return ['urbani', 'rossi', 'bianchi']
    return [current_user.ruolo]


# ── MQTT ──────────────────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    print("✅ Connesso al Broker MQTT!")
    client.subscribe("cantine/#")  # sensori + allerte zona


def on_message(client, userdata, msg):
    topic = msg.topic

    # Ignora i topic di comando (non sono JSON, li abbiamo pubblicati noi stessi)
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
                print(f"✅ Fusione completa ESP32+ESP8266 per {produttore}/{sede}: {payload}")

            # ── Da qui in poi identico per tutti i twin ───────────────────────
            temp_aria = payload.get('temp_int')
            temp_vino_calc = round(temp_aria * 0.95, 4) if temp_aria is not None else None

            if temp_vino_calc and temp_vino_calc > 24.0:
                print(f"🚨 Vino surriscaldato a {sede} ({produttore}) → {temp_vino_calc:.2f}°C")
                allarmi_recenti.append({
                    "tipo": "VINO_CALDO",
                    "produttore": produttore,
                    "sede": sede,
                    "valore": temp_vino_calc,
                    "ts": datetime.utcnow().isoformat()
                })

            valore_co2 = payload.get('co2', 0)
            allarme_attivo = valore_co2 > 1000

            if allarme_attivo:
                allarmi_recenti.append({
                    "tipo": "CO2_ALTA",
                    "produttore": produttore,
                    "sede": sede,
                    "valore": valore_co2,
                    "ts": datetime.utcnow().isoformat()
                })

            #per salvare tutto all'interno del database una volta che la riga è completa
            #si fa session.add() e session.commit()
            with app.app_context():
                db.session.add(DatoSensore(
                    produttore=produttore, sede=sede,
                    temp_int=payload.get('temp_int'), temp_est=payload.get('temp_est'),
                    umid_int=payload.get('umid_int'), umid_est=payload.get('umid_est'),
                    co2=valore_co2, allarme_co2=allarme_attivo,
                    temp_vino_proiettata=temp_vino_calc
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

    return render_template('hub.html',
                           nome=current_user.username, ruolo=current_user.ruolo,
                           produttori_visibili=autorizzati, dati_sedi=ultimi,
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
        'produttore':            d.produttore,
        'sede':                  d.sede,
        'timestamp':             d.timestamp.isoformat() if d.timestamp else None,
        'temp_int':              float(d.temp_int)  if d.temp_int  is not None else None,
        'temp_est':              float(d.temp_est)  if d.temp_est  is not None else None,
        'umid_int':              float(d.umid_int)  if d.umid_int  is not None else None,
        'umid_est':              float(d.umid_est)  if d.umid_est  is not None else None,
        'co2':                   float(d.co2)       if d.co2       is not None else None,
        'allarme_co2':           bool(d.allarme_co2),
        'temp_vino_proiettata':  float(d.temp_vino_proiettata) if d.temp_vino_proiettata is not None else None,
    } for d in ultimi])


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