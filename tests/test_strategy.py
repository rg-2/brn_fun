"""Tests for the named-strategy registry."""

from __future__ import annotations

import pytest

from brn_fun.strategy import STRATEGIES, StrategyConfig, get_strategy


def test_audusd_strategy_registered() -> None:
    """The AUD_USD strategy is present with sensible defaults."""
    cfg = get_strategy("audusd")
    assert isinstance(cfg, StrategyConfig)
    assert cfg.instrument == "AUD_USD"
    assert cfg.granularity == "M1"
    assert cfg.grid == 0.01
    assert cfg.pip == 0.0001
    # Cost model is on by default — enforced by our "always model spread" policy.
    assert cfg.spread_pips > 0
    assert cfg.limit_offset_pips > 0
    # Reference performance is documented so anyone reading the config knows
    # what to compare against.
    assert cfg.reference_pnl_10y != ""
    assert cfg.description != ""


def test_get_strategy_unknown_raises() -> None:
    """Unknown strategy name raises a clear KeyError."""
    with pytest.raises(KeyError, match="Unknown strategy"):
        get_strategy("nonexistent")


def test_strategy_config_is_frozen() -> None:
    """Configs are immutable so nothing accidentally mutates them at runtime."""
    cfg = get_strategy("audusd")
    with pytest.raises(Exception):  # frozen dataclass raises FrozenInstanceError
        cfg.stop_pips = 999  # type: ignore[misc]


def test_strategy_registry_keys_match_config_names() -> None:
    """The dict key should equal each config's ``name`` field."""
    for key, cfg in STRATEGIES.items():
        assert cfg.name == key, f"{key} entry has mismatched name {cfg.name!r}"
