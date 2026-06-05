# Venue × Coin Map — где спот, где перп, лучший funding, стейкинг

**Дата:** 2026-06-05
**Назначение:** единая карта решений по long-ноге (spot) для каждой монеты — на какой
площадке собирать дельта-нейтраль (unified vs decoupled), с учётом доступности спота,
лучшего funding-источника и нативного стейкинга.

**Источники:**
- HL spot/perp — живой `POST api.hyperliquid.xyz/info {"type":"spotMeta"}` (проверено curl 2026-06-05).
- Aster spot/perp — `fapi.asterdex.com/fapi/v1/exchangeInfo` (perp) + `sapi.asterdex.com/api/v1/exchangeInfo` (spot).
- Funding — cold-окно 2025-01 → 2026-04, исправленные (cadence-agnostic) замеры из
  `CROSS_VENUE_BACKTEST_REPORT.md` / `CROSS_VENUE_SYNTHESIS.md`.
- Стейкинг — `research/staking/staking_inputs.csv` (консервативные sourced-значения).

---

## 1. Доступность Spot / Perp

| монета | HL spot | HL perp | Aster spot | Aster perp |
|--------|:-------:|:-------:|:----------:|:----------:|
| BTC  | ✅ uBTC | ✅ | ✅ | ✅ |
| ETH  | ✅ uETH | ✅ | ✅ | ✅ |
| SOL  | ✅ uSOL | ✅ | ✅ | ✅ |
| HYPE | ✅ native | ✅ | ❌ | ✅ |
| AVAX | ✅ uAVAX | ✅ | ❌ | ✅ |
| ZEC  | ✅ uZEC | ✅ | ❌ | ✅ |
| XPL  | ✅ uXPL | ✅ | ❌ | ✅ |
| PURR | ✅ native | ✅ | ❌ | ❌ |
| LINK | ❌ | ✅ | ❌ | ✅ |
| DOGE | ❌ | ✅ | ❌ | ✅ |
| AAVE | ❌ | ✅ | ❌ | ✅ |

- HL spot у майоров — через **Unit-бридж** (`u<COIN>`); HYPE/PURR нативные.
- HL **без спота:** LINK (только `LINK0`-бридж, НЕ мапить на перп), DOGE (токен UDOGE
  есть, торгуемой пары нет), AAVE (только `AAVE0`-бридж).
- Aster spot — всего ~49 пар; из нашей вселенной только **BTC/ETH/SOL**.

### Где возможен unified (спот+перп на одной площадке)

| площадка | unified возможен | только perp (нужен внешний спот → decoupled) |
|---|---|---|
| **HL** | BTC, ETH, SOL, HYPE, AVAX, ZEC, XPL, PURR | LINK, DOGE, AAVE |
| **Aster** | BTC, ETH, SOL | HYPE, AVAX, LINK, DOGE, AAVE, ZEC, XPL |

---

## 2. Карта решений (7 рабочих монет)

| монета | лучший funding | LST / стейкинг | спот под long-ногу | → схема | сложность |
|--------|----------------|----------------|--------------------|---------|:--------:|
| **HYPE** | **HL 19.4%** (Aster 3.9) | kHYPE +2.2% | HL ✅ (нативный) | **unified на HL** | 🟢 низкая |
| **AVAX** | **Aster 10.5%** (HL 5.2) | sAVAX +4.5% | HL ✅ uAVAX, Aster ❌ | funding на Aster, perp-only → **decoupled** (спот HL/кошелёк) | 🔴 высокая |
| **SOL** | **Aster 6.1%** (HL 2.7) | jitoSOL +6.5% | HL ✅, Aster ✅ | unified на Aster; для стейкинга → decoupled jitoSOL | 🟡 средняя |
| **LINK** | **HL 11.2%** | ❌ нет | HL ❌, Aster ❌ | perp на HL, спота нигде → **decoupled, внешний спот** | 🔴 высокая |
| **ETH** | Aster 8.1% (HL 7.7) | wstETH +2.5% | HL ✅, Aster ✅ | unified (HL/Aster); стейкинг → decoupled wstETH | 🟡 средняя |
| **BTC** | **HL 9.2%** (Aster 8.1) | ❌ нет | HL ✅, Aster ✅ | **unified на HL** | 🟢 низкая |
| **DOGE** | Aster 7.9% (HL 7.3) | ❌ нет | HL ❌, Aster ❌ | спота нигде → **decoupled, внешний спот** | 🔴 высокая |

---

## 3. Как читать

**🟢 Бери и делай (unified, просто):**
- **HYPE** — звезда: топ-funding 19.4% + стейкинг, только на HL. Уже в живом сете.
- **BTC** — funding и спот на HL, unified из коробки. Стейкинга нет — чистый funding-play.

**🟡 Двойной доход, но за стейкинг платишь сложностью:**
- **SOL** — funding вдвое лучше на Aster (6.1 vs 2.7) + jitoSOL +6.5%. Максимум дохода =
  Aster-перп + jitoSOL-спот = decoupled.
- **ETH** — funding почти равный, unified на обеих; wstETH +2.5% только через decoupled.

**🔴 Дорого в обслуживании — взвешивать:**
- **AVAX** — звезда по доходу (10.5% funding + 4.5% sAVAX), НО лучший funding на Aster,
  где спота нет → только decoupled. «Вкусная, но хлопотная».
- **LINK / DOGE** — спота нет **нигде** в наших венью → обязательно внешний спот
  (on-chain/CEX). LINK без стейкинга, DOGE — слабейший по всему. Кандидаты на
  «не связываться», пока капитал маленький.

---

## 4. Сухой остаток

- **Звёзды двойного дохода:** HYPE, AVAX, SOL.
- **Простые unified:** HYPE, BTC (и ETH/SOL, если без стейкинга).
- **Decoupled неизбежен** для: AVAX (ради funding), SOL/ETH (ради стейкинга),
  LINK/DOGE (нет спота вообще).
- **Aster как unified-площадка** полезен только для ETH/SOL; для AVAX/DOGE — чисто
  perp-источник funding.
- **Живой прод-сет (`strategies.coins`):** BTC, ETH, SOL, HYPE, PURR — всё unified на HL.

---

## Caveats

- Funding — **cold-окно** (2025-01→2026-04), самый слабый период; в hot-режиме ставки
  были ×2-3. Лидерство венью по монете дрейфует во времени → для live нужен роутинг по
  скользящему сигналу, а не статический снимок.
- Стейкинг-APR консервативные; LST несут депег-риск (см. `research/staking/DEPEG_HISTORY.md`).
- Ликвидность Aster-спота и HL Unit-спота тоньше нативных сетей/крупных CEX — на $50k+
  проскальзывание ест edge.
- Прод-таблица `markets.has_spot` приведена в соответствие с HL API 2026-06-05
  (LINK/DOGE/AAVE → 0). Это метаданные, не функциональный гейт входа.
- Не юридическая консультация по доступу к площадкам (Aster: non-custodial, KYC
  дискреционный, Беларусь не в бан-листе — детали в `[[project_multi_exchange]]` memory).
