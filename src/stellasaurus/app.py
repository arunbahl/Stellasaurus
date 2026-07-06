"""Composition root: wire config -> db -> hot state -> venues -> background -> control.

Run with: ``python -m stellasaurus.app`` (or the ``stellasaurus`` console script).

Single asyncio event loop owns everything (DESIGN §5): two market-data feeds,
catalog sync, registry refresh, and the FastAPI dashboard. The hot path reads only
in-memory snapshots; the background plane pushes into them out of band.
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import uvicorn
from dotenv import load_dotenv

from stellasaurus.background.catalog_sync import CatalogSync
from stellasaurus.background.equivalence import EquivalenceEngine
from stellasaurus.background.pairing import PairingLoop
from stellasaurus.background.registry_loader import RegistryLoader
from stellasaurus.background.scheduler import TaskSupervisor
from stellasaurus.background.subscription_mgr import SubscriptionManager
from stellasaurus.common.config import Settings, load_settings
from stellasaurus.common.logging import configure_logging, get_logger
from stellasaurus.control.app import create_app
from stellasaurus.control.net import resolve_bind_hosts
from stellasaurus.control.readmodel import ReadModel
from stellasaurus.hot_path.ingest import BookStore
from stellasaurus.hot_path.snapshot import LimitsSnapshot, RegistrySnapshot
from stellasaurus.hot_path.state import HotStateStore
from stellasaurus.storage.audit_repo import AuditRepo
from stellasaurus.storage.db import Database
from stellasaurus.storage.markets_repo import MarketsRepo
from stellasaurus.storage.registry_repo import RegistryRepo
from stellasaurus.venues.factory import venue_clients

_log = get_logger("app")


def _limits_from_settings(s: Settings) -> LimitsSnapshot:
    return LimitsSnapshot(
        version=1,
        halted=not s.live_trading_enabled,  # Phase 1: trading disabled by definition
        theta_micros=s.theta_micros,
        hurdle=s.hurdle,
        target_size_default=s.target_size_default,
        max_bet_value_micros=s.max_bet_value_micros,
        max_bet_value_ceiling_micros=s.max_bet_value_ceiling_micros,
        max_aggregate_exposure_micros=s.max_aggregate_exposure_micros,
        max_open_pairs=s.max_open_pairs,
        max_committed_capital_micros=s.max_committed_capital_micros,
        min_t_days=s.min_t_days,
    )


async def run(settings: Settings | None = None) -> None:
    # Load .env into the process environment so the BAML LLM client (which reads
    # FIREWORKS_* directly from os.environ) and STELLA_ settings both see it.
    load_dotenv()
    settings = settings or load_settings()
    configure_logging()
    _log.info(
        "starting",
        kalshi_creds=settings.kalshi_credentials_present,
        poly_creds=settings.poly_credentials_present,
        live_trading=settings.live_trading_enabled,
    )

    # --- storage ---
    db = Database(settings.db_path)
    db.migrate()
    markets_repo = MarketsRepo(db)
    registry_repo = RegistryRepo(db)
    audit_repo = AuditRepo(db)

    # --- hot state ---
    store = HotStateStore(
        registry=RegistrySnapshot.empty(),
        limits=_limits_from_settings(settings),
        book_staleness_ms=settings.book_staleness_ms,
    )
    book_store = BookStore(store)

    async with httpx.AsyncClient(timeout=10.0) as http:
        clients = venue_clients(settings, http)

        loader = RegistryLoader(
            seed_path=settings.seed_path,
            registry_repo=registry_repo,
            markets_repo=markets_repo,
            audit_repo=audit_repo,
            store=store,
        )
        catalog = CatalogSync(
            clients=clients,
            markets_repo=markets_repo,
            registry_repo=registry_repo,
            audit_repo=audit_repo,
            on_catalog_updated=loader.load_seed,
        )
        sub_mgr = SubscriptionManager(
            settings=settings, http=http, store=store, book_store=book_store
        )

        # Initial load: seed (pairs STALE), then resolve each seeded pair's two
        # markets DIRECTLY (fast, targeted — no full catalog crawl needed to
        # verify; a leg whose venue is unreachable just stays STALE).
        loader.load_seed()
        await loader.resolve_seed_markets(clients)

        # Plan + run feeds for the now-VERIFIED pairs.
        planned = sub_mgr.plan()

        # --- read model ---
        read_model = ReadModel(store)
        read_model.feed_stats_provider = sub_mgr.feed_stats
        read_model.catalog_stats_provider = lambda: {
            "counts": catalog.last_counts,
            "last_sync_ms": catalog.last_sync_ms,
        }
        web = create_app(read_model, push_interval_ms=settings.dashboard_push_interval_ms)

        # --- supervise ---
        supervisor = TaskSupervisor()
        for p in planned:
            supervisor.supervise(f"feed:{p.feed.stats.venue.value}", p.runner)
        supervisor.run_periodic(
            "catalog_sync", settings.catalog_refresh_seconds, catalog.sync_once
        )

        # Phase 2 pairing loop: candidates -> LLM verdicts -> registry (source=LLM).
        engine = EquivalenceEngine()
        if settings.pairing_enabled and engine.configured:
            pairing = PairingLoop(
                clients=clients,
                engine=engine,
                registry_repo=registry_repo,
                audit_repo=audit_repo,
                publish=loader.publish,
                max_llm_calls=settings.pairing_max_llm_calls,
                min_score=settings.pairing_min_score,
            )

            async def pairing_cycle() -> None:
                await pairing.run_once()

            supervisor.run_periodic(
                "pairing", settings.pairing_refresh_seconds, pairing_cycle
            )
        else:
            _log.info(
                "pairing_disabled",
                enabled=settings.pairing_enabled,
                llm_configured=engine.configured,
            )

        hosts = resolve_bind_hosts(settings.dashboard_expose, settings.dashboard_host)
        servers = []
        for host in hosts:
            server = uvicorn.Server(
                uvicorn.Config(
                    web,
                    host=host,
                    port=settings.dashboard_port,
                    log_level="warning",
                    loop="asyncio",
                )
            )
            # With multiple servers in one process, let asyncio/KeyboardInterrupt
            # drive shutdown instead of each server fighting over signal handlers.
            server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
            servers.append(server)
        _log.info(
            "dashboard_ready",
            expose=settings.dashboard_expose,
            hosts=hosts,
            port=settings.dashboard_port,
            verified_pairs=len(store.registry().verified),
            feeds=len(planned),
        )
        try:
            await asyncio.gather(*(s.serve() for s in servers))
        finally:
            await supervisor.cancel_all()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
