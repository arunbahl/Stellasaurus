"""Typed configuration (pydantic-settings).

Precedence (highest first): STELLA_* environment variables, then the TOML file
(``config/default.toml`` by default), then the field defaults below.

Money-valued settings are integer **micro-USD** (see ``common.money``). Fields
flagged ``[UI]`` become runtime-editable from the dashboard in Phase 4; in Phase 1
they are read-only defaults surfaced on the dashboard.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

_DEFAULT_TOML = Path(os.environ.get("STELLA_CONFIG_FILE", "config/default.toml"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="STELLA_",
        toml_file=_DEFAULT_TOML,
        env_file=".env",  # local, gitignored; see .env.example for the template
        extra="ignore",
    )

    # --- storage / seed ---
    db_path: Path = Path("data/stella.db")
    seed_path: Path = Path("seeds/pairs.seed.yaml")

    # --- timing ---
    catalog_refresh_seconds: int = 300
    book_staleness_ms: int = 2000
    rest_poll_interval_ms: int = 1000
    dashboard_push_interval_ms: int = 250

    # --- venue endpoints ---
    # api.kalshi.com does not resolve; api.elections.kalshi.com is the working host.
    kalshi_rest_base: str = "https://api.elections.kalshi.com/trade-api/v2"
    kalshi_ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    poly_rest_base: str = "https://api.polymarket.us"
    poly_ws_url: str = "wss://api.polymarket.us/v1/ws/markets"

    # --- subscription limits ---
    poly_markets_per_conn: int = 100
    kalshi_max_ws_conns: int = 5

    # --- Kalshi catalog pagination (gentle, to avoid 429 rate limits) ---
    kalshi_catalog_page_size: int = 200
    kalshi_catalog_max_pages: int = 8
    kalshi_catalog_page_pause_ms: int = 300
    # Series-rotation catalog: the global /markets list is ~99.6% multivariate
    # parlay collections, so the catalog is built by sweeping ALL series (no
    # category filtering — every series is visited) in budget-paced chunks per
    # cycle, with a rotation cursor so successive cycles cover the full set.
    # The ONLY exclusion is structural: parlay collections (KXMVE*) cannot form
    # a clean two-leg locked pair regardless of topic.
    kalshi_series_per_cycle: int = 300  # request budget per catalog cycle
    # Bootstrap: at startup, loop rotation chunks back-to-back (Kalshi only)
    # until one full series sweep completes, instead of waiting ~6h of cycles.
    kalshi_bootstrap_sweep: bool = True

    # --- feed re-planning ---
    subscription_check_seconds: int = 30  # registry-change poll for feed re-plan

    # --- near-resolution priority sweep (in-game readiness) ---
    # Game-day markets list hours before start; the slow full rotation may not
    # revisit their series in time. This fast cycle re-syncs any Kalshi series
    # with markets resolving inside the window + a fresh Polymarket pull, then
    # runs a STRUCTURED-ONLY pairing pass (no LLM spend) so pairs are verified
    # and streaming before the game begins.
    priority_sync_seconds: int = 120
    priority_window_hours: int = 24

    # --- Kalshi environment: "prod" or "demo" (fake-money, separate keys).
    # Demo is the validation ground for the Phase-6 order gateways.
    kalshi_env: str = "prod"
    kalshi_demo_rest_base: str = "https://demo-api.kalshi.co/trade-api/v2"
    kalshi_demo_ws_url: str = "wss://demo-api.kalshi.co/trade-api/ws/v2"

    # --- dashboard ---
    # Reachability. "tailnet" (default) binds loopback + this host's Tailscale IP
    # so only localhost and tailnet peers can reach it (NOT the LAN). "localhost"
    # binds loopback only; "all" binds 0.0.0.0 (explicit LAN exposure).
    dashboard_expose: str = "tailnet"
    dashboard_host: str | None = None  # explicit single-host override (wins if set)
    dashboard_port: int = 8770

    # --- credentials (optional in Phase 1 -> keyless public mode if absent) ---
    kalshi_api_key_id: str | None = None
    kalshi_private_key_path: Path | None = None
    poly_access_key: str | None = None
    poly_ed25519_seed: str | None = None  # base64 32-byte Ed25519 seed

    # --- [UI]-settable later (Phase 4); read-only defaults now ---
    theta_micros: int = 20_000  # [UI] $0.02 min net edge per pair
    hurdle: float = 0.10  # [UI] min annualized return
    target_size_default: int = 10  # [UI]
    max_bet_value_micros: int = 50_000_000  # [UI] $50 per single entry
    max_bet_value_ceiling_micros: int = 500_000_000  # non-UI ceiling ($500)
    max_aggregate_exposure_micros: int = 5_000_000_000  # [UI]
    max_open_pairs: int = 20  # [UI]
    max_committed_capital_micros: int = 5_000_000_000  # [UI]
    min_t_days: float = 0.5

    # --- fee params (used Phase 3; cached now) ---
    kalshi_fee_multiplier_default: float = 0.07
    kalshi_balance_precision_micros: int = 100  # $0.0001 (validated live: fee 0.0167)
    poly_taker_fee_coefficient: float = 0.06  # quadratic: fee = coeff*C*p*(1-p)
    poly_maker_fee_coefficient: float = -0.0125  # negative = maker rebate
    fee_divergence_tolerance_micros: int = 10_000
    slippage_tolerance_bips: int = 50
    fee_param_refresh_seconds: int = 3600
    execution_policy_default: str = "TAKER_BOTH"

    # --- equivalence LLM (Phase 2) ---
    # The LLM client/model is defined in baml_src/clients.baml and reads
    # FIREWORKS_LLM_ENDPOINT + FIREWORKS_API_KEY_BAML from the environment, so no
    # LLM client settings live here.

    # --- pairing loop (Phase 2) ---
    pairing_enabled: bool = True  # runs only if the LLM is configured
    pairing_refresh_seconds: int = 600
    pairing_max_llm_calls: int = 10  # per cycle — bounds LLM spend
    pairing_min_score: float = 0.35  # candidate token-overlap floor
    pairing_llm_concurrency: int = 8  # concurrent equivalence evals per cycle

    # --- safety gate ---
    live_trading_enabled: bool = False
    # Re-entry cooldown after a non-hedged outcome (UNWOUND/FAILED) — damps
    # loss-churn on a chronically half-filling pair.
    reentry_cooldown_ms: int = 30_000
    # In-flight reservation TTL: a non-held reservation older than this is
    # presumed orphaned (executor died before recording) and purged. Far above
    # real execution (~2s); HANGING holds are exempt.
    reservation_ttl_ms: int = 30_000

    @property
    def kalshi_credentials_present(self) -> bool:
        return bool(self.kalshi_api_key_id and self.kalshi_private_key_path)

    @property
    def poly_credentials_present(self) -> bool:
        return bool(self.poly_access_key and self.poly_ed25519_seed)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence: process env > .env file > config TOML > field defaults.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
        )


def load_settings() -> Settings:
    return Settings()
