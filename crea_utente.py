from server import app, db, User
from werkzeug.security import generate_password_hash

with app.app_context():
    nome_utente = "FratelliUrbani"

    # La password viene criptata all'istante!
    password_criptata = generate_password_hash("Lucca")

    nuovo_utente = User(username=nome_utente, password_hash=password_criptata)

    db.session.add(nuovo_utente)
    db.session.commit()

    print(f"✅ Utente '{nome_utente}' creato con successo! Ora puoi fare il login.")