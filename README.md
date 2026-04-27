# MeshMonitor

**MeshMonitor** è un sistema completo per il calcolo e la visualizzazione delle aree di copertura radio di una mesh di antenne Meshtastic. Raccoglie dati dai nodi via MQTT o connessione diretta, calcola la copertura usando dati DEM (Digital Elevation Model) con analisi di visibilità, zona di Fresnel e link budget, e presenta i risultati su mappa interattiva.

---

## Architettura

```
MeshMonitor/
├── proto/                   # Definizioni Protobuf
├── scripts/                 # Script di utilità e scheduling
├── src/
│   ├── config.py            # Configurazione centralizzata
│   ├── database.py          # Gestione database nodi JSON
│   ├── models/              # Modelli dati
│   ├── input/               # Servizio acquisizione dati Meshtastic
│   ├── processing/          # Pipeline di calcolo copertura
│   ├── api/                 # API REST FastAPI
│   └── web/                 # Frontend web (Leaflet.js)
└── data/
    ├── nodes/               # Database nodi (JSON)
    ├── dem/                 # File DEM (GeoTIFF) - gestiti dall'admin
    ├── coverage/            # Risultati copertura per nodo (.npz)
    ├── heatmaps/            # Heatmap GeoJSON per freq/preset
    └── links/               # Connessioni inter-nodo (JSON)
```

## Componenti principali

### 1. INPUT — Acquisizione Dati Meshtastic
Servizio che si connette alla rete Meshtastic tramite:
- **MQTT**: broker configurabile (es. `mqtt.meshtastic.org`)
- **TCP diretto**: connessione al nodo gateway via IP:porta (default 4403)

Analizza i pacchetti ricevuti e aggiorna il database nodi JSON con:
- Identificativo (`!aabbccdd`), nome breve/lungo, posizione GPS, altezza
- Frequenza (433/868/915 MHz), modem preset
- Parametri antenna (opzionali, inseriti manualmente)

### 2. PROCESSING — Calcolo Copertura
Eseguito periodicamente (default: ogni 24h), calcola per ogni nodo completo:
- **ERP** (Effective Radiated Power) con warning se >+27 dBm
- **Link budget** e distanza massima per ogni modem preset
- **Viewshed** (analisi di visibilità) su dati DEM, parallelizzata su tutti i core
- **Zona di Fresnel** per ogni punto visibile, con ricevitore a 1.5m
- **Heatmap GeoJSON** aggregate per frequenza e modem preset
- **Mappa connessioni** tra nodi in visibilità diretta

### 3. VISUALIZZAZIONE — Web UI + API REST
Interfaccia web su OpenStreetMap con:
- Selezione frequenza e modem preset
- Heatmap di copertura aggregata
- Linee di connessione diretta tra nodi
- Pannello dettaglio nodo con diagramma di radiazione
- Avvio manuale computazione (globale o per singolo nodo)
- Editor database nodi

API REST documentata automaticamente su `/api/docs`

---

## Quickstart

Vedi [INSTALL.md](INSTALL.md) per le istruzioni complete.

```bash
git clone https://github.com/your-org/meshmonitor.git
cd MeshMonitor
cp .env.example .env
# Editare .env con la propria configurazione
docker-compose up -d
```

L'interfaccia web sarà disponibile su `http://localhost:8000`

---

## Formati dati

### Nodo (nodes/nodes.json)
```json
{
  "!aabbccdd": {
    "id": "!aabbccdd",
    "short_name": "NODE",
    "long_name": "My Node",
    "hardware_model": "TBEAM",
    "position": {"lat": 45.123, "lon": 9.456},
    "altitude": 250.0,
    "frequency": 868,
    "modem_preset": "MEDIUM_FAST",
    "last_seen": "2024-01-01T12:00:00Z",
    "antenna": {
      "tx_power_dbm": 27,
      "type": "dipole",
      "gain_dbi": 2.15,
      "azimuth_deg": 0,
      "beamwidth_deg": 360,
      "gain_min_dbi": 2.15,
      "gain_max_dbi": 2.15
    }
  }
}
```

### Heatmap (heatmaps/heatmap_868_MEDIUM_FAST.geojson)
GeoJSON FeatureCollection con proprietà `link_budget_dbm` per ogni punto.

### Connessioni (links/links_868_MEDIUM_FAST.json)
```json
[
  {
    "node_a": "!aabbccdd",
    "node_b": "!11223344",
    "distance_km": 12.5,
    "link_budget_dbm": 15.3,
    "los": true
  }
]
```

---

## Licenza
MIT License — vedi [LICENSE](LICENSE)
