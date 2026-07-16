#include <ESP8266WiFi.h>
#include <PubSubClient.h>
#include "DHT.h"

// =======================
// WIFI
// =======================
const char* ssid = "wi10973";
const char* password = "k8z422vc";

// =======================
// MQTT
// IMPORTANTE: usa lo stesso IP anche nell'ESP32 e nel server Python.
// =======================
const char* mqtt_server = "192.168.15.57";
const int mqtt_port = 1883;

WiFiClient espClient;
PubSubClient client(espClient);

// =======================
// TOPIC MQTT
// =======================
const char* TOPIC_DATI_ESTERNI = "cantine/urbani/pievepelago/sensori";

// =======================
// DHT11 ESTERNO
// Su ESP8266 NodeMCU: D2 corrisponde a GPIO4.
// =======================
#define DHTPIN D2
#define DHTTYPE DHT11
DHT dht(DHTPIN, DHTTYPE);

// =======================
// TIMER LETTURE
// 10000 = 10 secondi per le prove
// 900000 = 15 minuti
// =======================
unsigned long ultimoInvio = 0;
const unsigned long intervalloInvio = 10000;

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
  Serial.print("IP ESP8266: ");
  Serial.println(WiFi.localIP());
}

// =======================
// RICONNESSIONE MQTT
// =======================
void reconnect() {
  while (!client.connected()) {
    Serial.print("Connessione MQTT...");

    if (client.connect("ESP8266_ESTERNO_PIEVEPELAGO")) {
      Serial.println("connesso");
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

  setup_wifi();

  client.setServer(mqtt_server, mqtt_port);
  client.setBufferSize(256);

  Serial.println("Sistema esterno ESP8266 avviato");
  Serial.println("Temperatura e umidita esterne lette realmente dal DHT11");
}

// =======================
// LOOP
// =======================
void loop() {
  if (!client.connected()) {
    reconnect();
  }

  client.loop();

  if (ultimoInvio == 0 || millis() - ultimoInvio >= intervalloInvio) {
    ultimoInvio = millis();

    Serial.println("\n----- SENSORI ESTERNI REALI -----");

    // Questi valori arrivano direttamente dal DHT11 esterno.
    float temp_est = dht.readTemperature();
    float umid_est = dht.readHumidity();

    char payloadDati[180];

    if (isnan(temp_est) || isnan(umid_est)) {
      Serial.println("Errore di lettura dal DHT11 esterno");

      snprintf(
        payloadDati,
        sizeof(payloadDati),
        "{\"temp_est\":null,\"umid_est\":null,\"stato_dht\":\"ERRORE_DHT11\"}"
      );
    } else {
      Serial.print("Temperatura esterna reale: ");
      Serial.print(temp_est);
      Serial.println(" C");

      Serial.print("Umidita esterna reale: ");
      Serial.print(umid_est);
      Serial.println(" %");

      snprintf(
        payloadDati,
        sizeof(payloadDati),
        "{\"temp_est\":%.2f,\"umid_est\":%.2f,\"stato_dht\":\"OK\"}",
        temp_est,
        umid_est
      );
    }

    Serial.print("Pubblicazione dati esterni reali: ");
    Serial.println(payloadDati);

    if (!client.publish(TOPIC_DATI_ESTERNI, payloadDati)) {
      Serial.println("Pubblicazione MQTT fallita");
    }
  }
}
