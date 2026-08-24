import modelli_ml as ml
import numpy as np
from produttori import get_profilo_produttore
from config_sedi import SEDI 

def recupera_profilo_produttore(produttore):
    """
    Recupera target e caratteristiche del vino del produttore
    """

    profilo = get_profilo_produttore(produttore)

    if profilo is None:
        print(f"ATTENZIONE: produttore {produttore} non configurato")

        return {
            "tipo_vino": "Sconosciuto",
            "target_ambiente_temp": 18,
            "target_ambiente_umid": 65,
            "target_vino_temp":15,
            "tolleranza_temp_ambiente": 2,
            "tolleranza_umid_ambiente": 5,
            "tolleranza_vino_temp":1.5
        }

    return profilo

def recupera_dati_sede(produttore,sede):

    dati=SEDI.get((produttore,sede))

    if dati is None:
        print(
            f"ATTENZIONE: nessuna configurazione sede per {produttore}-{sede}"
        )
        
        return {
            "volume":500,
            "isolamento":0.7
        }
    
    return dati

def prepara_dati_per_modelli(dati_grezzi_db,secondi_tra_letture=10):
    """
    Riceve la lista di tuple dal DB  
    e la trasforma nei dizionari richiesti dai modelli.
    """
    # 1. Dizionario per: rileva_anomalie_sensori (Z-Score)
    # Formato atteso: { 'città': { 'produttore': {'temp': x, 'umid': y} } }
    dati_sensori_zone = {}
    
    # 2. Dizionario per: verifica_conformita_sedi
    # Lo facciamo organizzato per produttore.
    # Formato atteso: { 'produttore': { 'sede': {'temp_int': x, 'umid_int': y} } }
    dati_sedi_produttori = {}

    storico_temp_vino=[]
    minuti_trascorsi=[]
    storico_sedi={}

    
    for i,riga in enumerate(dati_grezzi_db):
        # Mappatura delle colonne basata sulla tua immagine
        id_record = riga[0]
        timestamp = riga[1]
        produttore = riga[2]
        sede = riga[3] #zona geografica
        temp_int = riga[4]
        temp_est = riga[5]
        umid_int = riga[6]
        umid_est = riga[7]
        co2 = riga[8]
        allarme_co2 = riga[9]
        temp_vino = riga[10]

        profilo=recupera_profilo_produttore(produttore)
        target_ambiente_temp = profilo["target_ambiente_temp"]
        target_ambiente_umid = profilo["target_ambiente_umid"]
        target_vino_temp=profilo["target_vino_temp"]
        tipo_vino = profilo["tipo_vino"]
        tolleranza_temp_ambiente=profilo["tolleranza_temp_ambiente"]
        tolleranza_umid_ambiente=profilo["tolleranza_umid_ambiente"]
        tolleranza_vino_temp=profilo["tolleranza_vino_temp"]

        # --- Popoliamo il dizionario per le anomalie (Z-score) ---
        if sede not in dati_sensori_zone:
            dati_sensori_zone[sede] = {}
        
        # Salviamo l'ultima lettura per quel produttore in quella sede
        dati_sensori_zone[sede][produttore] = {"temp": temp_int, "umid": umid_int}


        # --- Popoliamo il dizionario per la conformità sedi ---
        if produttore not in dati_sedi_produttori:
            dati_sedi_produttori[produttore] = {}
            
        dati_sedi_produttori[produttore][sede] = {"temp_int": temp_int, "umid_int": umid_int, "target_ambiente_temp":target_ambiente_temp,"target_ambiente_umid":target_ambiente_umid,"target_vino_temp":target_vino_temp,"tolleranza_temp_ambiente":tolleranza_temp_ambiente,"tolleranza_umid_ambiente":tolleranza_umid_ambiente,"tolleranza_vino_temp":tolleranza_vino_temp,"tipo_vino":tipo_vino}

        if temp_vino is not None:
            storico_temp_vino.append(temp_vino)
            minuti_trascorsi.append(i*(secondi_tra_letture/60.0))

        if sede not in storico_sedi:
            storico_sedi[sede]=[]

        storico_sedi[sede].append({
            "temp":temp_int,
            "umid":umid_int,
            "timestamp":timestamp
        })

    return dati_sensori_zone, dati_sedi_produttori, storico_temp_vino, minuti_trascorsi,storico_sedi

# ESEMPIO DI UTILIZZO NEL TUO FLUSSO:
# dati_zone, dati_produttori = prepara_dati_per_modelli(risultato_query_sql)

# Ora puoi chiamare direttamente le tue funzioni:
# anomalie = rileva_anomalie_sensori(dati_zone)
# allarmi_rossi = verifica_conformita_sedi(dati_produttori["rossi"], target_temp=18, target_umidita=65)

# -----------------------------
#  NUOVE FUNZIONI ML REALI
# -----------------------------

