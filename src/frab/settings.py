from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"


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
