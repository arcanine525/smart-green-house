"""Caller-owned composition for the water screen.

One application action = one database connection, one pinned publication,
one QueryContext. Day, weekly, summary, detail, lineage and downloads all
reuse the pinned IDs; nothing here re-reads the current pointer mid-action
and no SQL lives outside greenhouse.db.
"""
from dataclasses import dataclass
from datetime import date, timedelta
import json
from pathlib import Path

from greenhouse.contracts import (
    CatalogPublication, CycleWindow, HarvestDay, HarvestRecord, HarvestSummary, IrrigationDay,
    IrrigationSummary, PublicationRef, QueryContext,
)
from greenhouse.queries import ClimateService, DrainService, IrrigationService
from greenhouse.queries.catalog import CatalogService
from greenhouse.queries.harvest import HarvestService


def catalog_service(connection) -> CatalogService:
    from greenhouse.app.runtime import is_clickhouse
    if is_clickhouse(connection):
        from greenhouse.clickhouse.water import WaterAdapter
        return CatalogService(WaterAdapter(connection))
    from greenhouse.db.catalog import PostgresCatalogAdapter
    return CatalogService(PostgresCatalogAdapter(connection))


def irrigation_service(connection, raw_snapshots=None) -> IrrigationService:
    from greenhouse.app.runtime import is_clickhouse
    if is_clickhouse(connection):
        from greenhouse.clickhouse.water import WaterAdapter
        return IrrigationService(WaterAdapter(connection, raw_snapshots=raw_snapshots))
    from greenhouse.db.query_adapter import PostgresQueryAdapter
    return IrrigationService(PostgresQueryAdapter(connection, raw_snapshots=raw_snapshots))


def harvest_service(connection, raw_snapshots=None) -> HarvestService:
    from greenhouse.app.runtime import is_clickhouse
    if is_clickhouse(connection):
        from greenhouse.clickhouse.harvest import HarvestAdapter
        return HarvestService(HarvestAdapter(connection, raw_snapshots=raw_snapshots))
    from greenhouse.db.harvest_adapter import PostgresHarvestQueryAdapter
    return HarvestService(PostgresHarvestQueryAdapter(connection, raw_snapshots=raw_snapshots))


def climate_service(connection, raw_snapshots=None) -> ClimateService:
    """BR-01 evaluation service over the sealed climate bundle."""
    from greenhouse.app.runtime import is_clickhouse
    if is_clickhouse(connection):
        from greenhouse.clickhouse.threshold import ClimateAdapter
        return ClimateService(ClimateAdapter(connection,raw_snapshots=raw_snapshots))
    from greenhouse.db.climate_adapter import PostgresClimateQueryAdapter
    return ClimateService(PostgresClimateQueryAdapter(connection, raw_snapshots=raw_snapshots))


def drain_service(connection, raw_snapshots=None) -> DrainService:
    """BR-03 evaluation service over the sealed drain bundle (exact
    'drain_process_computer' context, measured basis, fixed bounds)."""
    from greenhouse.app.runtime import is_clickhouse
    if is_clickhouse(connection):
        from greenhouse.clickhouse.threshold import DrainAdapter
        return DrainService(DrainAdapter(connection,raw_snapshots=raw_snapshots))
    from greenhouse.db.drain_adapter import PostgresDrainQueryAdapter
    return DrainService(PostgresDrainQueryAdapter(connection, raw_snapshots=raw_snapshots))


def climate_publication_label(row):
    stamp = row.committed_at.astimezone().strftime('%d/%m/%Y %H:%M')
    if not row.climate_usable:
        return (f"{row.scope_id} · {stamp} · thiếu climate mart "
                f"({' · '.join(row.climate_reason_codes)})")
    return f"{row.scope_id} · {stamp} · {row.finality_status} · {row.cycle_count} vụ · climate-mart-v1"


def manifest_snapshot_id(directory: Path):
    """Self-declared snapshot_id from a readable manifest; None when invalid."""
    try:
        payload = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    snapshot_id = payload.get('snapshot_id')
    return snapshot_id if isinstance(snapshot_id, str) and snapshot_id.strip() else None


def _list_candidate_directories(directory: Path):
    try:
        return sorted(child for child in directory.iterdir()
                      if child.is_dir() and not child.name.startswith('.'))
    except OSError:
        return []


def raw_snapshot_mapping(root):
    """snapshot_id -> mounted raw snapshot directory, identity from manifest.

    Candidates are path identities: the configured root itself (a directly
    mounted snapshot root is supported) plus first- and second-level
    directories, covering the locked raw/<dataset>/<sha> layout. Basenames
    never key candidates, so same-named directories cannot overwrite each
    other before ambiguity detection. A candidate counts only when a readable
    manifest.json inside it declares its snapshot_id and the resolved path
    stays inside the configured root; unreadable directories, invalid
    manifests and ambiguous duplicates (same snapshot_id at several paths)
    are skipped, so those snapshots simply stay unverified in the UI. Only
    the small manifest is read here; per-file/record hashing stays in lineage.
    """
    root = Path(root)
    try:
        root_resolved = root.resolve(strict=False)
        if not root_resolved.is_dir():
            return {}
    except OSError:
        return {}
    candidates = {root_resolved}
    for first in _list_candidate_directories(root_resolved):
        candidates.add(first)
        candidates.update(_list_candidate_directories(first))
    found: dict[str, set] = {}
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            readable = resolved.is_dir()
        except OSError:
            continue
        if not readable or not (resolved == root_resolved or root_resolved in resolved.parents):
            continue
        snapshot_id = manifest_snapshot_id(resolved)
        if snapshot_id is None:
            continue
        found.setdefault(snapshot_id, set()).add(resolved)
    return {snapshot_id: next(iter(paths)) for snapshot_id, paths in sorted(found.items())
            if len(paths) == 1}


