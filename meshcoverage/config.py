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

    # DEM — bare-earth terrain model (required)
    dem_dir: Path = Field(default=Path("./data/dem"))
    dem_resolution: int = Field(default=30)   # metres

    # DSM — surface model with buildings and vegetation (optional, Change F)
    #
    # When set, obstacle heights along the signal path are read from the DSM
    # (which includes buildings, trees, etc.) instead of the bare-earth DTM.
    # TX and RX ground elevations always use the DTM so that antenna height
    # above ground and receiver height are measured correctly.
    #
    # Recommended free sources (GeoTIFF, place tiles in this directory):
    #   - Copernicus DEM GLO-30 DSM (global, 30 m):
    #       https://spacedata.copernicus.eu/collections/copernicus-digital-elevation-model
    #   - ALOS AW3D30 (global, 30 m):
    #       https://www.eorc.jaxa.jp/ALOS/en/aw3d30/
    #   - Local LiDAR DSM from national mapping agencies (best accuracy).
    #
    # Leave unset (or empty) to use bare-earth DTM only.
    dsm_dir: Optional[Path] = Field(
        default=None,
        description=(
            "Directory containing DSM GeoTIFF files (Digital Surface Model — "
            "includes buildings and vegetation). Optional. When provided, "
            "obstacle heights along the signal path use the DSM, improving "
            "accuracy in urban and forested areas. TX/RX ground elevations "
            "always come from the bare-earth DTM."
        ),
    )

    # Coverage calculation
    max_workers: int = Field(default=0)
    max_range_km: float = Field(default=50.0)
    receiver_height_m: float = Field(default=1.5)
    receiver_gain_dbi: float = Field(default=2.15)
    min_link_budget_db: float = Field(default=0.0)
    erp_warning_dbm: float = Field(default=27.0)

    # Heatmap
    heatmap_resolution_m: float = Field(default=100.0)

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
        dirs = [
            self.data_dir / "nodes",
            self.coverage_dir,
            self.heatmaps_dir,
            self.links_dir,
            self.dem_dir,
        ]
        if self.dsm_dir:
            dirs.append(self.dsm_dir)
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
