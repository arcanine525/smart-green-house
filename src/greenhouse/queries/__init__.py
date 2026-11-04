"""Pure summary oracle and services accepting structural query adapters."""
from greenhouse.queries.irrigation import (
    IrrigationService, QueryAdapter, basis_compatible, calendar_days,
    evaluate_day, result_cache_key, summarize_irrigation,
)
