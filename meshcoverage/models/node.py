"""
Modelli dati per i nodi Meshtastic.
"""
from __future__ import annotations
from typing import Optional, Literal
from datetime import datetime, timezone
from enum import Enum
from pydantic import BaseModel, Field, validator


# ---------------------------------------------------------------------------
# Node roles (mirrors proto/node.proto)
# ---------------------------------------------------------------------------

class NodeRole(str, Enum):
    CLIENT         = "CLIENT"
    CLIENT_MUTE    = "CLIENT_MUTE"
    ROUTER         = "ROUTER"
    ROUTER_CLIENT  = "ROUTER_CLIENT"   # deprecated
    REPEATER       = "REPEATER"        # deprecated
    TRACKER        = "TRACKER"
    SENSOR         = "SENSOR"
    TAK            = "TAK"
    CLIENT_HIDDEN  = "CLIENT_HIDDEN"
    LOST_AND_FOUND = "LOST_AND_FOUND"
    TAK_TRACKER    = "TAK_TRACKER"
    ROUTER_LATE    = "ROUTER_LATE"
    CLIENT_BASE    = "CLIENT_BASE"


# Roles eligible for automatic coverage calculation
AUTO_COMPUTE_ROLES: frozenset[NodeRole] = frozenset({
    NodeRole.ROUTER,
    NodeRole.ROUTER_LATE,
    NodeRole.CLIENT_BASE,
})


# ---------------------------------------------------------------------------
# Preset modem Meshtastic con parametri LoRa e link budget
# Ref: https://meshtastic.org/docs/overview/radio-settings/
#
# Receiver sensitivity values are from the Semtech SX1276/SX1262 datasheet:
#   Sensitivity (dBm) = -174 + 10·log10(BW_Hz) + NF + SNR_min
#   NF  ≈ 6 dB  (SX1276 typical)
#   SNR thresholds per spreading factor (Semtech AN1200.22):
#     SF7: -7.5 dB   SF8: -10.0 dB   SF9: -12.5 dB
#     SF10: -15.0 dB  SF11: -17.5 dB  SF12: -20.0 dB
#
# These are the values Meshtastic firmware itself uses (RadioLibInterface.cpp).
# ---------------------------------------------------------------------------

MODEM_PRESETS: dict[str, dict] = {
    "SHORT_TURBO": {
        "spreading_factor": 7,
        "bandwidth_khz": 500,
        "coding_rate": "4/5",
        # -174 + 10*log10(500e3) + 6 + (-7.5) = -174 + 57.0 + 6 - 7.5 = -118.5 → round to -117
        "receiver_sensitivity_dbm": -117.0,
        "description": "Corto raggio, massima velocità",
    },
    "SHORT_FAST": {
        "spreading_factor": 7,
        "bandwidth_khz": 250,
        "coding_rate": "4/5",
        # -174 + 10*log10(250e3) + 6 + (-7.5) = -174 + 54.0 + 6 - 7.5 = -121.5 → -120
        "receiver_sensitivity_dbm": -120.0,
        "description": "Corto raggio, veloce",
    },
    "SHORT_SLOW": {
        "spreading_factor": 8,
        "bandwidth_khz": 250,
        "coding_rate": "4/5",
        # -174 + 54.0 + 6 + (-10.0) = -124.0 → -123
        "receiver_sensitivity_dbm": -123.0,
        "description": "Corto raggio, lento",
    },
    "MEDIUM_FAST": {
        "spreading_factor": 9,
        "bandwidth_khz": 250,
        "coding_rate": "4/5",
        # -174 + 54.0 + 6 + (-12.5) = -126.5 → -126
        "receiver_sensitivity_dbm": -126.0,
        "description": "Raggio medio, veloce (default)",
    },
    "MEDIUM_SLOW": {
        "spreading_factor": 10,
        "bandwidth_khz": 250,
        "coding_rate": "4/5",
        # -174 + 54.0 + 6 + (-15.0) = -129.0
        "receiver_sensitivity_dbm": -129.0,
        "description": "Raggio medio, lento",
    },
    "LONG_FAST": {
        "spreading_factor": 11,
        "bandwidth_khz": 250,
        "coding_rate": "4/5",
        # -174 + 54.0 + 6 + (-17.5) = -131.5 → -132
        "receiver_sensitivity_dbm": -132.0,
        "description": "Lungo raggio, veloce",
    },
    "LONG_MODERATE": {
        "spreading_factor": 11,
        "bandwidth_khz": 125,
        "coding_rate": "4/8",
        # -174 + 10*log10(125e3) + 6 + (-17.5) = -174 + 51.0 + 6 - 17.5 = -134.5 → -134
        "receiver_sensitivity_dbm": -134.0,
        "description": "Lungo raggio, moderato",
    },
    "LONG_SLOW": {
        "spreading_factor": 12,
        "bandwidth_khz": 125,
        "coding_rate": "4/8",
        # -174 + 51.0 + 6 + (-20.0) = -137.0
        "receiver_sensitivity_dbm": -137.0,
        "description": "Lungo raggio, lento",
    },
    "VERY_LONG_SLOW": {
        "spreading_factor": 12,
        "bandwidth_khz": 62.5,
        "coding_rate": "4/8",
        # -174 + 10*log10(62.5e3) + 6 + (-20.0) = -174 + 48.0 + 6 - 20.0 = -140.0
        "receiver_sensitivity_dbm": -140.0,
        "description": "Raggio massimo, molto lento",
    },
}

