# Альтернативы Hyperliquid — синтез (refresh 2026-06-19)

**Запрос:** не хранить все средства на HL. Найти DEX'ы под **FRAB** (funding-арбитраж) и
**XSMOM** (cross-sectional momentum). Свежие funding-доходности + perp-возможности под XSMOM.

**Что внутри папки:**
- `FRAB_FUNDING.md` + `frab_funding_current.csv` — свежие funding-ставки (trailing 90d/365d
  через сегодня) по HL / Aster / dYdX / Lighter (+Paradex snapshot), cadence-agnostic.
- `XSMOM_VENUES.md` + `xsmom_venue_metrics.csv` — пригодность 7 площадок под XSMOM
  (покрытие/шорт/taker/плечо + **глубина на тонких алтах** = решающая метрика).
- `probe_spot_availability.py` — спот-доступность FRAB-коинов (long-нога) по venue.
- `probe_*.py`, `fetch_all.py`, `finalize.py`, `raw/` — сбор данных.

Это обновление июньского cold-скаута (`../CROSS_VENUE_SYNTHESIS.md`, `../VENUE_COIN_MAP.md`),
который мерил окно 2025-01→2026-04. Здесь — живые данные на 2026-06-19.

---

## Ключевая развилка: FRAB и XSMOM хотят РАЗНОГО от площадки

| | FRAB (delta-neutral funding harvest) | XSMOM (leveraged long/short momentum) |
|---|---|---|
| Нужен спот? | **ДА** — long-нога в споте (unified) или внешний спот (decoupled) | **НЕТ** — чисто perp, лонг+шорт |
| Главный критерий venue | высокий funding + наличие спота под коин | **глубина стакана на тонких алтах** |
| Фандинг | это **альфа** (зарабатываем) | это **кост/кредит** (нетто ≈ спред терцилей) |
| Что решает выбор | funding-лидерство (дрейфует) + unified vs decoupled | thin-alt liquidity, taker fee |

→ Поэтому «лучшая вторая площадка» для FRAB и для XSMOM — **разные**.

---

## 1. FRAB — свежий funding + спот

### 1a. Best-venue-per-coin (trailing 90d, актуальный режим)

| coin | venue | 90d % | сдвиг vs cold-скаут |
|------|-------|-------|---------------------|
| BTC  | **HL** | 2.62 | майоры сжались везде |
| ETH  | **HL** | 3.91 | dYdX/Lighter ушли вниз |
| SOL  | — | все ≤0 | **carry на SOL умер во всех venue** (был гл. аргумент Aster) |
| HYPE | **HL** | 8.88 | родной, единственный с положит. 90d |
| AVAX | **Aster** | 10.63 | **самая устойчивая cross-venue находка** (cold→now) |
| LINK | **Aster** | 10.71 | HL 9.6 рядом; оба низкий neg |
| DOGE | **Lighter** | 7.63 | Lighter — новый лидер |
| ZEC  | **dYdX** | 15.59 | единственный заметно положит. |
| XPL  | **Aster** | 10.33 | Lighter 365d=32%, но 90d просел |

Полные таблицы 90d/365d + neg-hours + дрейф — в `FRAB_FUNDING.md`.

### 1b. Спот-доступность (long-нога FRAB) — `probe_spot_availability.py`, live 2026-06-19

| venue | BTC | ETH | SOL | HYPE | AVAX | LINK | DOGE | ZEC | XPL | роль для FRAB |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|---|
| **HL** | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ⚠️ | ✅ | ✅ | unified baseline (майоры = Unit-бридж) |
| **Backpack** | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | **сильнейший unified-альтернативщик (8/9)** |
| **Aster** | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | в основном perp-источник funding (спот 3/9) |
| dYdX/Lighter/Paradex/edgeX/Vertex | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | **perp-only → только decoupled** (внешний спот + мост) |

⚠️ HL DOGE/LINK: токен в списке есть, но торгуемой USDC-пары по аудиту 2026-06-05 нет
(`UDOGE` без активной пары, LINK — только `LINK0`-бридж, НЕ мапить на перп). Считать спота
по DOGE/LINK на HL — нет.

### 1c. Вывод FRAB

- **Майоры (BTC/ETH/HYPE) вернулись на HL** — мультивенью-edge на них испарился (compression).
- **Aster держит AVAX/LINK** — единственный устойчивый funding-диверсификатор cold→now,
  но спота под них нет (AVAX/LINK на Aster ❌) → **decoupled** (Aster perp + спот снаружи).
