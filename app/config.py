from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "football-intel-service"
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    demo_mode: bool = True

    request_timeout_seconds: float = 10.0
    cache_ttl_seconds: int = 300

    football_data_api_key: str = ""
    football_data_base_url: str = "https://api.football-data.org/v4"

    the_odds_api_key: str = ""
    the_odds_base_url: str = "https://api.the-odds-api.com/v4"

    goalserve_api_key: str = ""
    goalserve_base_url: str = "https://www.goalserve.com/getfeed"

    zgzcw_enabled: bool = True
    zgzcw_bjzs_url: str = "https://plzx.zgzcw.com/bjzs/"
    zgzcw_live_url: str = "http://live.zgzcw.com/jz/"

    njstats_enabled: bool = True
    njstats_api_key: str = ""
    njstats_base_url: str = "https://www.njstats.cn"

    @property
    def enabled_sources(self) -> dict[str, bool]:
        return {
            "football_data": self.demo_mode or bool(self.football_data_api_key),
            "the_odds": self.demo_mode or bool(self.the_odds_api_key),
            "goalserve": self.demo_mode or bool(self.goalserve_api_key),
            "wubai": True,
            "zgzcw": self.zgzcw_enabled,
            "njstats": self.njstats_enabled,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
