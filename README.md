# MeshCoverage
<img width="834" height="655" alt="Immagine 2026-05-04 222825" src="https://github.com/user-attachments/assets/012adbca-4dcc-4f95-a58b-5631f1e465cd" />

**MeshCoverage** is a complete system for calculating and visualising radio coverage areas of a Meshtastic antenna mesh. It collects data from nodes via MQTT or direct connection, calculates coverage using DEM (Digital Elevation Model) data with visibility analysis, Fresnel zone and link budget calculations, and presents results on an interactive map.


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
git clone https://github.com/your-org/meshcoverage.git
cd MeshCoverage
cp .env.example .env
# Edit .env with your configuration
docker-compose up -d
```

The web interface will be available at `http://localhost:8000`



---
## Licence
GPL-3.0 license 
