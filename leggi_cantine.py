import sqlite3


def esplora_database():
    print("🔍 Lettura delle ultime 5 righe salvate nel Database...\n")

    # Connettiti al database
    conn = sqlite3.connect('database_cantine.db')
    cursor = conn.cursor()

    try:
        # Flask-SQLAlchemy di solito chiama la tabella "dato_sensore" (tutto minuscolo)
        cursor.execute("SELECT * FROM dato_sensore ORDER BY timestamp DESC LIMIT 15")
        righe = cursor.fetchall()

        if not righe:
            print("📭 Il database è ancora vuoto!")
        else:
            # Stampiamo i nomi delle colonne per farti capire l'ordine
            nomi_colonne = [description[0] for description in cursor.description]
            print(f"COLONNE: {nomi_colonne}\n")

            for riga in righe:
                print(riga)

    except sqlite3.OperationalError as e:
        print(f"❌ Errore (forse la tabella non esiste ancora o ha un nome diverso): {e}")

    conn.close()


if __name__ == '__main__':
    esplora_database()