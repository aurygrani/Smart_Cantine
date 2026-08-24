
import math
import pickle
import numpy as np
from sklearn.linear_model import LinearRegression
import os
# MODELLI FISICI
def calcola_vino_virtuale(temp_int_attuale, temp_vino_precedente=None, alfa=0.02):
    #caso 1: prima lettura per questa cantina, non ha un passato
    if temp_vino_precedente is None:
        return temp_int_attuale
    #caso 2: sistema a regime, applichiamo la formula dell'inerzia termica
    nuova_temp_vino = ((1-alfa)*temp_vino_precedente)+(alfa*temp_int_attuale)
    #arrotondiamo a 2 decimali per avere numeri puliti nel db
    return round(nuova_temp_vino,2)


#def calcola_umidita_lisciata(umid_int_letta, umid_int_precedente=None, beta=0.2):
#    """
#    Filtra gli sbalzi e il rumore del sensore di umidita
#    per evitare che l'umidificatore si accenda e spenga di continuo
#    """
#    if umid_int_precedente is None:
#        return umid_int_letta
#    
#    umidita_pulita=((1-beta)*umid_int_precedente)+(beta*umid_int_letta)
#    return round(umidita_pulita,1)


# Anomaly detection (controllo sensori esterni)
def rileva_anomalie_sensori(dati_sensori_zone, soglia_z=2.0):
    sensori_guasti_totali = []

    for zona, sensori_zona in dati_sensori_zone.items():

        temperature = [d["temp"] for d in sensori_zona.values() if d.get("temp") is not None]
        umidit = [d["umid"] for d in sensori_zona.values() if d.get("umid") is not None]

        # --------------------------------------------------
        # FALLBACK: pochi dati → soglie fisse
        # --------------------------------------------------
        if len(temperature) < 3:
            for nome_sensore, dati in sensori_zona.items():

                problemi = []

                temp = dati.get("temp")
                umid = dati.get("umid")

                if temp is not None and (temp < 5 or temp > 35):
                    problemi.append(f"Temperatura ({temp}°C)")

                if umid is not None and (umid < 20 or umid > 90):
                    problemi.append(f"Umidità ({umid}%)")

                if problemi:
                    sensori_guasti_totali.append(
                        f"{nome_sensore} (a {zona}) -> GUASTO: {' E '.join(problemi)}"
                    )

            continue

        # --------------------------------------------------
        # CALCOLO STATISTICHE
        # --------------------------------------------------
        media_t = sum(temperature) / len(temperature)
        varianza_t = sum((x - media_t) ** 2 for x in temperature) / len(temperature)
        dev_std_t = math.sqrt(varianza_t)

        media_u = sum(umidit) / len(umidit)
        varianza_u = sum((x - media_u) ** 2 for x in umidit) / len(umidit)
        dev_std_u = math.sqrt(varianza_u)

        # Protezione divisione per zero
        if dev_std_t == 0:
            dev_std_t = 0.0001
        if dev_std_u == 0:
            dev_std_u = 0.0001

        # --------------------------------------------------
        #  CONTROLLO Z-SCORE
        # --------------------------------------------------
        for nome_sensore, dati in sensori_zona.items():

            problemi_trovati = []

            temp = dati.get("temp")
            umid = dati.get("umid")

            if temp is not None:
                z_score_t = (temp - media_t) / dev_std_t
                if abs(z_score_t) > soglia_z:
                    problemi_trovati.append(f"Temperatura ({temp}°C)")

            if umid is not None:
                z_score_u = (umid - media_u) / dev_std_u
                if abs(z_score_u) > soglia_z:
                    problemi_trovati.append(f"Umidità ({umid}%)")

            # --------------------------------------------------
            #  CONTROLLO EXTRA HARD (ANTI-OUTLIER CHE NASCONDE)
            # --------------------------------------------------
            if temp is not None and (temp > 35 or temp < 5):
                if f"Temperatura ({temp}°C)" not in problemi_trovati:
                    problemi_trovati.append(f"Temperatura ({temp}°C)")

            if umid is not None and (umid > 95 or umid < 20):
                if f"Umidità ({umid}%)" not in problemi_trovati:
                    problemi_trovati.append(f"Umidità ({umid}%)")

            # --------------------------------------------------
            # OUTPUT
            # --------------------------------------------------
            if problemi_trovati:
                descrizione = " E ".join(problemi_trovati)
                sensori_guasti_totali.append(
                    f"{nome_sensore} (a {zona}) -> GUASTO: {descrizione}"
                )

    return sensori_guasti_totali

