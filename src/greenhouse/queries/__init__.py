"""Pure summary oracle and services accepting structural query adapters."""
from greenhouse.queries.catalog import CatalogAdapter, CatalogService
from greenhouse.queries.climate import (
    ClimateService, build_slots, combined_quality, episode_key, page_episodes,
)
from greenhouse.queries.drain import (
    DrainService, SlotReviewWindow, build_slots as build_drain_slots,
    drain_rule_content_signature,
)
from greenhouse.queries.energy import EnergyAdapter, EnergyService, energy_basis_gate, summarize_energy
from greenhouse.queries.irrigation import (
    IrrigationService, QueryAdapter, basis_compatible, calendar_days,
    evaluate_day, result_cache_key, summarize_irrigation,
)
from greenhouse.queries.intensity import EnergyIntensityService, join_intensity, portfolio