- **Lighter (DOGE/XPL) и dYdX (ZEC)** — новые точечные funding-источники, но **спота нет
  нигде** → тоже только decoupled.
- **Backpack — переоценка роли.** В cold-скауте забракован как funding-роутер (ставки не
  лучшие). Но по **споту он перекрывает 8/9 FRAB-коинов** (vs HL 8/9, но с РЕАЛЬНЫМИ книгами,
  не Unit-бриджем; покрывает LINK, которого у HL нет). Его честная роль — **CEX-like
  unified-площадка №2** для дельта-нейтрали, а не «лучший funding».

**FRAB-роадмап:** HL остаётся ядром (funding на майорах + unified). Первый шаг
диверсификации спот-кастоди (если цель «не всё на HL») — **Backpack как второй unified-дом**
для BTC/ETH/SOL/HYPE/ZEC/XPL (+ LINK/DOGE, которых на HL спота нет). Aster — только если
готовы на decoupled ради AVAX/LINK funding-премии (~+4–5пп на этих коинах).

---

## 2. XSMOM — пригодность площадок

Покрытие 32-коинов почти у всех ок (HL/Aster/dYdX 32/32, Lighter/edgeX 30/32, Paradex 29/32,
Backpack 27/32), API и шорт — у всех. **Решает глубина стакана на тонких алтах** (CRV, JTO,
JUP, PYTH, ZRO, EIGEN, PENDLE, WLD, INJ, TAO) — «листят, но книги нет».

| venue | 32/32 | thin-alt медиана vol / спред | taker | XSMOM-вердикт |
|---|---|---|---|---|
| **Hyperliquid** | 32/32 | $3.6M / 4.7bps · 9/10 алтов >$1M | 4.5bps | эталон, ядро |
| **Lighter** | 30/32 | $155k / 14bps · живые книги | **0** | **лучший #2** (нулевой taker критичен для недельного taker-кросса; нужен zk-SDK, не REST) |
| **Backpack** | 27/32 | $393k / **0.9bps** | 7bps | годен на **урезанной ~27-коин** вселенной; дорогой taker |
| Aster | 32/32 | $62k / 11bps | 3.5bps | ❌ объём только в WLD/TAO, остальное пыль |
| edgeX | 30/32 | ~$0 / 518bps | 3.8bps | ❌ list-but-no-book |
| Paradex | 29/32 | ~$1k / 302bps | 3.0bps | ❌ мёртвые книги + 3 коина нет |
| dYdX v4 | 32/32 | ~$1k / 93bps | 5.0bps | ❌ живы только мажоры |

Фандинг как carry-drag (HL): медиана |rate| ~11%/год, p90 ~28%, max ~43% — параметр
стратегии (штрафовать выбор терциля на ожидаемый фандинг), не блокер площадки.

**Вывод XSMOM:** единственный реальный «второй дом» — **Lighter** (живые двусторонние книги
на алтах + нулевой taker; цена — интеграция zk-SDK). **Backpack** — на обрезанной вселенной.
Все appchain-perp (dYdX/Paradex/edgeX) и Aster проваливают тонкие алты.

---

## 3. Сухой остаток — что куда

| | Ядро | Лучший #2 (свежая рекомендация) | Зачем |
|---|---|---|---|
| **FRAB** | Hyperliquid | **Backpack** (unified spot 8/9, вкл. LINK) | диверсификация спот-кастоди без мостов; Aster — точечно под AVAX/LINK funding (decoupled) |
| **XSMOM** | Hyperliquid | **Lighter** (0 taker, живые алт-книги) | разный matching/ликвидпул ⟂ HL; нулевой fee на недельном ребалансе |

**Общая картина:** «не всё на HL» — выполнимо, но по-разному для двух стратегий. HL остаётся
ядром обеих (по funding на майорах и по ликвидности алтов он вне конкуренции). Реальные
диверсификаторы — **Backpack** (спот-кастоди для FRAB) и **Lighter** (perp-ликвидность для
XSMOM). Aster — нишевый funding-источник под AVAX/LINK, только в decoupled. Все остальные
скаут-кандидаты (dYdX/Paradex/edgeX/Vertex/Drift) на свежих данных не проходят: либо funding
ушёл в анти-сигнал (dYdX майоры), либо тонкие алты мертвы.

**Caveat:** funding-лидерство дрейфует (SOL-edge Aster испарился за квартал) → для live нужен
скользящий роутинг по сигналу, не статический снимок. Числа — снимок на 2026-06-19;
Paradex — только snapshot (полный history-семплинг вне бюджета, TODO).
