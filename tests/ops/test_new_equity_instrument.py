"""scripts/ops/new_equity_instrument.py: one stock's instrument file, SPY's shape and SPY's caps."""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "new_equity_instrument", REPO / "scripts/ops/new_equity_instrument.py"
)
assert spec and spec.loader
gen = importlib.util.module_from_spec(spec)
sys.modules["new_equity_instrument"] = gen
spec.loader.exec_module(gen)


@pytest.fixture
def config(tmp_path: Path) -> Path:
    shutil.copytree(REPO / "config/instruments", tmp_path / "instruments")
    shutil.copytree(REPO / "config/limits", tmp_path / "limits")
    return tmp_path


@pytest.mark.parametrize("symbol", ["F", "XYZ", "BRK.B", "ABCDEF"])
def test_valid_symbols(symbol: str) -> None:
    assert gen.validate_symbol(symbol) == symbol


@pytest.mark.parametrize(
    "symbol", ["", "f", "Xyz", "ABCDEFG", "BRK.", ".B", "BR..K", "A1", "A-B", "../X", "F\n"]
)
def test_invalid_symbols_are_refused(symbol: str) -> None:
    with pytest.raises(ValueError):
        gen.validate_symbol(symbol)


def test_writes_spys_shape_with_spys_limits_file(config: Path) -> None:
    path = gen.write_instrument("XYZ", config)
    assert path == config / "instruments/xyz.toml"
    new = tomllib.loads(path.read_text())
    spy = tomllib.loads((config / "instruments/spy.toml").read_text())
    assert new["symbol"] == "XYZ"
    assert new["id"] == spy["id"] + 1
    for key in ("tick_size", "qty_step", "min_qty", "allows_fractional", "trading_hours"):
        assert new[key] == spy[key], key
    assert new["tick_size"] == "0.01" and new["qty_step"] == "1"
    assert not new["allows_fractional"]
    # The very same limits file as SPY, so its caps can never be looser than SPY's.
    assert (path.parent / new["limits"]).resolve() == (config / "limits/spy.toml").resolve()
    assert (path.parent / new["trading_hours"]["calendar"]).is_file()


def test_ids_stay_unique(config: Path) -> None:
    first = tomllib.loads(gen.write_instrument("F", config).read_text())["id"]
    second = tomllib.loads(gen.write_instrument("XYZ", config).read_text())["id"]
    assert second == first + 1


def test_refuses_to_overwrite(config: Path) -> None:
    with pytest.raises(FileExistsError):
        gen.write_instrument("SPY", config)


def test_main_exits_1_on_a_bad_symbol(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert gen.main(["xyz", "--config-dir", str(config)]) == 1
    assert "invalid symbol" in capsys.readouterr().err
    assert not (config / "instruments/xyz.toml").exists()
