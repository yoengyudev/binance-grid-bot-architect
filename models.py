"""Request schemas, immutable grid configuration, and engine exceptions."""

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional, Tuple
from pydantic import BaseModel, Field, FiniteFloat, StrictBool, StrictInt
from exchange_handler import load_config
from settings import (
    MAX_LEVELS,
)


OpenOrderEntry = Tuple[Optional[Decimal], Optional[Decimal]]


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class PauseRequest(BaseModel):
    active: StrictBool


class EngineStartRequest(BaseModel):
    auto_cover_inventory: StrictBool = False
    pending_grid_token: Optional[str] = None


class ForceLiquidateRequest(BaseModel):
    confirmed: StrictBool


class StopLossRequest(BaseModel):
    price: FiniteFloat = Field(gt=0)


class RecenterRequest(BaseModel):
    center_price: FiniteFloat = Field(gt=0)
    width_percentage: Optional[FiniteFloat] = Field(default=None, gt=0, lt=100)
    stop_loss_percentage: Optional[FiniteFloat] = Field(default=None, gt=0, lt=100)
    half_width_percentage: Optional[FiniteFloat] = Field(default=None, gt=0, lt=100)
    allocated_capital: Optional[FiniteFloat] = Field(default=None, gt=0)
    grid_levels: Optional[StrictInt] = Field(default=None, ge=2, le=2 * MAX_LEVELS)


class TradingHalt(RuntimeError):
    """An unsafe or unresolved state that requires the bot to stop."""


class GridStopConflict(TradingHalt):
    """Exact BUY levels overlap the retained hard-stop floor."""

    def __init__(self, lowest_buy_price: Decimal, hard_stop_price: Decimal) -> None:
        self.lowest_buy_price = lowest_buy_price
        self.hard_stop_price = hard_stop_price
        super().__init__(
            f"Cannot start this exact grid: lowest BUY {lowest_buy_price:,.2f} USDT "
            f"is at or below the hard stop {hard_stop_price:,.2f} USDT. "
            "Re-anchor with all BUY levels above the hard stop and keep the "
            "stop at or above its current value. Save and review the new grid "
            "before pressing START."
        )


class InsufficientGridCapital(TradingHalt):
    """A requested grid cannot be funded from the free Spot quote balance."""


class GridSizingError(ValueError):
    """A rounded grid order cannot satisfy the required minimum size."""


class UncertainOrderError(TradingHalt):
    """The exchange may have accepted an order without returning its ID."""


class LiquidationPending(RuntimeError):
    """BTC is still exposed; retry reconciliation without declaring success."""


class RunnerLeaseLost(TradingHalt):
    """This process no longer owns the durable trading fence."""


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"Set grid.{name} to a positive number in config.json.")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"grid.{name} must be a valid number.") from error
    if not result.is_finite() or result <= 0:
        raise ValueError(f"grid.{name} must be positive and finite.")
    return result


def _order_decimal(value: Any, default: str = "0") -> Decimal:
    return Decimal(str(value if value is not None else default))


@dataclass(frozen=True)
class GridConfig:
    symbol: str
    investment_quote: Decimal
    lower_price: Decimal
    upper_price: Decimal
    spacing_percent: Decimal
    initial_inventory_percent: Decimal
    stop_loss_price: Decimal
    poll_seconds: int
    auto_center_percent: Optional[Decimal] = None
    buy_grid_levels: Optional[int] = None
    sell_grid_levels: Optional[int] = None

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "GridConfig":
        raw = load_config(path)
        grid = raw["grid"]
        investment = _decimal(grid.get("investment_quote"), "investment_quote")
        lower = _decimal(grid.get("lower_price"), "lower_price")
        upper = _decimal(grid.get("upper_price"), "upper_price")
        spacing = _decimal(grid.get("spacing_percent"), "spacing_percent")
        inventory_percent = _decimal(
            grid.get("initial_inventory_percent", 50), "initial_inventory_percent"
        )
        stop_loss = _decimal(grid.get("stop_loss_price"), "stop_loss_price")
        poll_seconds = grid.get("poll_seconds", 10)
        if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, int):
            raise ValueError("grid.poll_seconds must be an integer.")
        if not 2 <= poll_seconds <= 3600:
            raise ValueError("grid.poll_seconds must be between 2 and 3600.")
        if not 0 < stop_loss < lower < upper:
            raise ValueError("Require stop_loss_price < lower_price < upper_price.")
        if spacing >= 100:
            raise ValueError("grid.spacing_percent must be below 100.")
        if inventory_percent >= 100:
            raise ValueError("grid.initial_inventory_percent must be below 100.")
        configured_width = grid.get("auto_center_percent")
        width = (
            _decimal(configured_width, "auto_center_percent")
            if configured_width is not None
            else (upper - lower) * 100 / (upper + lower)
        )
        if width >= 100:
            raise ValueError("grid.auto_center_percent must be below 100.")
        return cls(raw["grid"]["symbol"], investment, lower, upper,
                   spacing, inventory_percent, stop_loss, poll_seconds, width)

    def fingerprint(self) -> str:
        parameters = {
            "symbol": self.symbol,
            "investment_quote": str(self.investment_quote),
            "lower_price": str(self.lower_price),
            "upper_price": str(self.upper_price),
            "spacing_percent": str(self.spacing_percent),
            "initial_inventory_percent": str(self.initial_inventory_percent),
            "stop_loss_price": str(self.stop_loss_price),
        }
        if self.buy_grid_levels is not None and self.sell_grid_levels is not None:
            parameters["buy_grid_levels"] = self.buy_grid_levels
            parameters["sell_grid_levels"] = self.sell_grid_levels
        encoded = json.dumps(parameters, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def breakout_fingerprint(self) -> str:
        """Grid geometry stays stable when the trailing stop rises."""
        parameters = {
            "symbol": self.symbol,
            "investment_quote": str(self.investment_quote),
            "lower_price": str(self.lower_price),
            "upper_price": str(self.upper_price),
            "spacing_percent": str(self.spacing_percent),
            "initial_inventory_percent": str(self.initial_inventory_percent),
        }
        if self.buy_grid_levels is not None and self.sell_grid_levels is not None:
            parameters["buy_grid_levels"] = self.buy_grid_levels
            parameters["sell_grid_levels"] = self.sell_grid_levels
        encoded = json.dumps(parameters, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class StopResult:
    canceled: int = 0
    filled: int = 0
    unresolved: int = 0
