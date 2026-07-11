from flask import Flask, render_template, request, jsonify, redirect, url_for, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from sqlalchemy import func
import os
import json
import paho.mqtt.client as mqtt

app = Flask(__name__)
app.config['SECRET_KEY'] = 'chiave-segreta-urbani'
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'database_cantine.db')

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# ── MODELLI ────────────────────────────────────────────────────────────────────

class User(UserMixin, db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(100), unique=True)
    password_hash = db.Column(db.String(200))
    # ruolo: 'admin' | 'urbani' | 'rossi' | 'bianchi'
    ruolo         = db.Column(db.String(50), default='urbani')


class DatoSensore(db.Model):
    id                   = db.Column(db.Integer, primary_key=True)
    timestamp            = db.Column(db.DateTime, default=datetime.utcnow)
    produttore           = db.Column(db.String(50))
    sede                 = db.Column(db.String(50))
    temp_int             = db.Column(db.Float)
    temp_est             = db.Column(db.Float)
    umid_int             = db.Column(db.Float)
    umid_est             = db.Column(db.Float)
    co2                  = db.Column(db.Float)
    allarme_co2          = db.Column(db.Boolean, default=False)
    temp_vino_proiettata = db.Column(db.Float)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


with app.app_context():
    db.create_all()
    # Aggiunge la colonna 'ruolo' se il DB esiste già senza di essa
    try:
        db.session.execute(db.text("ALTER TABLE user ADD COLUMN ruolo VARCHAR(50) DEFAULT 'urbani'"))
        db.session.commit()
        print("✅ Colonna 'ruolo' aggiunta al DB esistente.")
    except Exception:
        pass  # Colonna già presente, nessun problema


# ── HELPER: produttori visibili per l'utente loggato ──────────────────────────

def produttori_autorizzati():
    """Restituisce la lista di produttori che l'utente può vedere."""
    if current_user.ruolo == 'admin':
        return ['urbani', 'rossi', 'bianchi']
    return [current_user.ruolo]


# ── MQTT ───────────────────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    print("✅ Connesso al Broker MQTT locale!")
    client.subscribe("cantine/#")


def on_message(client, userdata, msg):
    topic = msg.topic
    print(f"\n👉 Ricevuto messaggio su: {topic}")

    try:
        payload = json.loads(msg.payload.decode('utf-8'))
        parti_topic = topic.split('/')

        if len(parti_topic) >= 4 and parti_topic[3] == "sensori":
            produttore = parti_topic[1]
            sede       = parti_topic[2]

            temp_aria      = payload.get("temp_int")
            temp_vino_calc = temp_aria * 0.95 if temp_aria is not None else None

            if temp_vino_calc and temp_vino_calc > 24.0:
                print(f"🚨 ATTENZIONE a {sede}! Vino in surriscaldamento.")
                client.publish(
                    f"cantine/{produttore}/{sede}/comandi",
                    json.dumps({
                        "azione": "ACCENDI_BUZZER_VINO",
                        "motivo": "Temperatura proiettata oltre soglia",
                        "valore": temp_vino_calc
                    })
                )

            valore_co2     = payload.get("co2", 0)
            allarme_attivo = valore_co2 > 1000

            with app.app_context():
                db.session.add(DatoSensore(
                    produttore=produttore,
                    sede=sede,
                    temp_int=payload.get("temp_int"),
                    temp_est=payload.get("temp_est"),
                    umid_int=payload.get("umid_int"),
                    umid_est=payload.get("umid_est"),
                    co2=valore_co2,
                    allarme_co2=allarme_attivo,
                    temp_vino_proiettata=temp_vino_calc
                ))
                db.session.commit()
                print(f"💾 Salvata riga per {produttore} - {sede}")

    except Exception as e:
        print(f"❌ Errore parsing MQTT: {e}")


mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.connect("localhost", 1883, 60)
mqtt_client.loop_start()


# ── ROTTE ──────────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def home():
    autorizzati = produttori_autorizzati()

    subq = (
        db.session.query(
            DatoSensore.produttore,
            DatoSensore.sede,
            func.max(DatoSensore.id).label('max_id')
        )
        .filter(DatoSensore.produttore.in_(autorizzati))
        .group_by(DatoSensore.produttore, DatoSensore.sede)
        .subquery()
    )

    ultimi = (
        db.session.query(DatoSensore)
        .join(subq, DatoSensore.id == subq.c.max_id)
        .order_by(DatoSensore.sede)
        .all()
    )

    allarmi_co2 = sum(1 for d in ultimi if d.allarme_co2)
    temp_media  = (
        sum(d.temp_int for d in ultimi if d.temp_int) / len(ultimi)
        if ultimi else 0
    )

    return render_template(
        'hub.html',
        nome=current_user.username,
        ruolo=current_user.ruolo,
        produttori_visibili=autorizzati,
        dati_sedi=ultimi,
        allarmi_co2=allarmi_co2,
        temp_media=temp_media
    )


@app.route('/sede/<nome_sede>')
@login_required
def vista_sede(nome_sede):
    autorizzati = produttori_autorizzati()

    dati_sede = (
        DatoSensore.query
        .filter(
            DatoSensore.sede == nome_sede,
            DatoSensore.produttore.in_(autorizzati)
        )
        .order_by(DatoSensore.timestamp.desc())
        .limit(20)
        .all()
    )

    # Blocca accesso diretto via URL a sedi con dati non autorizzati
    # (la sede può esistere ma appartenere solo ad altri produttori)
    return render_template(
        'index.html',
        nome=current_user.username,
        ruolo=current_user.ruolo,
        produttori_visibili=autorizzati,
        dati=dati_sede,
        sede=nome_sede
    )


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