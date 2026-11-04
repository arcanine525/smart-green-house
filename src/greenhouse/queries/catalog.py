"""Typed filter catalog for applications: options come from published data.

UI selectors must be discoverable from this catalog, never pasted IDs. Like
IrrigationService, the service accepts an adapter by Protocol and holds no SQL.
"""
from typing import Protocol

from greenhouse.contracts import CatalogCycle, CatalogPublication, HarvestRuleProfile, IrrigationRuleProfile


class CatalogAdapter(Protocol):
    def catalog_publications(self) -> tuple[CatalogPublication, ...]: ...
    def catalog_cycles(self, publication_id: str) -> tuple[CatalogCycle, ...]: ...
    def catalog_rules(self, scope_id: str, compartment_id: str) -> tuple[IrrigationRuleProfile, ...]: ...
    def catalog_harvest_rules(self, scope_id: str, compartment_id: str) -> tuple[HarvestRuleProfile, ...]: ...
    def catalog_schedule_rules(self, scope_id: str, compartment_id: str, *,
                               measurement_context, metrics, bases): ...


class CatalogService:
    def __init__(self, adapter: CatalogAdapter):
        self.adapter = adapter

    def publications(self):
        """Every scope's current publication; unusable rows stay visible with reasons."""
        return self.adapter.catalog_publications()

    def usable(self, publications=None):
        rows = self.publications() if publications is None else tuple(publications)
        return tuple(row for row in rows if row.usable)

    def harvest_usable(self, publications=None):
        rows = self.publications() if publications is None else tuple(publications)
        return tuple(row for row in rows if row.harvest_usable)

    def climate_usable(self, publications=None):
        rows = self.publications() if publications is None else tuple(publications)
        return tuple(row for row in rows if row.climate_usable)

    def cycles(self, publication: CatalogPublication):
        return self.adapter.catalog_cycles(publication.publication_id)

    def cycle(self, publication: CatalogPublication, cycle_id: str) -> CatalogCycle:
        for candidate in self.cycles(publication):
            if candidate.cycle_id == cycle_id:
                return candidate
        raise ValueError('Cycle is absent from the pinned publication catalog')

    def rules(self, publication: CatalogPublication, compartment_id: str):
        return self.adapter.catalog_rules(publication.scope_id, compartment_id)

    def harvest_rules(self, publication: CatalogPublication, compartment_id: str):
        return self.adapter.catalog_harvest_rules(publication.scope_id, compartment_id)

    def schedule_rules(self, publication: CatalogPublication, compartment_id: str, *,
                       measurement_context: str, metrics, bases):
        """Declared-group selector: the caller must pass its declared
        evaluation group explicitly; there is no unfiltered call. Offered
        options are compatible with the declared group — every segment of
        an offered revision matches it (mixed/foreign-group revisions are
        excluded rather than offered as a failing choice)."""
        return self.adapter.catalog_schedule_rules(
            publication.scope_id, compartment_id,
            measurement_context=measurement_context, metrics=metrics, bases=bases)