def ml_prevedi_minuti_rimasti(minuti_trascorsi,storico_temp,target_vino,tolleranza):
    """
    usa la regressione lineare per tracciare il trend termico e
    restituisce i minuti totali mancanti prima del problema
    """
    if len(storico_temp)<4:
        return None

    # 1. Sicurezza sull'ordinamento cronologico (dal più vecchio al più recente)
    # Se per qualsiasi motivo i dati arrivano invertiti, li riordiniamo
    dati_combinati = sorted(zip(minuti_trascorsi, storico_temp))
    minuti_ordinati, temp_ordinate = zip(*dati_combinati)

    t0=minuti_ordinati[0]
    X=np.array([m-t0 for m in minuti_ordinati]).reshape(-1,1)
    y=np.array(temp_ordinate)

    modello=LinearRegression()
    modello.fit(X,y)

    pendenza=modello.coef_[0]
    intercetta=modello.intercept_

    limite_massimo=target_vino+tolleranza
    limite_minimo=target_vino-tolleranza
    temp_corrente=temp_ordinate[-1]

    if abs(pendenza)<1e-5:
        return None

    #se il vino è già fuori limite
    if temp_corrente >= limite_massimo or temp_corrente <= limite_minimo:
        return 0

    if pendenza >0:
        minuti_al_limite=(limite_massimo-intercetta)/pendenza
        
    elif pendenza <0:
        minuti_al_limite=(limite_minimo-intercetta)/pendenza
    else:
        return None   

    minuti_correnti=X[-1][0]
    minuti_restanti=minuti_al_limite-minuti_correnti

    if minuti_restanti<0:
        return 0

    return int(minuti_restanti)

# =========================
# NUOVA VERSIONE AVANZATA (USA FINESTRA RECENTE)
# =========================

def ml_prevedi_minuti_rimasti_finestra(minuti_trascorsi, storico_temp, target_vino, tolleranza, finestra=5):
    """
    Usa solo gli ultimi N valori → più realistico per IoT
    """

    if len(storico_temp) < finestra:
        return None

    ultimi_minuti = minuti_trascorsi[-finestra:]
    ultime_temp = storico_temp[-finestra:]

    return ml_prevedi_minuti_rimasti(ultimi_minuti, ultime_temp, target_vino, tolleranza)


#MACHINE LEARNING AVANZATO (modelli pre-addestrati)
def prevedi_minuti_sistemi(temp_est, temp_int, umid_est, umid_int, target_temp, target_umid, isolamento,volume):
    """
    (REGRESSORE) prevede gli esatti minuti di accensione dei sistemi di climatizzazione e umidificazione
    fondamentale per fare vedere che ottimizziamo i consumi con il machine learning
    """
    try:
        BASE_DIR=os.path.dirname(__file__)
        path_modello=os.path.join(BASE_DIR,"regressore_sistemi_multi.pkl")

        with open(path_modello,"rb") as f:
            modello_regressore=pickle.load(f)

        condizioni_attuali=np.array([[temp_est, temp_int, umid_est, umid_int, target_temp,target_umid,isolamento,volume]])
        minuti_stimati=modello_regressore.predict(condizioni_attuali)[0]

        minuti_clima=max(0,int(round(minuti_stimati[0])))
        minuti_umidificatore=max(0,int(round(minuti_stimati[1])))

        return minuti_clima,minuti_umidificatore

    except Exception as e:
        print(f"Errore ML sistemi: {e}")
        return None,None

def prevedi_fascia_sede(temp_est, umidita_est, target_int, isolamento, volume):
    """
    (CLASSIFICATORE) predice la fascia di efficienza della cantina
    """
    try:
        BASE_DIR=os.path.dirname(__file__)
        path_modello=os.path.join(BASE_DIR,"classificatore_sedi.pkl")

        with open(path_modello,"rb") as f:
            modello_rf=pickle.load(f)

        nuovi_dati=np.array([[temp_est, umidita_est, target_int, isolamento, volume]])

        fascia_prevista=modello_rf.predict(nuovi_dati)[0]
        return fascia_prevista

    except Exception as e:
        print(f"Errore classificatore: {e}")
        return "SCONOSCIUTA"

