"""Composition root: wire config -> db -> hot state -> venues -> background -> control.

Run with: ``python -m stellasaurus.app`` (or the ``stellasaurus`` console script).

Single asyncio event loop owns everything (DESIGN §5): two market-data feeds,
catalog sync, registry refresh, and the FastAPI dashboard. The hot path reads only
in-memory snapshots; the background plane pushes into them out of band.
"""

from __future__ import annotations

import asyncio
import contextlib
from decimal import Decimal

import httpx
import uvicorn
from dotenv import load_dotenv

from stellasaurus.background.catalog_sync import CatalogSync
from stellasaurus.background.equivalence import EquivalenceEngine
from stellasaurus.background.fee_sync import FeeParamSync
from stellasaurus.background.feed_manager import FeedManager
from stellasaurus.background.halt import HaltController
from stellasaurus.background.pairing import PairingLoop
from stellasaurus.background.registry_loader import RegistryLoader
from stellasaurus.background.scheduler import TaskSupervisor
from stellasaurus.background.subscription_mgr import SubscriptionManager
from stellasaurus.common.clock import wall_ms
from stellasaurus.common.config import Settings, load_settings
from stellasaurus.common.logging import configure_logging, get_logger
from stellasaurus.common.types import Venue
from stellasaurus.control.app import create_app
from stellasaurus.control.net import resolve_bind_hosts
from stellasaurus.control.readmodel import ReadModel
from stellasaurus.hot_path.evaluator import OpportunityEvaluator
from stellasaurus.hot_path.execution import PaperExecutionEngine
from stellasaurus.hot_path.fees import FeeParams
from stellasaurus.hot_path.ingest import BookStore
from stellasaurus.hot_path.opportunities import OpportunitySink
from stellasaurus.hot_path.positions import PositionsStore
from stellasaurus.hot_path.risk import RiskManager
from stellasaurus.hot_path.snapshot import LimitsSnapshot, RegistrySnapshot
from stellasaurus.hot_path.state import HotStateStore
from stellasaurus.storage.audit_repo import AuditRepo
from stellasaurus.storage.db import Database
from stellasaurus.storage.markets_repo import MarketsRepo
from stellasaurus.storage.pnl_repo import PnlRepo
from stellasaurus.storage.positions_repo import PositionsRepo
from stellasaurus.storage.registry_repo import RegistryRepo
from stellasaurus.venues.factory import venue_clients

_log = get_logger("app")