def calcola_trend(storico, finestra=5):
    if len(storico) < finestra:
        return 0

    ultimi = storico[-finestra:]
    temp = [x["temp"] for x in ultimi]

    return (temp[-1] - temp[0]) / finestra


def prepara_feature(storico):
    X = []
    y = []

    for i in range(2, len(storico)):
        t1 = storico[i-1]["temp"]
        t2 = storico[i-2]["temp"]

        X.append([t1, t2])
        y.append(storico[i]["temp"])

    return np.array(X), np.array(y)


def allena_modello(storico):
    if len(storico) < 3:
        return None

    X, y = prepara_feature(storico)

    from sklearn.linear_model import LinearRegression
    model = LinearRegression()
    model.fit(X, y)

    return model


def prevedi_prossimo_valore(storico, model):
    if model is None or len(storico) < 2:
        return None

    t1 = storico[-1]["temp"]
    t2 = storico[-2]["temp"]

    previsione=model.predict([[t1, t2]])

    return previsione[0]


def varianza_temperatura(storico):
    temp = [x["temp"] for x in storico]
    return np.var(temp)

# LOGICA DI CONTROLLO
def verifica_ambiente(temp_int, umidita_int, target_temp, target_umidita, tolleranza_t=2.0, tolleranza_u=5.0):
    
    #Controlla l'aria della stanza e decide la modalita di funzionamento per l'impianto di
    #trattamento aria. Gestisce riscaldamento, raffreddamento, de/umidificazione
    
    #condizione di base: parametri perfetti, macchinari in stand-by
    comandi={"climatizzatore":"OFF", "sistema_umidita":"OFF"}

    #controllo temperatura
    if temp_int > (target_temp+tolleranza_t):
        comandi["climatizzatore"]="RAFFREDDAMENTO"
    elif temp_int < (target_temp-tolleranza_t):
        comandi["climatizzatore"]="RISCALDAMENTO"

    #controllo umidita
    if umidita_int < (target_umidita-tolleranza_u):
        comandi["sistema_umidita"]="UMIDIFICAZIONE"
    elif umidita_int > (target_umidita+tolleranza_u):
        comandi["sistema_umidita"]="DEUMIDIFICAZIONE"
    
    return comandi

def verifica_conformita_sedi(dati_interni_sedi):
    """
    Controlla lo stato di salute di tutte le sedi di un produttore.
    Segnala quali sedi sono "fuori target" ambientale e qual è il problema specifico.
    Input atteso (es. preparato da Au per le Cantine Rossi):
    {
        "Vignola": {"temp_int": 18.5, "umid_int": 70.0},
        "Carpi": {"temp_int": 15.0, "umid_int": 75.0},
        "Modena": {"temp_int": 25.0, "umid_int": 60.0}
    }
    Ritorna: Un dizionario solo con le sedi in allarme e le cause.
    """
    sedi_in_allarme = {}
    
    
    # Esamino una sede alla volta
    for nome_sede, misurazioni in dati_interni_sedi.items():
        
        target_temp=misurazioni["target_ambiente_temp"]
        target_umidita=misurazioni["target_ambiente_umid"]

        temp=misurazioni["temp_int"]
        umid=misurazioni["umid_int"]

        tolleranza_temp=misurazioni["tolleranza_temp_ambiente"]
        tolleranza_umid=misurazioni["tolleranza_umid_ambiente"]


        problemi_rilevati = []
        
        # 1. Controllo la Temperatura
        if temp is not None:
            if temp > target_temp+tolleranza_temp:
                problemi_rilevati.append(f"Temp. troppo alta ({temp}°C)")
            elif temp < target_temp-tolleranza_temp:
                problemi_rilevati.append(f"Temp. troppo bassa ({temp}°C)")
                
        # 2. Controllo l'Umidità
        if umid is not None:
            if umid > target_umidita+tolleranza_umid:
                problemi_rilevati.append(f"Umidità troppo alta ({umid}%)")
            elif umid < target_umidita-tolleranza_umid:
                problemi_rilevati.append(f"Umidità troppo bassa ({umid}%)")
                
        # Se ho trovato dei problemi, li associo a questa sede
        if problemi_rilevati:
            sedi_in_allarme[nome_sede] = problemi_rilevati
            
    return sedi_in_allarme

