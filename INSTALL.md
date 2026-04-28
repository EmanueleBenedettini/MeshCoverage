# MeshCoverage Installation

## System Requirements

- **OS**: Linux (Ubuntu 22.04+ recommended), macOS, or Docker
- **Python**: 3.11+
- **RAM**: 4 GB minimum, 8+ GB recommended for DEM calculations over large areas
- **CPU**: At least 4 cores recommended (viewshed calculation is parallelised)
- **Disk**: Depends on DEM files; typically 1–10 GB for regional areas

---

## Installation with Docker (recommended for production)

### 1. Clone the repository
```bash
git clone https://github.com/EmanueleBenedettini/MeshCoverage.git
cd MeshCoverage
```

### 2. Configuration
```bash
cp .env.example .env
nano .env
```

Edit the necessary variables (see Configuration section).

### 3. Start
```bash
docker-compose up -d
```

Check the logs:
```bash
docker-compose logs -f
```

The web interface will be available at `http://localhost:8000`

---

## Manual Installation (development / bare-metal)

### 1. System dependencies (Ubuntu/Debian)
```bash
sudo apt-get update
sudo apt-get install -y \
    python3.11 python3.11-venv python3.11-dev \
    libgdal-dev gdal-bin \
    libproj-dev \
    libgeos-dev \
    protobuf-compiler \
    git curl
```

### 2. Python virtual environment
```bash
python3.11 -m venv venv
source venv/bin/activate
```

### 3. Install Python dependencies
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Generate Protobuf code
```bash
chmod +x scripts/generate_proto.sh
./scripts/generate_proto.sh
```

### 5. Configuration
```bash
cp .env.example .env
nano .env
```

### 6. Create data directories
```bash
mkdir -p data/{nodes,dem,coverage,heatmaps,links}
```

---

## Configuration (.env)

| Variable | Default | Description |
|---|---|---|
| `MESHCOVERAGE_HOST` | `0.0.0.0` | Web server host |
| `MESHCOVERAGE_PORT` | `8000` | Web server port |
| `MESHCOVERAGE_DATA_DIR` | `./data` | Data directory |
| `MQTT_ENABLED` | `false` | Enable MQTT connection |
| `MQTT_BROKER` | `mqtt.meshtastic.org` | MQTT broker |
| `MQTT_PORT` | `1883` | MQTT port |
| `MQTT_USERNAME` | `` | MQTT username (optional) |
| `MQTT_PASSWORD` | `` | MQTT password (optional) |
| `MQTT_TOPIC` | `msh/#` | MQTT topic to subscribe to |
| `DIRECT_ENABLED` | `false` | Enable direct connection |
| `DIRECT_HOST` | `localhost` | Meshtastic node IP |
| `DIRECT_PORT` | `4403` | Meshtastic node port |
| `COMPUTE_SCHEDULE` | `0 3 * * *` | Cron for automatic calculation (3:00 every night) |
| `DEM_DIR` | `./data/dem` | DEM file directory |
| `DEM_RESOLUTION` | `30` | Target DEM resolution in metres |
| `MAX_WORKERS` | `0` | CPU cores for parallelisation (0=auto) |
| `LOG_LEVEL` | `INFO` | Log level (DEBUG/INFO/WARNING/ERROR) |
| `SECRET_KEY` | `changeme` | Secret key for sessions |

---

## Loading DEM Files

DEM (Digital Elevation Model) files in **GeoTIFF** format should be copied to the `data/dem/` directory.

### Recommended DEM sources

**Europe (25m resolution):**
- [Copernicus DEM](https://copernicus.eu/en/access-data/copernicus-services/land-dem) - GLO-30 (30m global, free)
- [EU-DEM](https://www.eea.europa.eu/data-and-maps/data/copernicus-land-monitoring-service-eu-dem) - 25m Europe

**Italy:**
- [TINITALY](http://tinitaly.pi.ingv.it/) - 10m, excellent quality for Italian territory
- [National Cartographic Portal](http://www.pcn.minambiente.it/) - Regional DTM

**Example download with wget (Copernicus GLO-30):**
```bash
# Download tile for your area of interest (e.g. northern Italy)
# Files are named Copernicus_DSM_COG_10_N45_00_E010_00_DEM.tif
wget -P data/dem/ "https://...copernicus-dem.../"
```

### Multiple DEM file management
MeshCoverage automatically handles multiple adjacent DEM files. Simply copy all necessary tiles to the `data/dem/` directory. The system indexes them on startup and uses the correct file for each geographic location.

---

## Starting Services

### With Docker Compose
```bash
# Full startup (web server + input service + scheduler)
docker-compose up -d

# Web server only (without automatic data acquisition)
docker-compose up -d web

# Immediate manual recalculation
docker-compose exec web python -m src.processing.coverage_calculator --all
```

### Manual
```bash
# Use the helper script to create/activate venv, install requirements and start the web server
bash scripts/start_web.sh
# Or, to use a different uvicorn command:
# bash scripts/start_web.sh uvicorn src.api.app:app --host 0.0.0.0 --port 8000
```
Other useful commands:
```bash
source venv/bin/activate

# Web server + API (port 8000)
uvicorn src.api.app:app --host 0.0.0.0 --port 8000 --reload

# Data acquisition service (in background)
python -m src.input.service &

# Scheduler (periodic calculation)
python scripts/scheduler.py &

# Immediate manual calculation
python -m src.processing.coverage_calculator --all
# or for a specific node
python -m src.processing.coverage_calculator --node !aabbccdd
```

---

## Update

```bash
git pull origin main
pip install -r requirements.txt  # update dependencies
./scripts/generate_proto.sh       # regenerate protobuf if modified
docker-compose restart            # restart services
```

---

## Troubleshooting

**Error "No DEM data for coordinates":**
- Verify that DEM files cover the area of your nodes
- Check that files are in GeoTIFF format (`.tif`)

**Slow calculation:**
- Increase `MAX_WORKERS` in `.env`
- Reduce DEM resolution (`DEM_RESOLUTION=60` for faster calculations)
- Limit maximum distance (`MAX_RANGE_KM` in node configuration)

**MQTT connection fails:**
- Check firewall on port 1883
- Verify credentials in `.env`
- Test with `mosquitto_sub -h mqtt.meshtastic.org -t "msh/#"`
