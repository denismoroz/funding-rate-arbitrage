import json
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"

_ETH_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TICKER_RE = re.compile(r"^[A-Z]{3,5}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="FRAB_",
        extra="ignore",
    )

    data_dir: Path = Field(default=DEFAULT_DATA_DIR)
    db_url: str = Field(default="")

    hl_api_url: str = Field(default="https://api.hyperliquid.xyz/info")
    hl_request_timeout_s: float = Field(default=10.0)
    hl_min_request_interval_ms: int = Field(default=200)

    strategy_name: str = Field(default="strategy_a")  # "strategy_a" | "two_phase_dynamic"
    strategy_params_json: str = Field(default="")     # optional JSON override of default params

    dry_run: bool = Field(default=False)

    log_level: str = Field(default="INFO")

    hl_network: Literal["testnet", "mainnet"] = Field(default="mainnet")
    hl_private_key: SecretStr | None = Field(default=None)
    hl_account_address: str | None = Field(default=None)
    # Comma-separated coin list in env (e.g. "PURR,HYPE" or "BTC,ETH,SOL,AVAX,LINK,AAVE")
    # Empty default = use the server.py-side DEFAULT_COINS for the engine universe override.
    hl_universe: str = Field(default="")
    # Risk caps for live engine
    hl_max_open_positions: int = Field(default=5)
    hl_position_size_usd: float = Field(default=10.0)
    hl_live_slippage: float = Field(default=0.01)

    # --- Margin-policy settings (Phase B1) ---
    # Per-coin params JSON. Empty string = use legacy hl_position_size_usd uniform sizing.
    # When non-empty, expected shape:
    #   {"BTC": {"position_size_usd": 100.0, "leverage": 20, "maint_ratio": 0.01}, ...}
    per_coin_params_json: str = Field(default="")
    # Total budget the strategy will manage.
    budget_cap_usd: float = Field(default=1000.0, gt=0)
    # Multiplier over initial margin kept as buffer.
    margin_buffer_x: float = Field(default=3.0, ge=1.0, le=10.0)
    # margin_ratio threshold below which a top-up triggers.
    top_up_trigger: float = Field(default=2.0)
    # Account-wide ratio at or below which weakest FP is force-closed.
    forced_close_trigger: float = Field(default=1.5)
    # Target margin_ratio after top-up.
    healthy_ratio: float = Field(default=3.0)

    @field_validator("hl_account_address")
    @classmethod
    def _validate_eth_address(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not _ETH_ADDR_RE.match(v):
            raise ValueError(
                f"hl_account_address must be a 42-char hex Ethereum address starting with 0x, got: {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _require_credentials_for_live(self) -> "Settings":
        if self.hl_private_key is None or self.hl_account_address is None:
            raise ValueError(
                f"hl_private_key and hl_account_address are required when hl_network={self.hl_network!r}"
            )
        return self

    @model_validator(mode="after")
    def _validate_margin_policy(self) -> "Settings":
        """Проверка cross-field инвариантов маржинальной политики."""
        if self.top_up_trigger <= 1.0:
            raise ValueError(
                f"top_up_trigger must be > 1.0, got {self.top_up_trigger}"
            )
        if self.top_up_trigger >= self.healthy_ratio:
            raise ValueError(
                f"top_up_trigger ({self.top_up_trigger}) must be < healthy_ratio ({self.healthy_ratio})"
            )
        if not (1.0 < self.forced_close_trigger < self.top_up_trigger <= self.healthy_ratio):
            raise ValueError(
                f"thresholds must satisfy 1.0 < forced_close_trigger ({self.forced_close_trigger}) "
                f"< top_up_trigger ({self.top_up_trigger}) <= healthy_ratio ({self.healthy_ratio})"
            )
        return self

    def per_coin_params(self) -> "dict[str, dict] | None":
        """Parse per_coin_params_json into a validated dict, or None if empty.

        Returns None when per_coin_params_json is empty (legacy uniform-sizing mode).
        Raises ValueError on any parse or validation error.

        Expected shape::

            {
                "BTC": {"position_size_usd": 100.0, "leverage": 20, "maint_ratio": 0.01},
                ...
            }
        """
        raw = self.per_coin_params_json.strip()
        if not raw:
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"per_coin_params_json is not valid JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise ValueError("per_coin_params_json must be a JSON object (dict)")

        result: dict[str, dict] = {}
        sizes_present: list[bool] = []
        for ticker, params in data.items():
            if not isinstance(ticker, str) or not _TICKER_RE.match(ticker):
                raise ValueError(
                    f"per_coin_params_json key {ticker!r} must be an uppercase 3-5 letter ticker"
                )
            if not isinstance(params, dict):
                raise ValueError(
                    f"per_coin_params_json[{ticker!r}] must be a dict, got {type(params).__name__}"
                )
            # leverage + maint_ratio are required; position_size_usd is optional
            # (auto-derived from budget_cap/K/buffer when omitted for all coins).
            for required_key in ("leverage", "maint_ratio"):
                if required_key not in params:
                    raise ValueError(
                        f"per_coin_params_json[{ticker!r}] missing required key {required_key!r}"
                    )
            leverage = params["leverage"]
            maint_ratio = params["maint_ratio"]
            if not isinstance(leverage, int) or not (1 <= leverage <= 50):
                raise ValueError(
                    f"per_coin_params_json[{ticker!r}].leverage must be int in [1, 50], got {leverage!r}"
                )
            if not isinstance(maint_ratio, (int, float)) or not (0 < maint_ratio < 0.5):
                raise ValueError(
                    f"per_coin_params_json[{ticker!r}].maint_ratio must be float in (0, 0.5), got {maint_ratio!r}"
                )

            entry: dict = {
                "leverage": int(leverage),
                "maint_ratio": float(maint_ratio),
            }
            if "position_size_usd" in params:
                pos_size = params["position_size_usd"]
                if not isinstance(pos_size, (int, float)) or pos_size <= 0:
                    raise ValueError(
                        f"per_coin_params_json[{ticker!r}].position_size_usd must be float > 0, "
                        f"got {pos_size!r}"
                    )
                entry["position_size_usd"] = float(pos_size)
                sizes_present.append(True)
            else:
                sizes_present.append(False)
            result[ticker] = entry

        # Mixed mode (some coins with position_size_usd, some without) is ambiguous
        # — fail loud so the user picks one mode.
        if any(sizes_present) and not all(sizes_present):
            missing = [t for t, p in result.items() if "position_size_usd" not in p]
            raise ValueError(
                "per_coin_params_json: position_size_usd must be set for all coins "
                f"or none (mixed mode unsupported). Missing in: {missing}"
            )
        return result

    def get_coin_spec(self, coin: str) -> "CoinMarginSpec":
        from frab.constants import (
            CoinMarginSpec, RESEARCH_LEVERAGE, RESEARCH_MAINT_RATIO,
            FALLBACK_LEVERAGE, FALLBACK_MAINT_RATIO,
        )
        overrides = self.per_coin_params() or {}
        if coin in overrides:
            spec = overrides[coin]
            return CoinMarginSpec(
                leverage=spec["leverage"],
                maint_ratio=spec["maint_ratio"],
            )
        return CoinMarginSpec(
            leverage=RESEARCH_LEVERAGE.get(coin, FALLBACK_LEVERAGE),
            maint_ratio=RESEARCH_MAINT_RATIO.get(coin, FALLBACK_MAINT_RATIO),
        )

    def universe_tuple(self) -> tuple[str, ...]:
        """Parse hl_universe env string into tuple of coin names. Empty → ()."""
        raw = self.hl_universe.strip()
        if not raw:
            return ()
        return tuple(c.strip().upper() for c in raw.split(",") if c.strip())

    def model_post_init(self, __context: object) -> None:
        if not self.db_url:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            object.__setattr__(
                self,
                "db_url",
                f"sqlite+aiosqlite:///{self.data_dir / 'frab.db'}",
            )


def get_settings() -> Settings:
    return Settings()
