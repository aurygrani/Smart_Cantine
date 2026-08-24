# Configurazione produttori

PROFILI_PRODUTTORI = {

    "rossi": {
        "tipo_vino": "Rosso",
        "target_ambiente_temp": 18,
        "target_ambiente_umid": 65,
        "target_vino_temp":16,
        "tolleranza_temp_ambiente": 2,
        "tolleranza_umid_ambiente": 5,
        "tolleranza_vino_temp":1.5
    },

    "bianchi": {
        "tipo_vino": "Bianco",
        "target_ambiente_temp": 12,
        "target_ambiente_umid": 60,
        "target_vino_temp": 10,
        "tolleranza_temp_ambiente": 2,
        "tolleranza_umid_ambiente": 5,
        "tolleranza_vino_temp":1.5
    },

    "urbani": {
        "tipo_vino": "Rosè",
        "target_ambiente_temp": 14,
        "target_ambiente_umid": 62,
        "target_vino_temp":12,
        "tolleranza_temp_ambiente": 2,
        "tolleranza_umid_ambiente": 5,
        "tolleranza_vino_temp":1.5
    }

}

def get_profilo_produttore(produttore):

    """
    Restituisce il profilo configurato
    per un produttore
    """

    return PROFILI_PRODUTTORI.get(produttore)