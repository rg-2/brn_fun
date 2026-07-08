"""Config + secrets loading.

Two layers:
  - config.yaml — instruments, granularity, db path (checked in).
  - .env / process env — Oanda credentials (never checked in).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Oanda's supported granularities. Not exhaustive of every variant they list,
# but covers everything we'd realistically use.
Granularity = Literal[
    "S5", "S10", "S15", "S30",
    "M1", "M2", "M4", "M5", "M10", "M15", "M30",
    "H1", "H2", "H3", "H4", "H6", "H8", "H12",
    "D", "W", "M",
]


class AppConfig(BaseModel):
    """Values from config.yaml."""

    instruments: list[str] = Field(min_length=1)
    default_granularity: Granularity = "M15"
    db_path: Path = Path("data/brn_fun.sqlite")
    price: Literal["M", "B", "A"] = "M"


class Secrets(BaseSettings):
    """Values from .env / process env. Field names map to OANDA_* by prefix."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="OANDA_",
        extra="ignore",
    )

    api_key: str
    account_id: str
    env: Literal["practice", "live"] = "practice"

    @property
    def api_hostname(self) -> str:
        return (
            "api-fxtrade.oanda.com"
            if self.env == "live"
            else "api-fxpractice.oanda.com"
        )


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """Load and validate config.yaml."""
    text = Path(path).read_text()
    data = yaml.safe_load(text) or {}
    return AppConfig(**data)


def load_secrets() -> Secrets:
    """Load Oanda credentials from .env or the environment."""
    return Secrets()  # type: ignore[call-arg]  # pydantic-settings reads env
