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
    # Timeout for the HL SDK (Info/Exchange) requests path. Higher than the httpx
    # read timeout because it also covers write calls (orders/cancels/transfers),
    # and because SDK calls have NO default timeout (=> infinite hang on a dead
    # socket during a connection-reset storm, which freezes the awaiting EngineLoop).
    hl_sdk_timeout_s: float = Field(default=30.0)
    hl_min_request_interval_ms: int = Field(default=200)

    strategy_name: str = Field(default="strategy_a")  # "strategy_a" | "two_phase_dynamic"
    strategy_params_json: str = Field(default="")     # optional JSON override of default params

    dry_run: bool = Field(default=False)

    log_level: str = Field(default="INFO")

    hl_network: Literal["testnet", "mainnet"] = Field(default="mainnet")
    hl_private_key: SecretStr | None = Field(default=None)
    hl_account_address: str | None = Field(default=None)
    # Risk caps for live engine
    hl_max_open_positions: int = Field(default=5)
    hl_position_size_usd: float = Field(default=10.0)
    hl_live_slippage: float = Field(default=0.01)

    # --- Margin-policy settings (Phase B1) ---
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

    # --- XSMOM credentials (secrets only; universe/budget/leverage are strategy
    #     params edited via the UI → stored in the xsmom Strategy row's params_json) ---
    xsmom_hl_private_key: SecretStr | None = Field(default=None)
    xsmom_hl_account_address: str | None = Field(default=None)

    # --- Local-mode flag ---
    local_mode: bool = Field(default=False)

    @field_validator("hl_account_address", "xsmom_hl_account_address")
    @classmethod
    def _validate_eth_address(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not _ETH_ADDR_RE.match(v):
            raise ValueError(
                f"account_address must be a 42-char hex Ethereum address starting with 0x, got: {v!r}"
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

    def has_xsmom_credentials(self) -> bool:
        """True when both xsmom HL credentials are configured."""
        return self.xsmom_hl_private_key is not None and self.xsmom_hl_account_address is not None

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
