#include <WiFi.h>
#include <PubSubClient.h>
#include "DHT.h"

// =======================
// WIFI
// =======================
const char* ssid = "wi10973";
const char* password = "k8z422vc";

// =======================
// MQTT
// =======================
const char* mqtt_server = "192.168.15.57";
const int mqtt_port = 1883;

WiFiClient espClient;
PubSubClient client(espClient);

// =======================
// TOPIC MQTT
// =======================
const char* TOPIC_DATI_INTERNI = "cantine/urbani/pievepelago/sensori";
const char* TOPIC_COMANDI = "cantine/urbani/pievepelago/comandi";

// =======================
// DHT11 INTERNO
// =======================
#define DHTPIN 18
#define DHTTYPE DHT11
DHT dht(DHTPIN, DHTTYPE);

// =======================
// MQ-135
// =======================
#define CO2_PIN 34

// =======================
// BUZZER E LED
// =======================
#define BUZZER 19
#define LED_TEMPERATURA 25
#define LED_UMIDITA 26
#define LED_CO2 27

// =======================
// TIMER LETTURE
// 10000 = 10 secondi per le prove
// 900000 = 15 minuti
// =======================
unsigned long ultimoInvio = 0;
const unsigned long intervalloInvio = 10000;

// Spegne tutti gli attuatori all'avvio.
void spegniAttuatori() {
  digitalWrite(BUZZER, LOW);
  digitalWrite(LED_TEMPERATURA, LOW);
  digitalWrite(LED_UMIDITA, LOW);
  digitalWrite(LED_CO2, LOW);
}

// =======================
// CONNESSIONE WIFI
// =======================
void setup_wifi() {
  Serial.println();
  Serial.print("Connessione al WiFi: ");
  Serial.println(ssid);

  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println("\nWiFi connesso");
  Serial.print("IP ESP32: ");
  Serial.println(WiFi.localIP());
}

// =======================
// CALLBACK MQTT
// Il server invia un messaggio del tipo:
// TEMP=1;UMID=0;CO2=1;BUZZER=0
// 1 = acceso, 0 = spento
// =======================
void callback(char* topic, byte* payload, unsigned int length) {
  if (strcmp(topic, TOPIC_COMANDI) != 0) {
    return;
  }

  // Limite di sicurezza per il buffer del messaggio.
  if (length >= 80) {
    Serial.println("Comando MQTT troppo lungo: ignorato");
    return;
  }

  char messaggio[80];
  memcpy(messaggio, payload, length);
  messaggio[length] = '\0';

  Serial.print("Comando ricevuto dal server: ");
  Serial.println(messaggio);

  int statoTemperatura;
  int statoUmidita;
  int statoCO2;
  int statoBuzzer;

  int campiLetti = sscanf(
    messaggio,
    "TEMP=%d;UMID=%d;CO2=%d;BUZZER=%d",
    &statoTemperatura,
    &statoUmidita,
    &statoCO2,
    &statoBuzzer
  );

  if (campiLetti != 4) {
    Serial.println("Formato del comando non valido: comando ignorato");
    return;
  }

  // Accetta soltanto valori 0 oppure 1.
  if ((statoTemperatura != 0 && statoTemperatura != 1) ||
      (statoUmidita != 0 && statoUmidita != 1) ||
      (statoCO2 != 0 && statoCO2 != 1) ||
      (statoBuzzer != 0 && statoBuzzer != 1)) {
    Serial.println("Stati non validi: devono essere 0 oppure 1");
    return;
  }

  // L'ESP32 non esegue piu i calcoli: applica la decisione del server.
  digitalWrite(LED_TEMPERATURA, statoTemperatura ? HIGH : LOW);
  digitalWrite(LED_UMIDITA, statoUmidita ? HIGH : LOW);
  digitalWrite(LED_CO2, statoCO2 ? HIGH : LOW);
  digitalWrite(BUZZER, statoBuzzer ? HIGH : LOW);

  Serial.print("LED temperatura: ");
  Serial.println(statoTemperatura ? "ACCESO" : "SPENTO");
  Serial.print("LED umidita: ");
  Serial.println(statoUmidita ? "ACCESO" : "SPENTO");
  Serial.print("LED CO2: ");
  Serial.println(statoCO2 ? "ACCESO" : "SPENTO");
  Serial.print("Buzzer: ");
  Serial.println(statoBuzzer ? "ACCESO" : "SPENTO");
}

// =======================
// RICONNESSIONE MQTT
// =======================
void reconnect() {
  while (!client.connected()) {
    Serial.print("Connessione MQTT...");

    if (client.connect("ESP32_INTERNO_PIEVEPELAGO")) {
      Serial.println("connesso");

      if (client.subscribe(TOPIC_COMANDI)) {
        Serial.print("Sottoscritto al topic: ");
        Serial.println(TOPIC_COMANDI);
      } else {
        Serial.println("Errore nella sottoscrizione al topic comandi");
      }
    } else {
      Serial.print("fallita, rc=");
      Serial.print(client.state());
      Serial.println("; nuovo tentativo tra 5 secondi");
      delay(5000);
    }
  }
}

// =======================
// SETUP
// =======================
void setup() {
  Serial.begin(115200);
  dht.begin();

  pinMode(BUZZER, OUTPUT);
  pinMode(LED_TEMPERATURA, OUTPUT);
  pinMode(LED_UMIDITA, OUTPUT);
  pinMode(LED_CO2, OUTPUT);

  spegniAttuatori();
  setup_wifi();

  client.setServer(mqtt_server, mqtt_port);
  client.setCallback(callback);
  client.setBufferSize(256);

  Serial.println("Sistema interno avviato");
  Serial.println("I LED sono controllati esclusivamente dal server MQTT");
}

// =======================
// LOOP
// =======================
void loop() {
  if (!client.connected()) {
    reconnect();
  }

  // Necessario per ricevere continuamente i comandi MQTT.
  client.loop();

  if (ultimoInvio == 0 || millis() - ultimoInvio >= intervalloInvio) {
    ultimoInvio = millis();

    Serial.println("\n----- SENSORI INTERNI -----");

    float temp_int = dht.readTemperature();
    float umid_int = dht.readHumidity();
    int valoreGrezzoMQ = analogRead(CO2_PIN);

    if (isnan(temp_int) || isnan(umid_int)) {
      Serial.println("Errore di lettura dal DHT11 interno");
      return;
    }

    // Conversione approssimativa mantenuta dal codice originale.
    int co2_ppm = map(valoreGrezzoMQ, 0, 4095, 400, 4000);
    if (co2_ppm < 400) {
      co2_ppm = 400;
    }

    Serial.print("Temperatura interna reale: ");
    Serial.print(temp_int);
    Serial.println(" C");

    Serial.print("Umidita interna reale: ");
    Serial.print(umid_int);
    Serial.println(" %");

    Serial.print("CO2 stimata: ");
    Serial.print(co2_ppm);
    Serial.println(" ppm");

    // Non vengono piu creati temp_est e umid_est simulati.
    // I dati esterni reali vengono pubblicati esclusivamente dall'ESP8266.
    char payloadDati[180];
    snprintf(
      payloadDati,
      sizeof(payloadDati),
      "{\"temp_int\":%.2f,\"umid_int\":%.2f,\"co2\":%d,\"stato_dht\":\"OK\"}",
      temp_int,
      umid_int,
      co2_ppm
    );

    Serial.print("Pubblicazione dati interni: ");
    Serial.println(payloadDati);

    if (!client.publish(TOPIC_DATI_INTERNI, payloadDati)) {
      Serial.println("Pubblicazione MQTT fallita");
    }
  }
}
