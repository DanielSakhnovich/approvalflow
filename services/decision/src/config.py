import json
from pathlib import Path

from afcommon.state import StateStore, cas_update
from pydantic import BaseModel

_THRESHOLDS_KEY = "config:thresholds"
_FX_KEY = "config:fx-rates"


class Thresholds(BaseModel):
    ceiling_cents: int = 25000
    trusted_ceiling_cents: int = 40000
    min_confidence: float = 0.80
    saas_monthly_cap_cents: int = 20000
    meals_per_attendee_cents: int = 7500


def _find_repo_file(name: str) -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / name).exists():
            return parent / name
    raise FileNotFoundError(name)


class ConfigRepo:
    def __init__(self, store: StateStore):
        self._store = store

    async def get_thresholds(self) -> Thresholds:
        value, _ = await self._store.get(_THRESHOLDS_KEY)
        if value is None:
            defaults = Thresholds()
            await cas_update(self._store, _THRESHOLDS_KEY, lambda _: defaults.model_dump())
            return defaults
        return Thresholds.model_validate(value)

    async def update_thresholds(self, partial: dict) -> Thresholds:
        unknown = set(partial) - set(Thresholds.model_fields)
        if unknown:
            raise ValueError(f"unknown threshold keys: {sorted(unknown)}")

        def merge(current: dict | None) -> dict:
            base = Thresholds.model_validate(current) if current else Thresholds()
            return Thresholds.model_validate({**base.model_dump(), **partial}).model_dump()

        merged = await cas_update(self._store, _THRESHOLDS_KEY, merge)
        return Thresholds.model_validate(merged)

    async def get_fx_rates(self) -> dict[str, float]:
        value, _ = await self._store.get(_FX_KEY)
        if value is None:
            seed = json.loads(_find_repo_file("sample-invoices.json").read_text())["fxRates"]
            await cas_update(self._store, _FX_KEY, lambda _: seed)
            return seed
        return value
