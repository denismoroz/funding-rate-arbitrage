# Plan: Extend HL Universe Backtest (post-MVP1 release)

**Status:** PAUSED — to be picked up ~2026-06-04 after current release work settles.

## Context

MVP1 в проде на HL с U3 = {BTC, ETH, SOL}. На последние 90 дней даёт ~7% annualized — кросс-биржевой путь (Binance/Bybit spot + HL perp short) принципиально сложен (transfer latency, atomic execution на 2 биржах). Цель: **выжать максимум из одной HL**, добавив все коины где есть native/wrapped USDC спот с приемлемой parity и ликвидностью.

## Что уже выяснено (2026-05-31)

Аудит через `spotMetaAndAssetCtxs` + `metaAndAssetCtxs` показал **7 viable** коинов на HL для delta-neutral (spot + perp short):

| Coin | spot pair | kind    | max_lev | parity | spot $vol/d | now funding APR |
|------|-----------|---------|---------|--------|-------------|-----------------|
| **HYPE** | HYPE  | NATIVE  | 10×     | 0.04%  | **$135M**   | **41.8%** |
| BTC      | UBTC  | WRAPPED | **40×** | 0.09%  | $11M        | floor 10.95% |
| **ZEC**  | UZEC  | WRAPPED | 10×     | 0.08%  | **$10.6M**  | floor |
| ETH      | UETH  | WRAPPED | **25×** | 0.10%  | $3.3M       | floor |
| **PURR** | PURR  | NATIVE  | 3×      | 0.88%  | $3M         | **157%** (волатилен) |
| SOL      | USOL  | WRAPPED | **20×** | 0.10%  | $1.7M       | floor |
| XPL      | UXPL  | WRAPPED | 10×     | 0.03%  | $0.2M       | −2% (тонкий) |

**Выпали (по-прежнему недоступны):**
- AVAX — UAVAX parity 6.87%, тонкий top-of-book
- LINK, AAVE, BNB — только через bridge tokens (*0) с independent price discovery
- DOGE — нет HL спота вообще
- TRUMP, BERA, PUMP, MON, MEGA — parity broken (perp есть, спот мёртв)

**Сюрприз 1:** leverage caps значительно выросли с прошлого аудита (2026-05-19):
- BTC 20× → **40×** (cap_eff 0.87 → 0.93)
- ETH 20× → **25×** (0.87 → 0.89)
- SOL 10× → **20×** (0.77 → 0.87)
Это уже само по себе бустит cap_efficiency существующих U3 позиций ~5-7pp без изменений стратегии.

**Сюрприз 2:** HYPE с $135M/d spot volume и текущим funding ~42% APR — кандидат №1 для расширения. Native (без обёртки), parity идеальная.

## План работы (когда возвращаемся)

### Step 1 — Fetch funding history для 4 новых
- HYPE, ZEC, PURR, XPL через [research/fetch_funding_history.py](fetch_funding_history.py)
- От листинга каждого коина до today (HYPE с ~ноября 2024, ZEC дата неизвестна, PURR с лета 2023)
- Сохранить в `research/data/<COIN>.csv` стандартным форматом

### Step 2 — Обновить per-coin attribution для нового универса
- Скрипт: `/tmp/per_coin_attribution.py` (адаптировать под актуальные leverage caps, добавить HYPE/ZEC/PURR/XPL)
- Запустить на full / 2024 / 2025 / last_180d / last_90d / last_30d
- Цель: понять кто из 4 новых реально несёт альфу в холодном рынке

### Step 3 — Re-run portfolio_margin_sweep на расширенных универсах
Прогнать [research/portfolio_margin_sweep.py](portfolio_margin_sweep.py) на 3 универсах с обновлёнными leverage caps:

```python
PER_COIN_LEVERAGE = {
    "BTC": 40, "ETH": 25, "SOL": 20,          # обновлённые
    "HYPE": 10, "ZEC": 10, "PURR": 3, "XPL": 10,
}
```

**Универсы:**
- **U3** (baseline, что в проде): {BTC, ETH, SOL} — пересчитать с новыми leverages
- **U4** (консервативный апгрейд): U3 + HYPE
- **U5**: U4 + ZEC
- **U7** (всё viable): + PURR + XPL

**Сетка:** margin_buffer ∈ {2, 3, 5} × position_size ∈ {$50, $100, $150} × K ∈ {3, 5, 7}

### Step 4 — Per-coin attribution внутри backtest
Расширить [research/portfolio_margin.py:62](portfolio_margin.py#L62) чтобы state логировал `per_coin_funding`, `per_coin_fees`, `per_coin_n_trades`. Иначе мы получим только агрегаты и не поймём кто внутри корзины двигает результат (как в текущем sweep).

### Step 5 — Report
Что нужно увидеть:
- Annual / Calmar / Sharpe для каждой комбинации universe × buffer × size × K
- Per-coin attribution на лучшей конфигурации каждого универса
- Сравнение U3-new (с новыми lev) vs U3-old vs U4 vs U7 — где переломный момент в risk-adjusted
- Анализ PURR отдельно: 3× lev, но текущий funding 157% — стоит ли держать как «концентрированную» позицию или урезать size_per_coin?

### Step 6 — Production rollout decision
- Если U4 (+HYPE) выигрывает на cold market — обновить strategy params в БД, добавить HYPE в `MAINNET_SPOT_TOKEN_MAP` (NATIVE, не WRAPPED) и в universe.
- Если PURR/ZEC дают marginal — оставить отложенным
- Обновить [project_strategy_a_final.md](.claude/projects/-Users-d-prj-funding-rate-arbitrage/memory/project_strategy_a_final.md) с новой рекомендацией

## Файлы, которые потребуется тронуть для rollout

1. [src/frab/exchanges/hyperliquid/tokens.py](../src/frab/exchanges/hyperliquid/tokens.py) — добавить HYPE (NATIVE, без обёртки) в MAINNET_SPOT_TOKEN_MAP или ввести отдельный NATIVE_SPOT_TOKEN_SET
2. [src/frab/strategy/two_phase/params.py:16](../src/frab/strategy/two_phase/params.py#L16) — расширить default universe
3. Strategy params в БД (через UI или скриптом) — на live стратегии
4. `validate_spot_pairs` — учесть native (PURR, HYPE) которые не имеют префикса U

## Открытые вопросы для разрешения в рантайме

- Действительно ли HYPE достаточно ликвиден для $100 спот ордера без >5bps slippage? Проверить L2 book live.
- ZEC funding history — есть ли у него «горячие» периоды или он всегда у floor?
- PURR 3× leverage — насколько это меняет required capital? cap_eff = 100/(100+100/3×3) = 0.5 — очень дорого.

## Связанная память
- [project_strategy_a_final.md](.claude/projects/-Users-d-prj-funding-rate-arbitrage/memory/project_strategy_a_final.md) — текущая рекомендация на 7 коинов (большая часть из которых не работает на HL)
- [feedback_hl_bridge_tokens.md](.claude/projects/-Users-d-prj-funding-rate-arbitrage/memory/feedback_hl_bridge_tokens.md) — почему AVAX0/LINK0/AAVE0 нельзя мапить
- [project_mvp1_release.md](.claude/projects/-Users-d-prj-funding-rate-arbitrage/memory/project_mvp1_release.md) — текущий live статус
