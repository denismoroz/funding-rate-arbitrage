"""
Ф0 — конфиг костов для стенда.

Реальные косты из research B (аудит 2026-06-11): главный рычаг B — maker vs taker.
Стенд по умолчанию судит по TAKER (консервативно) + slippage; maker — отдельный
сценарий, чтобы видеть, держится ли эдж только на оптимистичном исполнении.
"""
from __future__ import annotations

from dataclasses import dataclass

# из engine.py (Hyperliquid): perp taker 3.5bps, spot taker 7bps
from engine import PERP_TAKER, SPOT_TAKER


@dataclass(frozen=True)
class Costs:
    perp_fee: float = PERP_TAKER      # за ногу перпа, доля notional
    spot_fee: float = SPOT_TAKER      # за ногу спота
    slippage: float = 0.0005          # 5bps, добавляется к каждой сделке

    @property
    def perp_cost(self) -> float:
        return self.perp_fee + self.slippage

    @property
    def spot_cost(self) -> float:
        return self.spot_fee + self.slippage


# консервативный дефолт стенда (taker + 5bps slip)
TAKER = Costs()

# оптимистичный сценарий (maker 2bps, тот же slip) — для «держится ли эдж на maker»
MAKER = Costs(perp_fee=0.0002, spot_fee=0.0002, slippage=0.0005)
