> **>> CORRECTION NOTE (2026-06-05):** The Backpack funding numbers in this file are
> **superseded** by a cadence-agnostic (time-weighted) annualization. Backpack's 8h
> funding era was previously annualized ~8× too high. Canonical corrected Backpack
> cold numbers (from the fixed `backpack/compare_hl_backpack.py`): BTC 4.09%, ETH 4.62%,
> SOL 0.06%, AVAX 0.86%, LINK 11.96%, DOGE 6.35%. Corrected routing:
> **HL best:** BTC (9.23%), HYPE (19.40%), LINK (11.21% — see caveat)
> **Aster best:** ETH (8.06%), SOL (6.14%), AVAX (10.49%), DOGE (7.94%)
> **Backpack:** not best on any coin worth routing — it only *marginally* edges HL on
> LINK (11.96 vs 11.21, +0.75pp), but with ~2× the negative-hours (13% vs 6%) and thinner
> liquidity, so LINK stays on HL. The old "best-of-3" table below (Backpack winning
> ETH/LINK/DOGE by wide margins) is wrong — those were the inflated numbers.
> Old portfolio APR claims (12.05% gross, 11.0% 50/50) are inflated; the corrected
> two-phase backtest (`CROSS_VENUE_BACKTEST_REPORT.md`) gives occupied-APR ~6.7%
> (8.3% +staking). See corrected `portfolio_50k_model.py` / `PORTFOLIO_50K_REPORT.md`.

# Cross-Venue Funding Harvest — синтез (HL + Backpack + Aster)

**Дата:** 2026-06-04
**Контекст:** проверка тезиса «HL — единственный нормальный venue». Опровергнут:
Backpack и Aster — настоящие диверсификаторы, закрывающие слабые места HL.
Источники: `research/backpack/COMPARISON_REPORT.md`, `research/aster/COMPARISON_REPORT.md`.

---

## 1. Best-venue-per-coin (cold-режим 2025-01 → 2026-04, annualized funding %)

| coin | HL    | Backpack | Aster | **лучший** | venue |
|------|-------|----------|-------|------------|-------|
| BTC  | 9.23  | 7.32     | 8.14  | **9.23**   | HL |
| ETH  | 7.68  | 8.25     | 8.06  | **8.25**   | Backpack |
| SOL  | 2.72  | 0.10     | 6.13  | **6.13**   | Aster |
| HYPE | 19.40 | —        | 3.87  | **19.40**  | HL |
| AVAX | 5.16  | 1.38     | 10.49 | **10.49**  | Aster |
| LINK | 11.21 | 19.94    | 10.09 | **19.94**  | Backpack |
| DOGE | 7.31  | 10.89    | 7.94  | **10.89**  | Backpack |

**Каждая площадка лидирует на своём наборе:**
- **HL** — BTC, HYPE (родной токен)
- **Aster** — SOL, AVAX (две худшие монеты HL!)
- **Backpack** — ETH, LINK, DOGE

---

## 2. Прирост доходности (gross funding на ноционал, equal-weight 7 монет)

| Аллокация | Mean funding APR | Δ к HL-only |
|-----------|------------------|-------------|
| HL-only   | **8.96%**        | —           |
| Best-of-3 (идеальный роутинг) | **12.05%** | **+3.09 пп (+34% относит.)** |

Прирост приходит из закрытия именно провалов HL: SOL 2.72→6.13, AVAX 5.16→10.49,
LINK 11.21→19.94. Это gross на ноционал; после fee/margin реализованный APR на занятый
капитал масштабируется тем же множителем (см. waterfall в [[project_margin_backtest_findings]]).

**Честная оговорка:** 12.05% — это **верхняя граница** (in-sample, идеальный роутинг,
без cross-venue издержек). Реальный прирост меньше — см. §4.

---

## 3. Архитектура cross-venue (что выбрал пользователь проработать)

Ключевая идея: **спот-нога не обязана быть на той же площадке, что перп-нога.**

```
        ┌─ spot LONG (там, где дешевле/ликвиднее spot)
delta-  │
neutral ┤
        └─ perp SHORT (там, где funding ЖИРНЕЕ всего в моменте)
```

### Вариант A — single-venue per coin (проще)
Каждая монета целиком на своей лучшей площадке (spot+perp на одной venue, unified margin):
- BTC/HYPE → HL, SOL/AVAX → Aster, ETH/LINK/DOGE → Backpack.
- Требование: на venue есть И spot, И perp для монеты. ✅ HL; ⚠️ Aster/Backpack — нужно
  верифицировать spot-пары SOL/AVAX/LINK/DOGE и их ликвидность (TODO).
- Маржа: unified в рамках venue (как сейчас на HL). Никакого cross-venue transfer-риска.
- **Это минимальное расширение текущего движка** — тот же 3-leg паттерн, просто три
  инстанса executor'а на три venue.

### Вариант B — true cross-venue (сложнее, гибче)
Спот на одной площадке, short-perp на другой (где funding выше прямо сейчас):
- Расширяет вселенную перп-онли площадок (Lighter/edgeX/Paradex/dYdX — все живые, API
  проверены 2026-06-04).
- Цена: маржа на ДВУХ venue, нет автоматического hedge-offset → выше суммарный
  margin-lock; rebalance при расхождении; bridge/transfer USDC между площадками.
- Оправдан только на капитале, где edge перекрывает двойную маржу + gas.

---

## 4. Издержки и риски, которые срежут идеальные 12%

1. **In-sample / lookahead** — «лучший venue» посчитан задним числом. Реально funding-
   лидерство монеты по venue дрейфует; нужен live-роутинг по скользящему сигналу.
2. **Ликвидность тоньше HL** — Backpack/Aster объёмы в разы меньше; slippage на входе/
   выходе ест edge, особенно на $10k+ позициях.
3. **Доп. fees/gas** — три venue = три набора taker-fee + (для Aster/DEX) gas/bridge.
4. **Operational overhead** — 3 executor-модуля, 3 набора ключей, 3 баланса USDC,
   мониторинг ликвидаций на каждой.
5. **Counterparty/chain-риск ×3** — диверсификация funding-источника ценой умножения
   площадочного риска. Backpack (CEX-like), Aster (BNB-chain), HL (L1) — разные профили.
6. **Negative-hours различаются** — роутинг должен смотреть не только на среднее, но и на
   долю отрицательных часов (Backpack majors 28-39% neg — там short-perp часто платит).

---

## 5. Вывод и next steps

**Тезис «HL — единственный вариант» опровергнут эмпирически.** Multi-venue поднимает
gross-floor с ~9% до ~12% (верхняя граница), закрывая провалы HL по SOL/AVAX/LINK.
Backpack и Aster — комплементарны, не дубль.

**Приоритет (для roadmap, не для $1k сейчас):**
1. Верифицировать spot-пары SOL/AVAX (Aster) и LINK/DOGE/ETH (Backpack) + глубину стакана.
2. Backtest Варианта A: тот же two_phase движок, но per-coin venue-роутинг по cold-данным
   обеих площадок. Сравнить с HL-only на занятый капитал.
3. Решение о масштабе: Вариант A оправдан при ~$10-50k; Вариант B (true cross-venue) —
   при $50k+.

**Сейчас ($1k shakedown):** остаёмся HL-only, gas+slippage > edge. Это roadmap на scaling.
```
Проверенные живые API (2026-06-04): Aster fapi.asterdex.com, Backpack api.backpack.exchange,
Paradex api.prod.paradex.trade, Lighter mainnet.zklighter.elliot.ai, edgeX pro.edgex.exchange.
```