VALID_FREQUENCIES = {433, 868, 915}
VALID_MODEM_PRESETS = set(MODEM_PRESETS.keys())


# ---------------------------------------------------------------------------
# Modelli Pydantic
# ---------------------------------------------------------------------------

class Position(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)

    def __repr__(self):
        return f"Position(lat={self.lat:.6f}, lon={self.lon:.6f})"


class AntennaParams(BaseModel):
    """
    Parametri facoltativi dell'antenna. Vanno inseriti manualmente.

    Esempi:
    - Dipolo omnidirezionale: gain=2.15, azimuth=0, beamwidth=360, gain_min=gain_max=2.15
    - J-pole: gain=2.15, azimuth=15, beamwidth=360, gain_min=2.1@195°, gain_max=2.2@15°
    - Yagi direzionale: gain=8, azimuth=180, beamwidth=60, gain_min=0@150/210°, gain_max=8@180°
    """
    tx_power_dbm: Optional[float] = Field(default=None, description="Potenza TX (dBm)")
    type: Optional[str] = Field(default=None, description="Tipo antenna: dipole, collinear, yagi, j-pole, ...")
    gain_dbi: Optional[float] = Field(default=None, description="Guadagno nominale (dBi)")
    azimuth_deg: Optional[float] = Field(default=0.0, description="Direzione puntamento (0=Nord, °)")
    beamwidth_deg: Optional[float] = Field(default=360.0, description="Angolo apertura (° orizzontale)")
    gain_min_dbi: Optional[float] = Field(default=None, description="Guadagno minimo (dBi)")
    gain_max_dbi: Optional[float] = Field(default=None, description="Guadagno massimo (dBi)")
    gain_min_angle: Optional[float] = Field(default=None, description="Angolo del guadagno minimo (°)")
    gain_max_angle: Optional[float] = Field(default=None, description="Angolo del guadagno massimo (°)")
    elevation_deg: Optional[float] = Field(default=0.0, description="Inclinazione verticale (°, >0=sopra orizzonte)")

    @property
    def is_directional(self) -> bool:
        """True se l'antenna è direzionale (beamwidth < 360°)."""
        return self.beamwidth_deg is not None and self.beamwidth_deg < 360.0

    def gain_at_azimuth(self, target_azimuth: float) -> float:
        """
        Calcola il guadagno dell'antenna verso un azimuth dato.
        Per antenne omnidirezionali restituisce gain_dbi.
        Per antenne direzionali usa interpolazione lineare.
        """
        if self.gain_dbi is None:
            return 0.0

        if not self.is_directional:
            return self.gain_dbi

        # Differenza angolare normalizzata [-180, 180]
        diff = ((target_azimuth - (self.azimuth_deg or 0)) + 180) % 360 - 180
        half_bw = (self.beamwidth_deg or 60) / 2.0

        if abs(diff) > half_bw:
            # Fuori dal fascio: usa gain_min se disponibile, altrimenti -20dBi
            return self.gain_min_dbi if self.gain_min_dbi is not None else -20.0

        # Dentro il fascio: interpolazione coseno semplificata
        gain_max = self.gain_max_dbi if self.gain_max_dbi is not None else self.gain_dbi
        gain_min = self.gain_min_dbi if self.gain_min_dbi is not None else (self.gain_dbi - 3)
        factor = (1.0 - abs(diff) / half_bw)  # 1 al centro, 0 ai bordi
        return gain_min + factor * (gain_max - gain_min)

    def is_in_coverage_sector(self, target_azimuth: float) -> bool:
        """True se il target è nell'angolo di copertura dell'antenna."""
        if not self.is_directional:
            return True
        diff = abs(((target_azimuth - (self.azimuth_deg or 0)) + 180) % 360 - 180)
        return diff <= (self.beamwidth_deg or 360) / 2.0


