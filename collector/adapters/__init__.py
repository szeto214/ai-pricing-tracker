"""Registry adapter per-situs.

Adapter dipakai kalau heuristik generik menghasilkan data buruk untuk satu
situs. Tulis adapter HANYA setelah melihat HTML asli situs itu — tebakan buta
lebih buruk daripada heuristik generik.

Cara menambah adapter:

    # collector/adapters/cursor.py
    from ..extract import Plan, parse_price, parse_period

    def extract(soup, raw_html) -> list[Plan]:
        plans = []
        for card in soup.select("[data-testid='pricing-card']"):
            ...
        return plans          # kembalikan [] kalau tidak yakin -> jatuh ke generik

Nama modul harus sama persis dengan slug target, dengan '-' diganti '_'.
Adapter yang melempar exception dicatat sebagai error dan pipeline lanjut
memakai ekstraktor generik — arsip tidak pernah berhenti karena adapter patah.
"""

from __future__ import annotations

import importlib
from typing import Callable

_CACHE: dict[str, Callable | None] = {}


def get_adapter(slug: str):
    if slug in _CACHE:
        return _CACHE[slug]
    mod_name = f"{__name__}.{slug.replace('-', '_')}"
    try:
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, "extract", None)
    except ModuleNotFoundError:
        fn = None
    except Exception:  # noqa: BLE001  — adapter rusak jangan menjatuhkan run
        fn = None
    _CACHE[slug] = fn
    return fn