def _limits_from_settings(s: Settings) -> LimitsSnapshot:
    return LimitsSnapshot(
        version=1,
        # Paper trading starts un-halted; the flag is exercised by the kill
        # switch (manual + auto-triggers). REAL order submission remains
        # separately hard-gated by live_trading_enabled when it exists.
        halted=False,
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

        # --- Phase 3: evaluator + fee engine (paper mode) ---
        fee_params = FeeParams(
            kalshi_taker_multiplier=Decimal(str(settings.kalshi_fee_multiplier_default)),
            kalshi_maker_multiplier=Decimal(str(settings.kalshi_fee_multiplier_default)) / 4,
            kalshi_precision_micros=settings.kalshi_balance_precision_micros,
            poly_taker_coefficient=Decimal(str(settings.poly_taker_fee_coefficient)),
            poly_maker_coefficient=Decimal(str(settings.poly_maker_fee_coefficient)),
        )
        opp_sink = OpportunitySink()

        # --- Phase 4: risk manager + PAPER executor + kill switch ---
        positions_store = PositionsStore()
        positions_repo = PositionsRepo(db)
        pnl_repo = PnlRepo(db)
        risk = RiskManager(state=store, positions=positions_store)
        executor = PaperExecutionEngine(
            state=store, positions=positions_store, fee_params=fee_params,
            slippage_tolerance_bips=settings.slippage_tolerance_bips,
        )
        halt = HaltController(
            store=store, positions=positions_store, audit_repo=audit_repo,
        )
        evaluator = OpportunityEvaluator(
            state=store, fee_params=fee_params, sink=opp_sink,
            risk_gate=risk, executor=executor,
        )
        book_store.add_listener(evaluator.on_book_update)

        async def drain_opportunities() -> None:
            # Hot path never touches disk: fired paper opportunities are drained
            # to the audit log out of band, and dead pairs pruned from the sink.
            fired = opp_sink.drain_fired()
            for o in fired:
                audit_repo.append(
                    actor="evaluator",
                    event_type="PAPER_FIRE",
                    pair_id=o.pair_id,
                    detail={
                        "orientation": o.orientation, "qty": o.qty,
                        "vwap_yes_micros": o.vwap_yes_micros,
                        "vwap_no_micros": o.vwap_no_micros,
                        "fees_per_pair_micros": o.fees_per_pair_micros,
                        "net_edge_micros": o.net_edge_micros,
                        "annualized_return": o.annualized_return,
                    },
                )
            opp_sink.prune(frozenset(store.registry().verified))
            # Phase 4 drains: risk decisions + positions to durable storage.
            for d in risk.drain_decisions():
                audit_repo.append(
                    actor="risk_manager",
                    event_type="RISK_DECISION",
                    pair_id=d.pair_id,
                    detail={"orientation": d.orientation, "qty": d.qty,
                            "committed_micros": d.committed_micros,
                            "approved": d.approved, "rejected_by": d.rejected_by},
                )
            for p in positions_store.drain_new():
                positions_repo.upsert(p)
                audit_repo.append(
                    actor="paper_executor",
                    event_type=f"PAPER_{p.hedge_status.value}",
                    pair_id=p.pair_id,
                    detail={"position_id": p.position_id, "qty": p.qty,
                            "committed_micros": p.committed_micros,
                            "unwind_loss_micros": p.unwind_loss_micros},
                )
            for p in positions_store.resolve_expired(wall_ms()):
                # Locked pair pays $1/pair at resolution: realized = payout - committed.
                payout = p.qty * 1_000_000
                realized = payout - p.committed_micros
                pnl_repo.record(
                    pair_id=p.pair_id,
                    predicted_edge_micros=realized,  # paper fills == predicted
                    realized_edge_micros=realized,
                    fees_micros=p.fees_micros,
                    detail={"position_id": p.position_id, "qty": p.qty,
                            "committed_micros": p.committed_micros},
                )
                audit_repo.append(
                    actor="paper_executor", event_type="POSITION_RESOLVED",
                    pair_id=p.pair_id,
                    detail={"position_id": p.position_id,
                            "committed_micros": p.committed_micros,
                            "realized_edge_micros": realized},
                )

        # --- read model ---
        read_model = ReadModel(store)
        read_model.opportunity_sink = opp_sink
        read_model.positions_store = positions_store
        read_model.risk_manager = risk
        read_model.pnl_totals_provider = pnl_repo.totals
        read_model.feed_stats_provider = sub_mgr.feed_stats
        read_model.catalog_stats_provider = lambda: {
            "counts": catalog.last_counts,
            "last_sync_ms": catalog.last_sync_ms,
        }
        web = create_app(
            read_model,
            push_interval_ms=settings.dashboard_push_interval_ms,
            halt_controller=halt,
        )

        # --- supervise ---
        supervisor = TaskSupervisor()

        # Feeds are owned by the FeedManager, which re-plans them whenever the
        # verified pair set changes (pairs verified later stream without restart).
        feed_mgr = FeedManager(
            store=store, sub_mgr=sub_mgr,
            check_interval_s=settings.subscription_check_seconds,
        )
        supervisor.supervise("feed_manager", feed_mgr.run)

        # Catalog: optional bootstrap loops Kalshi rotation chunks back-to-back
        # until one full series sweep completes, then periodic chunks keep it
        # fresh. Polymarket is synced once per periodic cycle only.
        kalshi_client = clients[Venue.KALSHI]

        async def catalog_bootstrap() -> None:
            if not settings.kalshi_bootstrap_sweep:
                return
            for _ in range(80):  # safety bound (~11k series / chunk size)
                await catalog.sync_once(venues={Venue.KALSHI})
                swept, total = getattr(kalshi_client, "rotation", (0, 0))
                if total and swept >= total:
                    _log.info("catalog_bootstrap_complete", series=total)
                    return
            _log.warning("catalog_bootstrap_capped")

        supervisor.supervise_once("catalog_bootstrap", catalog_bootstrap)
        supervisor.run_periodic(
            "catalog_sync", settings.catalog_refresh_seconds, catalog.sync_once
        )
        supervisor.run_periodic("opportunity_drain", 5, drain_opportunities)
        supervisor.run_periodic("halt_watch", 10, halt.watch_once)

        # Phase 5: fee-param sync + divergence reconciliation (§6.4/§6.10).
        fee_sync = FeeParamSync(
            settings=settings, http=http, clients=clients, store=store,
            initial=fee_params, publish_to=[evaluator, executor],
            halt=halt, audit_repo=audit_repo,
        )
        supervisor.run_periodic(
            "fee_sync", settings.fee_param_refresh_seconds, fee_sync.sync_once
        )

        # Phase 2 pairing loop: candidates -> LLM verdicts -> registry (source=LLM).
        engine = EquivalenceEngine()
        if settings.pairing_enabled and engine.configured:
            pairing = PairingLoop(
                markets_repo=markets_repo,
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
            bootstrap=settings.kalshi_bootstrap_sweep,
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
