# Architecture refactor (Phase F, 2026-05-27)

После incident'а 2026-05-22 проведён архитектурный обзор: см.
[architecture-review.md](architecture-review.md). Документ фиксирует
findings, целевую архитектуру (universal `ExchangeAdapter` + `PortfolioService`
+ tick pipeline) и поэтапный план миграции (8 фаз).

Task-specs по фазам:

- [task-f1-portfolio-domain.md](task-f1-portfolio-domain.md) — domain
  value objects + PortfolioService; убирает accumulators со стратегии.
- [task-f2-exchange-adapter.md](task-f2-exchange-adapter.md) — universal
  `ExchangeAdapter` Protocol + `HyperliquidAdapter` consolidation +
  `DryRunAdapterGuard`. Зависит от F1.
- [task-f14-cutover.md](task-f14-cutover.md) — F1.4 cutover: Engine.equity
  через portfolio_service, удаление strategy accumulators, Position.state
  миграция. Зависит от F1.1-F1.3d + F2.1-F2.5 (foundation landed).
- F3 (Strategy → ExchangeAdapter), F4 (Engine pipeline / TickComponent),
  F5 (server slim-down), F6 (strategy dedup), F7 (DB recorder breakup),
  F8 (multi-exchange) — task-spec'и будут написаны по мере приближения.

Phase F0 (быстрый dry-run patch в стратегии) пропускается: prod engine
остаётся off до завершения F1+F2, после чего DryRunAdapterGuard даёт
полноценную защиту на уровне адаптера.

База данных будет пересоздана с нуля — старые миграции в репозитории
остаются для истории, F1 пишет новую финальную initial-схему.

---

# Margin-aware backtest tasks (Phase A)

Three executor-ready tasks built per `instructions/claude_task_planner_prompt.md`. The change request:

> Strategy is delta-neutral funding harvest (long spot + short perp). Current backtest does not reserve any margin for the perp short — it pays only the perp fee at open. That's unrealistic: on Hyperliquid, perp shorts require initial margin, and spot/perp wallets are separate. Add a per-coin margin model (with cross-margin perp wallet at portfolio level), enabling honest capital-efficiency and risk metrics.

# Recommended execution order

1. **Task 001** — `engine.py` margin model (single-coin level). Backwards-compatible: all existing scripts must keep working identically when the new params are omitted.
2. **Task 002** — Portfolio simulator (`portfolio_margin.py`) that uses cross-margin perp wallet across multiple positions, K-slot concurrency, and budget cap.
3. **Task 003** — Sweep wrapper + `MARGIN_ANALYSIS.md` writeup.

Tasks 002 and 003 can be done before 001 in principle (the portfolio script can self-contain the margin logic), but doing 001 first is cleaner.

# Review checklist

When reviewing the produced diff for each task:

- [ ] **Scope**: only files listed in "Files likely involved" were touched.
- [ ] **Backwards compat (Task 001)**: `uv run python research/backtest_a.py` produces identical output to before. Verify via `simulate(df, ...)` vs `simulate(df, ..., per_coin_leverage=None)` byte-identical PnL.
- [ ] **Tests / verify script** runs and exits 0.
- [ ] **No new dependencies** added to `pyproject.toml`.
- [ ] **Style**: numpy/pandas, snake_case, plain `print()` (no logging framework in `research/`). Russian comments are OK where the existing code has them.
- [ ] **No emojis** in source.
- [ ] **Docstrings**: short, only where the WHY is non-obvious.
- [ ] **No invented numbers** in MARGIN_ANALYSIS.md — every cited metric must trace to a sweep CSV row.
- [ ] **No "fix me later"** / TODO comments.

# Suggested execution workflow (validated 2026-05-22)

Default delegation chain: Opus (planner) → Sonnet sub-agent (orchestrator, `run_in_background=true`) → opencode CLI → gemma4:26b (executor).

```bash
cd /Users/d/prj/funding-rate-arbitrage

# 1) Ensure clean git tree + dedicated worktree for the experiment
git status
git worktree add -b A2-gemma .claude/worktrees/A2-gemma main
ln -sfn /Users/d/prj/funding-rate-arbitrage/research/data \
        .claude/worktrees/A2-gemma/research/data

# 2) Permission setup (one-time): allow Bash for sub-agents
echo '{"permissions":{"allow":["Bash(*)"]}}' > .claude/settings.json

# 3) Planner (Opus) writes the spec into .ai/task-NNN-*.md and spawns a Sonnet
#    sub-agent (Agent tool, model=sonnet, run_in_background=true) with the spec
#    + an instruction "drive gemma chunk-by-chunk, sequential only, never parallel".

# 4) The sub-agent invokes gemma per chunk via inline-message opencode calls.
#    Example chunk command issued by the sub-agent:
cd /Users/d/prj/funding-rate-arbitrage/.claude/worktrees/A2-gemma
date +%s > /tmp/A2-cN.start
opencode run \
  "Edit research/portfolio_margin.py. Find the LAST line of the file. Append these EXACT lines after it (blank line first, then content). Use 4-space indentation: <BLOCK>. Only that. Verify: python3 -c 'import ast; ast.parse(open(\"research/portfolio_margin.py\").read())'." \
  --dangerously-skip-permissions \
  --model ollama/gemma4:26b \
  --agent build \
  --dir "$(pwd)" \
  > /tmp/A2-cN.log 2>&1
date +%s > /tmp/A2-cN.end

# 5) Between chunks the sub-agent ONLY runs:
#      python3 -c "import ast; ast.parse(open('<path>').read())"
#      uv run python <target script> 2>&1 | tail -N
#    Never starts a second opencode call before the first finishes.

# 6) Parent (Opus) reviews final state, runs full verification, then commits.
git diff
git diff --stat
```

Constraints (STRICT):
- ONE gemma task at a time. Never run two `opencode run ...` concurrently — ollama can only host one model effectively on a typical Mac.
- Per chunk: ≤80 lines added, ≤30 lines edited.
- Anchor strategy: prefer "append at end of file" for new builds; "replace this exact line with this block" for edits. AVOID "insert near X".
- If gemma produces broken structure on a chunk and retry also fails, STOP and report — do not silently fall back to Edit (defeats the experiment).

# Assumptions made while planning

- Period / data: existing `research/data/<COIN>.csv` and `<COIN>_1h.csv` are present and joinable on hourly timestamps for all 7 coins (BTC, ETH, SOL, AVAX, LINK, AAVE, DOGE).
- HL maintenance-margin ratios per coin are approximations (1% majors, 2.5% mid, 5% alt). Production code may use more exact values; for backtest this is close enough.
- `position_size_usdc` is uniform across coins by user decision; per-coin leverage encodes the differing IM requirements.
- `BUDGET_CAP_USD = $1000` for the sweep — a tractable scale; results extrapolate linearly to larger capital (modulo liquidity limits).
- Liquidation modelling is a simplification: when cross-margin `perp_equity < total_maintenance`, ALL open positions are force-closed and all perp_cash is lost. Real HL ADL would sometimes close fewer positions; this is conservative.
- `sUSDe ≈ 12% APR` baseline for comparison (current Ethena rate at time of writing — quote with that caveat in the analysis MD).
