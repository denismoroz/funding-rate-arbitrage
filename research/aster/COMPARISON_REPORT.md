# Aster DEX vs Hyperliquid — funding rate comparison

**Дата:** 2026-06-04
**Данные:** скачано в этой сессии, `research/aster/funding_history/` (7 монет вкл. HYPE,
8h funding, с 2024-06). Скрипт: `fetch_and_compare.py`.

---

## TL;DR — Aster закрывает СЛАБЫЕ места HL (SOL, AVAX)

Aster — perp DEX в экосистеме BNB (Binance-fork API). Не «11%-потолок»: в cold-режиме
обходит HL на портфеле (8.48% vs 7.22%, excl HYPE), и главное — **силён ровно там, где HL
слаб**: SOL и AVAX. Плюс низкая доля отрицательных часов.

| coin | HL cold % | Aster cold % | Δ (Ast−HL) | Aster neg % |
|------|-----------|--------------|------------|-------------|
| BTC  | 9.23      | 8.14         | −1.09      | 9.9         |
| ETH  | 7.68      | 8.06         | **+0.38**  | 7.9         |
| SOL  | 2.72      | **6.13**     | **+3.41**  | 23.6        |
| HYPE | 19.40     | 3.87         | −15.53     | 26.7        |
| AVAX | 5.16      | **10.49**    | **+5.33**  | 6.7         |
| LINK | 11.21     | 10.09        | −1.12      | 6.0         |
| DOGE | 7.31      | 7.94         | **+0.62**  | 14.9        |
| **портфель (excl HYPE)** | **7.22** | **8.48** | **+1.26** | |

*Окно COLD 2025-01-01 → 2026-04-01. Funding 8h, annualized = rate × 1095.*

---

## Ключевые наблюдения

1. **SOL +3.41% и AVAX +5.33%** — это две худшие cold-монеты HL (SOL 2.72%, AVAX 5.16%).
   Aster даёт по ним вдвое больше. Идеальная комплементарность.
2. **HYPE −15.5%** — HYPE родной для HL, на Aster его премия мизерная (3.87%). HYPE
   держать ТОЛЬКО на HL.
3. **Низкий negative-фон** на majors (AVAX 6.7%, LINK 6%, BTC 9.9%) — заметно лучше
   Backpack (там BTC 28%, AVAX 34%). Для short-perp harvest меньше часов «платим мы».
4. История с 2024-06 — есть кусок hot-окна (не анализировал, но данные собраны).

---

## Структурная пригодность

- **API:** `https://fapi.asterdex.com/fapi/v1/fundingRate` — Binance-совместимый, public,
  пагинация startTime, без ключей для public-data. Торговый API — Binance-style HMAC.
- **Funding:** каждые 8h (грубее HL hourly → менее гранулярный exit).
- **Spot:** Aster торгует и spot — delta-neutral в рамках одной venue возможен (проверить
  spot-пары SOL/AVAX + ликвидность).
- **Риск:** Binance-fork DEX, BNB-экосистема; быстрый рост 2025, но молодой, ликвидность
  тоньше HL.

---

## Оговорки

- 8h funding = грубее exit-гранулярность, чем hourly HL/Backpack.
- SOL neg% 23.6% — у Aster SOL тоже волатилен по знаку (как и везде).
- Не проверена реальная глубина стакана / slippage на SOL-AVAX perp.

---

## Вывод

**Aster — приоритетный диверсификатор №2 (после/наравне с Backpack).** Вместе они
накрывают слепые зоны HL:

| Лучшая venue по монете (cold) | |
|---|---|
| BTC | HL (9.23) |
| ETH | Aster/Backpack ≈ HL (~8.1) |
| **SOL** | **Aster (6.13)** ← HL только 2.72 |
| HYPE | HL (19.40) |
| **AVAX** | **Aster (10.49)** ← HL 5.16 |
| **LINK** | **Backpack (19.94)** ← HL 11.21 |
| DOGE | Backpack (10.89) / Aster (7.94) |

Это прямое подтверждение: **multi-venue funding-harvest поднимает пол доходности** —
HL не «единственный вариант». См. сводку cross-venue в `research/CROSS_VENUE_SYNTHESIS.md`.