def gestore_allarme_qualita(minuti_trascorsi, storico_temp, produttore):
    """
    controlla la sicurezza del vino. Se i limiti non sono ancora stati superati, 
    interroga il modello di machine learning per sapere se stiamo andando verso un problema
    """
    if not storico_temp:
        return "Nessun dato disponibile"

    profilo=recupera_profilo_produttore(produttore)

    target_vino=profilo["target_vino_temp"]
    tolleranza=profilo["tolleranza_vino_temp"]

    temp_vino_attuale=storico_temp[-1]

    #CONTROLLO IMMEDIATO: se è già fuori limite, scatta l'allarme
    if temp_vino_attuale > (target_vino+tolleranza):
        return (
            f"ALLARME_CRITICO_CALDO: "
            f"vino fuori range di sicurezza "
            f"{temp_vino_attuale}°C "
            f"(target {target_vino}°C)"
        )
    if temp_vino_attuale < (target_vino-tolleranza):
        return (
            f"ALLARME_CRITICO_FREDDO: "
            f"vino fuori range di sicurezza "
            f"{temp_vino_attuale}°C "
            f"(target {target_vino}°C)"
        )

   

    #CONTROLLO DI RIENTRO: verifichiamo se stiamo guarendo
    if len(storico_temp) >= 2:
        trend=storico_temp[-1]-storico_temp[-2]
        if temp_vino_attuale>target_vino and trend <0:
            return "OK:Temperatura in discesa, rientro verso il target termico in corso"
        if temp_vino_attuale <target_vino and trend >0:
            return "OK:Temperatura in salita, rientro verso il target termico in corso"

    #CONTROLLO PREDITTIVO: chiediamo all'intelligenza artificiale quanto manca
    minuti_totali=ml.ml_prevedi_minuti_rimasti_finestra(minuti_trascorsi,storico_temp,target_vino,tolleranza)

    #fallback
    if minuti_totali is None:
        minuti_totali=ml.ml_prevedi_minuti_rimasti(minuti_trascorsi, storico_temp, target_vino,tolleranza)
    
    #se il modello calcola un tempo residuo, lo mostra
    #FORMATTAZIONE DEL TESTO
    if minuti_totali is not None:
        ore=minuti_totali // 60
        minuti=minuti_totali%60 
    #creiamo un stringa a seconda dei casi
        if ore >0:
            tempo_str=f"{ore} ore e {minuti} minuti"
        else:
            tempo_str=f"{minuti} minuti"

        return f"PREAVVISO: si stima il superamento del limite termico del vino tra {tempo_str}"

    #se il trend è perfettamente piatto o sicuro
    return "OK: temperatura del vino stabile e in linea con il target"

#timer per i motori
def avvia_cicli_smart_sistemi(temp_est,temp_int,umid_est,umid_int,target_temp,target_umid,isolamento,volume):
    """
    interroga il machine learning e genera il comando fisico per l'hardware
    """
    minuti=ml.prevedi_minuti_sistemi(temp_est, temp_int, umid_est,umid_int, target_temp,target_umid, isolamento,volume)

    #gestione del fallback in caso di errore
    if minuti is None or minuti[0] is None or minuti[1] is None:
        print(f"Fallback attivo: Errore ML")
        #regola fissa di emergenza per entrambi i sistemi
        return {
            "climatizzatore":{"comando":"ACCENDI", "modalita":"STANDARD","timer_minuti":30},
            "sistema_umidita":{"comando":"ACCENDI","modalita":"STANDARD","timer_minuti":15}
        }

    #se tutto va bene, spacchettiamo i due valori
    minuti_clima,minuti_umidificatore=minuti

    #determinazione modalita climatizzatore
    modo_clima="OFF"
    if minuti_clima>0:
        delta_temp=temp_int-target_temp
        if delta_temp>3.0:
            modo_clima="RAFFREDDAMENTO_INTENSIVO"
        elif delta_temp>0:
            modo_clima="RAFFREDDAMENTO_LIEVE"
        elif delta_temp <-3.0:
            modo_clima="RISCALDAMENTO_INTENSIVO"
        elif delta_temp<0:
            modo_clima="RISCALDAMENTO_LIEVE"
        else:
            modo_clima="MANTENIMENTO"

    #determinazione modalita umidita
    modo_umid="OFF"
    if minuti_umidificatore>0:
        delta_umid=umid_int-target_umid
        if delta_umid>10.0:
            modo_umid="DEUMIDIFICAZIONE"
        elif delta_umid<-10.0:
            modo_umid="UMIDIFICAZIONE"
        else:
            modo_umid="MANTENIMENTO"

    #costruiamo il payload MQTT per i due sistemi distinti
    comandi_motori={
        "climatizzatore":{
            "comando":"ACCENDI" if minuti_clima > 0 else "OFF",
            "modalita":modo_clima,
            "timer_minuti":minuti_clima
        },
        "sistema_umidita":{
            "comando":"ACCENDI" if minuti_umidificatore > 0 else "OFF",
            "modalita":modo_umid,
            "timer_minuti":minuti_umidificatore
        }
    }

    return comandi_motori

    
