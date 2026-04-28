"""
Centralised MeshCoverage configuration.
All settings are read from environment variables or .env file.
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

    # Web server
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)
    secret_key: str = Field(default="changeme-please-set-in-env")
    log_level: str = Field(default="INFO")

    # Data directories
    data_dir: Path = Field(default=Path("./data"))

    # MQTT
    mqtt_enabled: bool = Field(default=False)
    mqtt_broker: str = Field(default="mqtt.meshtastic.org")
    mqtt_port: int = Field(default=1883)
    mqtt_username: Optional[str] = Field(default=None)
    mqtt_password: Optional[str] = Field(default=None)
    mqtt_topic: str = Field(default="msh/#")
    mqtt_tls: bool = Field(default=False)

    # Direct Meshtastic connection
    direct_enabled: bool = Field(default=False)
    direct_host: str = Field(default="localhost")
    direct_port: int = Field(default=4403)

    # Scheduling
    compute_schedule: str = Field(default="0 3 * * *")

    # DEM
    dem_dir: Path = Field(default=Path("./data/dem"))
    dem_resolution: int = Field(default=30)   # metres

    # Coverage calculation
    max_workers: int = Field(default=0)       # 0 = auto (all cores)
    max_range_km: float = Field(default=50.0) # Maximum analysis distance
    receiver_height_m: float = Field(default=1.5)   # Receiver height
    receiver_gain_dbi: float = Field(default=2.15)  # Receiver antenna gain
    min_link_budget_db: float = Field(default=0.0)  # Minimum margin for "coverage"
    erp_warning_dbm: float = Field(default=27.0)    # ERP warning threshold

    # Heatmap
    heatmap_resolution_m: float = Field(default=100.0)  # heatmap grid resolution

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
        """Creates all necessary directories if they do not exist."""
        for d in [
            self.data_dir / "nodes",
            self.coverage_dir,
            self.heatmaps_dir,
            self.links_dir,
            self.dem_dir,
        ]:
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
