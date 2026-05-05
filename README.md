# MeshCoverage
<img width="834" height="655" alt="Immagine 2026-05-04 222825" src="https://github.com/user-attachments/assets/012adbca-4dcc-4f95-a58b-5631f1e465cd" />

**MeshCoverage** is a complete system for calculating and visualising radio coverage areas of a Meshtastic antenna mesh. It collects data from nodes via MQTT or direct connection, calculates coverage using DEM (Digital Elevation Model) data with visibility analysis, Fresnel zone and link budget calculations, and presents results on an interactive map.

---

## Architecture

```
MeshCoverage/
├── proto/                   # Protobuf definitions
├── scripts/                 # Utility and scheduling scripts
├── src/
│   ├── config.py            # Centralised configuration
│   ├── database.py          # Node JSON database management
│   ├── models/              # Data models
│   ├── input/               # Meshtastic data acquisition service
│   ├── processing/          # Coverage calculation pipeline
│   ├── api/                 # FastAPI REST API
│   └── web/                 # Web frontend (Leaflet.js)
└── data/
    ├── nodes/               # Node database (JSON)
    ├── dem/                 # DEM files (GeoTIFF) - managed by admin
    ├── coverage/            # Coverage results per node (.npz)
    ├── heatmaps/            # GeoJSON heatmaps by frequency/preset
    └── links/               # Inter-node connections (JSON)
```

## Main Components

### 1. INPUT — Meshtastic Data Acquisition
Service that connects to the Meshtastic network via:
- **MQTT**: configurable broker (e.g. `mqtt.meshtastic.org`)
- **Direct TCP**: connection to gateway node via IP:port (default 4403)

Parses received packets and updates the JSON node database with:
- Identifier (`!aabbccdd`), short/long name, GPS position, altitude
- Frequency (433/868/915 MHz), modem preset
- Antenna parameters (optional, manually entered)

### 2. PROCESSING — Coverage Calculation
Executed periodically (default: every 24h), calculates for each complete node:
- **ERP** (Effective Radiated Power) with warning if >+27 dBm
- **Link budget** and maximum distance for each modem preset
- **Viewshed** (visibility analysis) on DEM data, parallelised across all cores
- **Fresnel zone** for each visible point, with receiver at 1.5m
- **GeoJSON heatmaps** aggregated by frequency and modem preset
- **Connection map** between nodes in direct line of sight

### 3. VISUALISATION — Web UI + REST API
Web interface on OpenStreetMap with:
- Frequency and modem preset selection
- Aggregated coverage heatmap
- Direct connection lines between nodes
- Node detail panel with radiation diagram
- Manual computation start (global or per node)
- Node database editor

REST API automatically documented at `/api/docs`

---

## Quickstart

See [INSTALL.md](INSTALL.md) for complete instructions.

```bash
git clone https://github.com/EmanueleBenedettini/meshcoverage.git
cd MeshCoverage
cp .env.example .env
# Edit .env with your configuration
docker-compose up -d
```

The web interface will be available at `http://localhost:8000`

---

## Data Formats

### Node (nodes/nodes.json)
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
GeoJSON FeatureCollection with `link_budget_dbm` property for each point.

### Connections (links/links_868_MEDIUM_FAST.json)
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

## Licence
GPL-3.0 license 