class GestoreAllarmiIntelligente:

    def __init__(self):
        pass

    # ==================================================
    # 🚀 METODO PRINCIPALE
    # ==================================================
    def analizza(self, dati_db):

        if not dati_db:
            return { "errore": "Nessun dato disponibile"}

        ultimo=dati_db[-1]

        timestamp=ultimo[1]
        produttore_attuale=ultimo[2]

        dati_zone, dati_produttori, storico_vino, minuti, storico_sedi = prepara_dati_per_modelli(dati_db)

        report = {
            "timestamp":timestamp,
            "produttore":produttore_attuale,
            "stato_globale":"OK",

            "anomalie": [],
            "qualita_vino": None,
            "stato_sedi": {},
            "azioni_smart": {},

            "priorita":{}
        }

        # 🔍 1. ANOMALIE SENSORI
        anomalie = ml.rileva_anomalie_sensori(dati_zone)
        report["anomalie"] = anomalie

        # 🍷 2. QUALITÀ VINO
        

        stato_vino = gestore_allarme_qualita(
            minuti,
            storico_vino,
            produttore_attuale
        )

        report["qualita_vino"] = stato_vino

        # 🏭 3. CONTROLLO SEDI
        for prod, sedi in dati_produttori.items():
            report["stato_sedi"][prod] = verifica_conformita_sedi(sedi)

        # ⚙️ 4. AZIONI SMART
        

        temp_int = ultimo[4]
        temp_est = ultimo[5]
        umid_int = ultimo[6]
        umid_est = ultimo[7]

            
        profilo = recupera_profilo_produttore(produttore_attuale) or {"target_ambiente_temp":16,"target_ambiente_umid":70}
        config = recupera_dati_sede(produttore_attuale,ultimo[3])

        comandi = avvia_cicli_smart_sistemi(
            temp_est,
            temp_int,
            umid_est,
            umid_int,
            profilo["target_ambiente_temp"],
            profilo["target_ambiente_umid"],
            isolamento=config["isolamento"],
            volume=config["volume"]
        )

        report["azioni_smart"] = comandi

        # 🚨 5. PRIORITÀ
        report["priorita"] = self._calcola_priorita(report)

        report["stato_globale"]=report["priorita"]["priorita"]
        return report

    # ==================================================
    # 🧠 PRIORITÀ INTELLIGENTE (FIX QUI)
    # ==================================================
    def _calcola_priorita(self, report):

        risultato = {
            "priorita": "OK",
            "causa": [],
            "score": 0,
            "dettagli": [],
            "causa_principale":"NESSUNA"
        }

        # 🔍 1. SENSORI
        if report["anomalie"]:
            risultato["score"] += 40
            risultato["causa"].append("SENSORI")

            risultato["dettagli"].append({
                "tipo": "SENSORI",
                "messaggio": f"{len(report['anomalie'])} sensori anomali"
            })

        # 🍷 2. VINO
        if report["qualita_vino"]:

            msg = report["qualita_vino"]

            if "ALLARME_CRITICO" in msg:
                risultato["score"] += 60
                risultato["causa"].append("VINO")

                risultato["dettagli"].append({
                    "tipo": "VINO",
                    "livello": "CRITICO",
                    "messaggio": msg
                })

            elif "PREAVVISO" in msg:
                risultato["score"] += 30
                risultato["causa"].append("VINO")

                risultato["dettagli"].append({
                    "tipo": "VINO",
                    "livello": "ATTENZIONE",
                    "messaggio": msg
                })

        # 🏭 3. AMBIENTE
        problemi_sedi = sum(len(v) for v in report["stato_sedi"].values())

        if problemi_sedi > 0:
            risultato["score"] += 20
            risultato["causa"].append("AMBIENTE")

            risultato["dettagli"].append({
                "tipo": "AMBIENTE",
                "messaggio": f"{problemi_sedi} anomalie ambientali"
            })

        # ⚙️ 4. SISTEMI
        if report["azioni_smart"]:
            for sistema, info in report["azioni_smart"].items():
                if info.get("comando") == "ACCENDI":
                    risultato["score"] += 10

                    risultato["dettagli"].append({
                        "tipo": "SISTEMA",
                        "messaggio": f"{sistema} attivo"
                    })

        # 🧠 5. PRIORITÀ
        score = risultato["score"]

        if score >= 80:
            risultato["priorita"] = "CRITICA"
        elif score >= 50:
            risultato["priorita"] = "ALTA"
        elif score >= 30:
            risultato["priorita"] = "ATTENZIONE"
        elif score > 0:
            risultato["priorita"] = "MEDIA"
        else:
            risultato["priorita"] = "OK"

        # 🎯 6. CAUSA PRINCIPALE
        if not risultato["causa"]:
            risultato["causa_principale"] = "NESSUNA"
        elif len(risultato["causa"]) == 1:
            risultato["causa_principale"] = risultato["causa"][0]
        else:
            risultato["causa_principale"] = "MISTA"

        return risultato