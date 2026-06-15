"""Load the manual pair seed into the registry and publish a hot-path snapshot.

Phase-1 stand-in for the LLM equivalence engine: a human-authored YAML asserts
which Kalshi/Polymarket markets are equivalent and the polarity mapping. The
loader joins those assertions against the catalog (``markets`` table) to fill
``resolves_at`` and a terms fingerprint, writes ``pair_registry`` rows, then
builds and publishes an immutable ``RegistrySnapshot`` for the hot path.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from stellasaurus.common.clock import wall_ms
from stellasaurus.common.ids import terms_fingerprint
from stellasaurus.common.logging import audit, get_logger
from stellasaurus.common.types import OutcomePolarity, PairSource, PairStatus, Venue
from stellasaurus.hot_path.snapshot import PairRegistryEntry, RegistrySnapshot
from stellasaurus.hot_path.state import HotStateStore
from stellasaurus.storage.audit_repo import AuditRepo
from stellasaurus.storage.markets_repo import MarketRow, MarketsRepo
from stellasaurus.storage.registry_repo import RegistryRepo
from stellasaurus.venues.base import VenueClient, market_fingerprint

_log = get_logger("background.registry_loader")


class RegistryLoader:
    def __init__(
        self,
        *,
        seed_path: Path,
        registry_repo: RegistryRepo,
        markets_repo: MarketsRepo,
        audit_repo: AuditRepo,
        store: HotStateStore,
    ) -> None:
        self._seed_path = seed_path
        self._registry = registry_repo
        self._markets = markets_repo
        self._audit = audit_repo
        self._store = store
        self._version = 0

    def _seed_pairs(self) -> list[dict[str, object]]:
        if not self._seed_path.exists():
            return []
        doc = yaml.safe_load(self._seed_path.read_text("utf-8")) or {}
        return doc.get("pairs", [])

    async def resolve_seed_markets(self, clients: dict[Venue, VenueClient]) -> int:
        """Fetch each seeded pair's two markets DIRECTLY and upsert them.

        Avoids depending on a full (rate-limited) catalog crawl: a seeded pair can
        verify as soon as both of its specific markets are fetchable. A leg whose
        venue is unreachable (e.g. Polymarket without keys) is simply skipped, so
        that pair stays STALE until the leg resolves. Then publishes a fresh
        snapshot via ``load_seed``.
        """
        resolved = 0
        for raw in self._seed_pairs():
            for venue, native_id in (
                (Venue.KALSHI, str(raw.get("kalshi_ticker", ""))),
                (Venue.POLYMARKET, str(raw.get("poly_market_slug", ""))),
            ):
                if not native_id:
                    continue
                try:
                    market = await clients[venue].get_market(native_id)
                except Exception as exc:  # noqa: BLE001 - one leg failing is expected
                    _log.warning(
                        "seed_market_fetch_failed",
                        venue=venue.value,
                        native_id=native_id,
                        error=str(exc),
                    )
                    continue
                if market is None:
                    _log.warning("seed_market_not_found", venue=venue.value, native_id=native_id)
                    continue
                self._markets.upsert(
                    MarketRow(
                        venue=market.venue,
                        native_id=market.native_id,
                        title=market.title,
                        rules_text=market.rules_text,
                        settlement_source=market.settlement_source,
                        resolves_at_ms=market.resolves_at_ms,
                        status=market.status,
                        terms_fingerprint=market_fingerprint(market),
                    )
                )
                resolved += 1
        self.load_seed()
        _log.info("seed_markets_resolved", resolved=resolved)
        return resolved

    def load_seed(self) -> int:
        """Parse the seed file, upsert pairs, then publish a fresh snapshot.

        Returns the number of seed pairs processed.
        """
        if not self._seed_path.exists():
            _log.warning("seed_missing", path=str(self._seed_path))
            self.publish()
            return 0
        pairs = self._seed_pairs()
        for raw in pairs:
            self._upsert_seed_pair(raw)
        self.publish()
        _log.info("seed_loaded", count=len(pairs))
        return len(pairs)

    def _upsert_seed_pair(self, raw: dict[str, object]) -> None:
        pair_id = str(raw["pair_id"])
        kalshi_ticker = str(raw["kalshi_ticker"])
        poly_slug = str(raw["poly_market_slug"])
        k_market = self._markets.get(Venue.KALSHI, kalshi_ticker)
        p_market = self._markets.get(Venue.POLYMARKET, poly_slug)

        # A seed pair is only VERIFIED once both legs exist in the catalog;
        # otherwise it is STALE ("awaiting catalog") and not streamed.
        seed_status = PairStatus(str(raw.get("status", "VERIFIED")))
        # A seed pair is VERIFIED only once both legs exist in the catalog.
        status = PairStatus.STALE if k_market is None or p_market is None else seed_status

        resolves = [m.resolves_at_ms for m in (k_market, p_market) if m and m.resolves_at_ms]
        # T_days uses time until BOTH resolve -> the later resolution.
        resolves_at = max(resolves) if resolves else None

        fp = terms_fingerprint(
            {
                "kalshi": k_market.terms_fingerprint if k_market else None,
                "poly": p_market.terms_fingerprint if p_market else None,
            }
        )
        note = raw.get("acceptance_criteria_note")
        entry = PairRegistryEntry(
            pair_id=pair_id,
            canonical_proposition=str(raw["canonical_proposition"]),
            kalshi_ticker=kalshi_ticker,
            poly_market_slug=poly_slug,
            outcome_polarity=OutcomePolarity(str(raw["outcome_polarity"])),
            status=status,
            resolves_at_ms=resolves_at,
            acceptance_criteria={"note": note} if note else None,
            last_verified_at_ms=wall_ms(),
            terms_fingerprint=fp,
            source=PairSource.MANUAL_SEED,
        )
        self._registry.upsert(entry)

    def publish(self) -> RegistrySnapshot:
        """Rebuild the immutable snapshot from the durable registry and publish."""
        self._version += 1
        snapshot = RegistrySnapshot.build(self._version, self._registry.all_entries())
        self._store.publish_registry(snapshot)
        audit(
            self._audit,
            actor="registry_loader",
            event_type="REGISTRY_PUBLISHED",
            version=self._version,
            verified=len(snapshot.verified),
            total=len(snapshot.by_id),
        )
        return snapshot
