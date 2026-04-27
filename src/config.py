"""
Configurazione centralizzata MeshCoverage.
Tutte le impostazioni sono lette da variabili d'ambiente o file .env.
"""
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MESHCOVERAGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server web
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)
    secret_key: str = Field(default="changeme-please-set-in-env")
    log_level: str = Field(default="INFO")

    # Directory dati
    data_dir: Path = Field(default=Path("./data"))

    # MQTT
    mqtt_enabled: bool = Field(default=False)
    mqtt_broker: str = Field(default="mqtt.meshtastic.org")
    mqtt_port: int = Field(default=1883)
    mqtt_username: Optional[str] = Field(default=None)
    mqtt_password: Optional[str] = Field(default=None)
    mqtt_topic: str = Field(default="msh/#")
    mqtt_tls: bool = Field(default=False)

    # Connessione diretta Meshtastic
    direct_enabled: bool = Field(default=False)
    direct_host: str = Field(default="localhost")
    direct_port: int = Field(default=4403)

    # Scheduling
    compute_schedule: str = Field(default="0 3 * * *")

    # DEM
    dem_dir: Path = Field(default=Path("./data/dem"))
    dem_resolution: int = Field(default=30)   # metri

    # Calcolo copertura
    max_workers: int = Field(default=0)       # 0 = auto (tutti i core)
    max_range_km: float = Field(default=50.0) # Distanza massima analisi
    receiver_height_m: float = Field(default=1.5)   # Altezza ricevitore
    receiver_gain_dbi: float = Field(default=2.15)  # Guadagno antenna ricevente
    min_link_budget_db: float = Field(default=0.0)  # Margine minimo per "copertura"
    erp_warning_dbm: float = Field(default=27.0)    # Soglia ERP warning

    # Heatmap
    heatmap_resolution_m: float = Field(default=100.0)  # risoluzione grid heatmap

    @property
    def nodes_file(self) -> Path:
        return self.data_dir / "nodes" / "nodes.json"

    @property
    def coverage_dir(self) -> Path:
        return self.data_dir / "coverage"

    @property
    def heatmaps_dir(self) -> Path:
        return self.data_dir / "heatmaps"

    @property
    def links_dir(self) -> Path:
        return self.data_dir / "links"

    def ensure_dirs(self):
        """Crea tutte le directory necessarie se non esistono."""
        for d in [
            self.data_dir / "nodes",
            self.coverage_dir,
            self.heatmaps_dir,
            self.links_dir,
            self.dem_dir,
        ]:
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
