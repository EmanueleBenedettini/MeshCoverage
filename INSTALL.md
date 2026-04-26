# Installazione MeshMonitor

## Requisiti di sistema

- **OS**: Linux (Ubuntu 22.04+ raccomandato), macOS, o Docker
- **Python**: 3.11+
- **RAM**: 4 GB minimi, 8+ GB raccomandati per calcoli DEM su aree estese
- **CPU**: Almeno 4 core raccomandati (il calcolo viewshed è parallelizzato)
- **Disco**: Dipende dai file DEM; tipicamente 1–10 GB per area regionale

---

## Installazione con Docker (raccomandato per produzione)

### 1. Clonare il repository
```bash
git clone https://github.com/your-org/meshmonitor.git
cd MeshMonitor
```

### 2. Configurazione
```bash
cp .env.example .env
nano .env
```

Editare le variabili necessarie (vedere sezione Configurazione).

### 3. Avvio
```bash
docker-compose up -d
```

Controllare i log:
```bash
docker-compose logs -f
```

L'interfaccia web sarà disponibile su `http://localhost:8000`

---

## Installazione manuale (sviluppo / bare-metal)

### 1. Dipendenze di sistema (Ubuntu/Debian)
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

### 2. Ambiente virtuale Python
```bash
python3.11 -m venv venv
source venv/bin/activate
```

### 3. Installazione dipendenze Python
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Generazione codice Protobuf
```bash
chmod +x scripts/generate_proto.sh
./scripts/generate_proto.sh
```

### 5. Configurazione
```bash
cp .env.example .env
nano .env
```

### 6. Creazione directory dati
```bash
mkdir -p data/{nodes,dem,coverage,heatmaps,links}
```

---

## Configurazione (.env)

| Variabile | Default | Descrizione |
|---|---|---|
| `MESHMONITOR_HOST` | `0.0.0.0` | Host del server web |
| `MESHMONITOR_PORT` | `8000` | Porta del server web |
| `MESHMONITOR_DATA_DIR` | `./data` | Directory dati |
| `MQTT_ENABLED` | `false` | Abilita connessione MQTT |
| `MQTT_BROKER` | `mqtt.meshtastic.org` | Broker MQTT |
| `MQTT_PORT` | `1883` | Porta MQTT |
| `MQTT_USERNAME` | `` | Username MQTT (opzionale) |
| `MQTT_PASSWORD` | `` | Password MQTT (opzionale) |
| `MQTT_TOPIC` | `msh/#` | Topic MQTT da sottoscrivere |
| `DIRECT_ENABLED` | `false` | Abilita connessione diretta |
| `DIRECT_HOST` | `localhost` | IP nodo Meshtastic |
| `DIRECT_PORT` | `4403` | Porta nodo Meshtastic |
| `COMPUTE_SCHEDULE` | `0 3 * * *` | Cron per calcolo automatico (3:00 ogni notte) |
| `DEM_DIR` | `./data/dem` | Directory file DEM |
| `DEM_RESOLUTION` | `30` | Risoluzione DEM target in metri |
| `MAX_WORKERS` | `0` | Core CPU per parallelizzazione (0=auto) |
| `LOG_LEVEL` | `INFO` | Livello log (DEBUG/INFO/WARNING/ERROR) |
| `SECRET_KEY` | `changeme` | Chiave segreta per sessioni |

---

## Caricamento file DEM

I file DEM (Digital Elevation Model) in formato **GeoTIFF** vanno copiati nella directory `data/dem/`.

### Fonti DEM consigliate

**Europa (risoluzione 25m):**
- [Copernicus DEM](https://copernicus.eu/en/access-data/copernicus-services/land-dem) - GLO-30 (30m globale, gratuito)
- [EU-DEM](https://www.eea.europa.eu/data-and-maps/data/copernicus-land-monitoring-service-eu-dem) - 25m Europa

**Italia:**
- [TINITALY](http://tinitaly.pi.ingv.it/) - 10m, ottima qualità per il territorio italiano
- [Portale Cartografico Nazionale](http://www.pcn.minambiente.it/) - DTM regionali

**Download esempio con wget (Copernicus GLO-30):**
```bash
# Scaricare tile per l'area di interesse (es. Italia settentrionale)
# I file si chiamano Copernicus_DSM_COG_10_N45_00_E010_00_DEM.tif
wget -P data/dem/ "https://...copernicus-dem.../"
```

### Gestione file DEM multipli
MeshMonitor gestisce automaticamente più file DEM affiancati. Basta copiare tutti i tile necessari nella directory `data/dem/`. Il sistema li indicizza all'avvio e usa il file corretto per ogni posizione geografica.

---

## Avvio servizi

### Con Docker Compose
```bash
# Avvio completo (web server + input service + scheduler)
docker-compose up -d

# Solo web server (senza acquisizione automatica)
docker-compose up -d web

# Ricalcolo manuale immediato
docker-compose exec web python -m meshmonitor.processing.coverage_calculator --all
```

### Manuale
```bash
source venv/bin/activate

# Web server + API (porta 8000)
uvicorn meshmonitor.api.app:app --host 0.0.0.0 --port 8000 --reload

# Servizio acquisizione dati (in background)
python -m meshmonitor.input.service &

# Scheduler (calcolo periodico)
python scripts/scheduler.py &

# Calcolo manuale immediato
python -m meshmonitor.processing.coverage_calculator --all
# oppure per un nodo specifico
python -m meshmonitor.processing.coverage_calculator --node !aabbccdd
```

---

## Aggiornamento

```bash
git pull origin main
pip install -r requirements.txt  # aggiorna dipendenze
./scripts/generate_proto.sh       # rigenera protobuf se modificati
docker-compose restart            # riavvia servizi
```

---

## Troubleshooting

**Errore "No DEM data for coordinates":**
- Verificare che i file DEM coprano l'area dei nodi
- Controllare che i file siano in formato GeoTIFF (`.tif`)

**Calcolo lento:**
- Aumentare `MAX_WORKERS` nel `.env`
- Ridurre la risoluzione DEM (`DEM_RESOLUTION=60` per calcoli più veloci)
- Limitare la distanza massima (`MAX_RANGE_KM` nella config del nodo)

**Connessione MQTT fallisce:**
- Verificare firewall su porta 1883
- Controllare credenziali nel `.env`
- Testare con `mosquitto_sub -h mqtt.meshtastic.org -t "msh/#"`
