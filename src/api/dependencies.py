"""
Dipendenze condivise FastAPI.
Fornisce istanze singleton di CoverageCalculator e InputService
iniettate nei router tramite Depends().
"""
from __future__ import annotations
from functools import lru_cache
from typing import Optional

from meshmonitor.processing.coverage_calculator import CoverageCalculator
from meshmonitor.input.service import InputService

# ---------------------------------------------------------------------------
# Singleton coverage calculator
# ---------------------------------------------------------------------------
_calculator: Optional[CoverageCalculator] = None


def get_calculator() -> CoverageCalculator:
    global _calculator
    if _calculator is None:
        _calculator = CoverageCalculator()
    return _calculator


# ---------------------------------------------------------------------------
# Singleton input service
# ---------------------------------------------------------------------------
_input_service: Optional[InputService] = None


def get_input_service() -> InputService:
    global _input_service
    if _input_service is None:
        _input_service = InputService()
    return _input_service


def set_input_service(svc: InputService):
    global _input_service
    _input_service = svc
