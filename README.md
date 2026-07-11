# Smart_Cantine
Progetto per il corso di IoT and 3D Intelligent Systems


🍷 IoT Edge Computing System - Cantine Fratelli Urbani

![Status](https://img.shields.io/badge/Status-Active-brightgreen)
![Hardware](https://img.shields.io/badge/Hardware-ESP32%20%7C%20ESP8266-orange)
![Backend](https://img.shields.io/badge/Backend-Python%20Flask-blue)
![Protocol](https://img.shields.io/badge/Protocol-MQTT-lightgrey)

Sistema IoT proprietario basato su architettura Edge Computing per il monitoraggio ambientale e la telemetria predittiva della cantina (Sede: Pievepelago/Carpi/Vignola). Il sistema raccoglie dati termici e di qualità dell'aria, li sincronizza tramite un broker MQTT locale e li storicizza su un database SQLite, ponendo le basi per futuri modelli di Machine Learning.

---

## 📑 Indice
- [Descrizione del Progetto](#-descrizione-del-progetto)
- [Architettura di Sistema](#️-architettura-di-sistema)
- [Hardware Utilizzato](#-hardware-utilizzato)
- [Struttura dei Dati & Logica di Sincronizzazione](#-struttura-dei-dati--logica-di-sincronizzazione)
- [Installazione e Avvio Rapido](#-installazione-e-avvio-rapido)

---

## 🔬 Descrizione del Progetto

La conservazione ottimale del vino richiede un controllo rigoroso di temperatura, umidità e livelli di anidride carbonica (CO2). Questo progetto supera i tradizionali sistemi "dumb" introducendo una rete di microcontrollori che comunicano via Wi-Fi con un server locale (Edge Server). 
Il sistema non si limita a leggere i dati ambientali, ma calcola attivamente la **temperatura proiettata del vino** e gestisce la logica degli allarmi (es. saturazione CO2, surriscaldamento) in completa autonomia dalla rete internet esterna.

---

## 🏗️ Architettura di Sistema

Il progetto si divide in tre macro-componenti:

1. **Rete Sensoriale (Edge Nodes):**
   * **Nodo Interno (ESP32):** Monitora l'ambiente interno della cantina (Temp/Umidità), analizza i gas nell'aria tramite sensore MQ-135 e gestisce attuatori locali (LED di stato e Buzzer di allarme).
   * **Nodo Esterno (ESP8266):** Funge da stazione meteorologica esterna per confrontare il delta termico tra interno ed esterno.

2. **Livello di Trasporto (MQTT Broker):**
   * Un server Eclipse Mosquitto locale gestisce la coda dei messaggi in tempo reale sul protocollo TCP/IP (Porta 1883) sui topic `cantine/#`.

3. **Edge Server & Data Aggregation (Python/Flask):**
   * Un server Python agisce da sottoscrittore (Subscriber). Riceve i dati frammentati dai vari nodi, li attende in un buffer di stato (Sala d'Attesa) e li unisce in un singolo record sincronizzato all'interno del database.

---

## 💻 Hardware Utilizzato

* **Microcontrollori:** NodeMCU ESP32 (Core), NodeMCU V3 ESP8266 (Esterno)
* **Sensori Termici:** 2x DHT11 (Temperatura e Umidità)
* **Sensori Gas:** 1x MQ-135 (Monitoraggio Qualità dell'Aria / CO2)
* **Attuatori:** Buzzer Attivo 5V, LED di segnalazione
* **Edge Server:** PC/Server Windows locale con Python 3.x e Mosquitto

---

## 📊 Struttura dei Dati & Logica di Sincronizzazione

Per garantire set di dati puliti per l'addestramento di futuri modelli di Machine Learning, il server Python applica una logica di **Double-Check Synchronization**:
I nodi inviano pacchetti JSON asincroni con frequenza oraria. Il server salva in memoria cache il primo pacchetto in arrivo (es. dati interni) e sospende l'operazione di scrittura sul DB finché non riceve la controparte (dati esterni). 

Una riga del Database `DatoSensore` viene registrata solo quando è completa:
* `timestamp`: Generato automaticamente dal server (UTC)
* `produttore` / `sede`: Routing dinamico tramite Topic MQTT
* `temp_int` / `umid_int`: Dati interni
* `temp_est` / `umid_est`: Dati esterni
* `co2` / `allarme_co2`: Dati qualità aria
* `temp_vino_proiettata`: Elaborata via software dal server locale.

---

## 🚀 Installazione e Avvio Rapido

### 1. Setup del Broker MQTT (Mosquitto)

1. Installare **Mosquitto** sul server locale.
2. Modificare il file `mosquitto.conf` inserendo:

```text
listener 1883
allow_anonymous true
```

3. Disabilitare il firewall per le reti private sul server locale.
4. Avviare il servizio Mosquitto.

---

### 2. Flash dei Microcontrollori

1. Aprire gli sketch `.ino` tramite **Arduino IDE**.
2. Assicurarsi di avere installato le seguenti librerie:
   - `PubSubClient` di Nick O'Leary
   - `DHT sensor library` di Adafruit

3. Modificare l'indirizzo IP nei file di configurazione impostando il puntamento verso l'Edge Server locale:

```cpp
const char* mqtt_server = "INSERIRE_IP_SERVER_LOCALE";
```

4. Caricare il codice sui dispositivi **ESP32** e **ESP8266**.

---

### 3. Avvio dell'Edge Server (Python)

Creare un ambiente virtuale (opzionale ma consigliato) e installare le dipendenze:

```bash
pip install flask flask_sqlalchemy flask_login paho-mqtt werkzeug
```

Avviare il server d'ascolto e la dashboard web:

```bash
python server.py
```

---

## 📌 Note

- Verificare che tutti i dispositivi siano connessi alla stessa rete locale.
- Assicurarsi che l'IP configurato nei microcontrollori corrisponda a quello del server che ospita il broker MQTT.

---

*Progetto sviluppato per Cantine Fratelli Urbani.*

  listener 1883
  allow_anonymous true