class Node(BaseModel):
    """Nodo Meshtastic con tutti i dati disponibili."""
    id: str = Field(..., description="Identificativo !aabbccdd")
    role: Optional[NodeRole] = Field(default=None, description="Ruolo del nodo nella mesh")
    short_name: Optional[str] = None
    long_name: Optional[str] = None
    hardware_model: Optional[str] = None
    firmware: Optional[str] = None
    position: Optional[Position] = None
    ground_height_m: Optional[float] = Field(default=None, description="Altezza antenna dal suolo (m)")
    frequency_mhz: Optional[int] = Field(default=None, description="868, 433 o 915 MHz")
    modem_preset: Optional[str] = Field(default=None, description="Preset modem Meshtastic")
    antenna: Optional[AntennaParams] = Field(default=None)
    last_seen: Optional[datetime] = None
    auto_update: bool = Field(default=True, description="Se True, i dati di questo nodo possono essere aggiornati automaticamente")
    notes: Optional[str] = None

    @validator("id")
    def validate_id(cls, v):
        if not v.startswith("!") or len(v) != 9:
            raise ValueError(f"ID nodo non valido: {v!r}. Deve essere '!aabbccdd'")
        return v.lower()

    @validator("frequency_mhz")
    def validate_frequency(cls, v):
        if v is not None and v not in VALID_FREQUENCIES:
            raise ValueError(f"Frequenza non valida: {v}. Valori validi: {VALID_FREQUENCIES}")
        return v

    @validator("modem_preset")
    def validate_preset(cls, v):
        if v is not None and v not in VALID_MODEM_PRESETS:
            raise ValueError(f"Preset non valido: {v!r}")
        return v

    @property
    def is_complete(self) -> bool:
        """
        True se il nodo ha tutti i dati necessari per il calcolo di copertura.
        Richiede: posizione, frequenza, modem_preset, e parametri antenna.
        """
        if self.position is None:
            return False
        if self.frequency_mhz is None or self.modem_preset is None:
            return False
        if self.antenna is None:
            return False
        if self.antenna.tx_power_dbm is None or self.antenna.gain_dbi is None:
            return False
        return True

    @property
    def modem_params(self) -> Optional[dict]:
        """Parametri tecnici del preset modem selezionato."""
        if self.modem_preset is None:
            return None
        return MODEM_PRESETS.get(self.modem_preset)

    def update_from(self, other: "Node"):
        """
        Aggiorna i campi di questo nodo con i dati non-nulli dell'altro.
        Mantiene sempre i dati più recenti, completando quelli mancanti.
        """
        for field_name in self.model_fields:
            other_val = getattr(other, field_name, None)
            if other_val is not None:
                if field_name == "last_seen":
                    if self.last_seen is None or (other_val > self.last_seen):
                        object.__setattr__(self, field_name, other_val)
                else:
                    object.__setattr__(self, field_name, other_val)


class NodeUpdate(BaseModel):
    """Schema per aggiornamento parziale nodo via API."""
    role: Optional[NodeRole] = None
    short_name: Optional[str] = None
    long_name: Optional[str] = None
    position: Optional[Position] = None
    ground_height_m: Optional[float] = None
    frequency_mhz: Optional[int] = None
    modem_preset: Optional[str] = None
    antenna: Optional[AntennaParams] = None
    notes: Optional[str] = None
