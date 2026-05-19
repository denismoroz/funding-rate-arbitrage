import re
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"

_ETH_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


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

    paper_extra_slip_bps: float = Field(default=2.0)

    strategy_name: str = Field(default="strategy_a")  # "strategy_a" | "two_phase_dynamic"
    strategy_params_json: str = Field(default="")     # optional JSON override of default params

    log_level: str = Field(default="INFO")

    hl_network: Literal["paper", "testnet", "mainnet"] = Field(default="paper")
    hl_private_key: SecretStr | None = Field(default=None)
    hl_account_address: str | None = Field(default=None)
    # Comma-separated coin list in env (e.g. "PURR,HYPE" or "BTC,ETH,SOL,AVAX,LINK,AAVE")
    # Empty default = use the server.py-side DEFAULT_COINS for paper mode.
    hl_universe: str = Field(default="")
    # Risk caps applied when hl_network != "paper"
    hl_max_open_positions: int = Field(default=5)
    hl_position_size_usd: float = Field(default=10.0)

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
        if self.hl_network in ("testnet", "mainnet"):
            if self.hl_private_key is None or self.hl_account_address is None:
                raise ValueError(
                    f"hl_private_key and hl_account_address are required when hl_network={self.hl_network!r}"
                )
        return self

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
