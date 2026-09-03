from server import app, db, User
from werkzeug.security import generate_password_hash

with app.app_context():
    utenti = [
        {"username": "admin", "password": "admin", "ruolo": "admin"},
        {"username": "FratelliUrbani", "password": "Lucca", "ruolo": "urbani"},
        {"username": "Rossi s.r.l.", "password": "Rossi", "ruolo": "rossi"},
        {"username": "Bianchi", "password": "Bianchi", "ruolo": "bianchi"}
    ]

    for u in utenti:
        if not User.query.filter_by(username=u["username"]).first():
            nuovo_utente = User(
                username=u["username"],
                password_hash=generate_password_hash(u["password"]),
                ruolo=u["ruolo"]
            )
            db.session.add(nuovo_utente)

    db.session.commit()
    print("✅ Utenti ricreati con successo!")