"""config_store.py — Load/save the runtime config.yml.

Shared by main.py (startup) and routers/config.py (Settings UI) so neither has to
import the other — this is what breaks the previous main↔config circular import.
"""

from __future__ import annotations

from pathlib import Path

import yaml

CONFIG_PATH = Path("/app/config.yml")


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open() as f:
            return yaml.safe_load(f) or {}
    return {}


def save_config(cfg: dict) -> None:
    with CONFIG_PATH.open("w") as f:
        yaml.dump(cfg, f, allow_unicode=True)