def resolve_publication(service: IrrigationService, publication_id: str) -> PublicationRef:
    """Pin the chosen publication once per action by ID (never the pointer)."""
    return service.adapter.publication(publication_id)


def context_window(start_inclusive: date, end_inclusive: date):
    """The single inclusive-UI-end -> half-open context conversion."""
    if type(start_inclusive) is not date or type(end_inclusive) is not date:
        raise ValueError('Bộ lọc ngày yêu cầu ngày trọn vẹn')
    if start_inclusive > end_inclusive:
        raise ValueError('Ngày bắt đầu phải trước hoặc bằng ngày kết thúc')
    return start_inclusive, end_inclusive + timedelta(days=1)


def build_context(publication: PublicationRef, cycle: CycleWindow, start_inclusive: date,
                  end_inclusive: date, rule_version: str | None = None) -> QueryContext:
    start, end = context_window(start_inclusive, end_inclusive)
    return QueryContext(publication.scope_id, publication.publication_id, publication.snapshot_id,
                        cycle.cycle_id, (cycle.compartment_id,), start, end, rule_version)


@dataclass(frozen=True)
class WaterAction:
    """Everything one pinned action produced; exports must reuse these DTOs."""
    publication: PublicationRef
    context: QueryContext
    days: tuple[IrrigationDay, ...]
    summary: IrrigationSummary
    weekly: tuple[IrrigationSummary, ...]


def water_action(service: IrrigationService, publication: PublicationRef,
                 context: QueryContext) -> WaterAction:
    """Run day/summary/weekly reads against the same pinned publication."""
    days = service.get_irrigation_days(context)
    summary = service.get_irrigation_summary(context)
    weekly = service.get_irrigation_weekly(context)
    return WaterAction(publication=publication, context=context, days=days, summary=summary, weekly=weekly)


def cycle_lineage(service: IrrigationService, publication: PublicationRef, revision_id: str):
    return service.get_lineage(publication, revision_id=revision_id)


def publication_label(row: CatalogPublication):
    stamp = row.committed_at.astimezone().strftime('%d/%m/%Y %H:%M')
    counts = f"{row.cycle_count} vụ · {row.day_count} ngày" if row.usable else 'chưa có water mart'
    return f"{row.scope_id} · {stamp} · {row.finality_status} · {counts}"


def harvest_publication_label(row: CatalogPublication):
    stamp = row.committed_at.astimezone().strftime('%d/%m/%Y %H:%M')
    if not row.harvest_usable:
        return f"{row.scope_id} · {stamp} · thiếu harvest mart ({' · '.join(row.harvest_reason_codes)})"
    return f"{row.scope_id} · {stamp} · {row.finality_status} · {row.cycle_count} vụ · harvest mart v3"


@dataclass(frozen=True)
class HarvestAction:
    """Everything one pinned harvest action produced; exports reuse these DTOs."""
    publication: PublicationRef
    context: QueryContext
    summary: HarvestSummary
    days: tuple[HarvestDay, ...]
    weekly: tuple[HarvestSummary, ...]
    records: tuple[HarvestRecord, ...]


def harvest_action(service: HarvestService, publication: PublicationRef,
                   context: QueryContext) -> HarvestAction:
    """Read record/day/weekly/summary outcomes against one pinned action."""
    records = service.get_harvest_records(context)
    days = service.get_harvest_days(context)
    summary = service.get_harvest_summary(context)
    weekly = service.get_harvest_weekly(context)
    return HarvestAction(publication=publication, context=context, summary=summary,
                         days=days, weekly=weekly, records=records)


def energy_service(connection, raw_snapshots=None):
    from greenhouse.db.energy_adapter import PostgresEnergyQueryAdapter
    from greenhouse.queries.energy import EnergyService
    return EnergyService(PostgresEnergyQueryAdapter(connection, raw_snapshots=raw_snapshots))


def intensity_service(connection, raw_snapshots=None):
    from greenhouse.db.harvest_adapter import PostgresHarvestQueryAdapter
    from greenhouse.queries.intensity import EnergyIntensityService
    return EnergyIntensityService(energy_service(connection, raw_snapshots),
        PostgresHarvestQueryAdapter(connection, raw_snapshots=raw_snapshots))


def cost_service(connection, raw_snapshots=None):
    from greenhouse.db.cost_adapter import PostgresCostAdapter
    from greenhouse.queries.cost import EnergyCostService
    return EnergyCostService(
        energy_service(connection, raw_snapshots),
        PostgresCostAdapter(connection, raw_snapshots=raw_snapshots),
    )


def save_drain_profile(connection,publication,cycle,**kwargs):
    from greenhouse.app import runtime
    if runtime.is_clickhouse(connection):
        import os
        from greenhouse.clickhouse.schedules import save_drain_profile as save
        return save(connection,Path(os.environ.get('GREENHOUSE_CH_COORDINATOR','/candidate/coordinator')),publication,cycle,**kwargs)
    from greenhouse.db.drain_profiles import save_drain_profile as save
    revision=save(connection,publication,cycle,**kwargs)
    connection.commit()
    return revision
