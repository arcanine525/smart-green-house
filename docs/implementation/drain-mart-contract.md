# BR-03 drain mart + query — technical contract (T16 implementation note)

Status: implementation/contract note for mart + query layer only. BR-03 UI
and the accepted end-to-end evidence are T17 scope; nothing here claims the
BR is accepted. Base commit: `3c74612d22e92f573e3b842a7f40dd1abf3d9240`
(T15 pushed). This note documents the sealed-bundle contract T17 builds on.

## 1. Identities (frozen, exactly as loaded)

- Measurement context: `drain_process_computer` (exact core identity from
  `core.drain_observation`; no generic rename). Declared group:
  (`drain_process_computer`, `RuleBasis.MEASURED`) over metrics `ec`, `ph`
  (`RULE_SCHEDULE_METRICS`). Units: `ec` dS/m, `ph` pH.
- CODE_VERSION: `pipeline-v5-drain`; bundles: water + harvest + climate +
  drain (`drain-mart-v1`). Seal trigger (migration 010) requires ALL FOUR
  receipts for v5 seals; the 009 trigger keeps guarding v4; 001–009 stayed
  byte-identical (checksum-skip verified on apply).
- Additive DDL: `mart.dim_drain_cycle`, `mart.fact_drain_slot`
  (`ec_drain`, `ph_drain` + `measure_quality` jsonb + source
  revision/correction identity; UNIQUE (publication, cycle, slot_start);
  PK (publication, revision_id)), `mart.drain_publication` receipt, sealed
  triggers, lookup index — all guarded children of the sealed publication.

## 2. Frozen semantics

- Per-measure triplets (status/applicability/reason) of the policy's drain
  measures freeze at materialization from `core.measure_quality`; the
  loader's EC-negative→invalid and pH-0→suspect decisions are NOT
  re-derived. Missing policy measures show
  `missing/unknown/measure_quality_absent`.
- Evaluation-input masking ONLY: each slot/metric combines ITS OWN triplet
  (single-measure group), masks the value when not
  accepted+applicable (EC negative → quality_invalid, pH 0 →
  quality_suspect); pH verdicts never touch EC in the same row and vice
  versa. Source facts keep originals and remain visible through the
  bounded source inspection (values stay original when masked).
- Fixed bounds ONLY from the explicitly selected immutable schedule
  revision (EC bounds nonnegative; lower ≤ upper; boundary equality
  in-range; delta segments REFUSED for drain content).
- Declared profile boundary, independent of the cache: an explicitly
  selected BR-03 profile is validated on EVERY service path (cached,
  `use_cache=False`, namespace-less adapters, bounded detail) BEFORE any
  cache lookup or mart fetch to declare only EC/pH fixed-bound segments in
  `drain_process_computer`/MEASURED — no delta, no foreign group. Partial
  EC-only or pH-only profiles stay legal (uncovered metrics unknown), and
  a no-rule selection stays legal. The shared engine's generic multi-group
  schedule semantics are unchanged; caching can never change query
  validity.
- No rule → `rule_not_configured` everywhere with joint valid 0; a partial
  revision leaves uncovered metrics unknown. Historical revisions replay
  exactly; at most ONE selected revision per metric; no latest fallback.
- Whole-cycle episodes are built by the shared engine within
  `get_evaluation` (original + clipped facts/directions/peak intact);
  query windows intersect; episode min-filter only pages lists.

## 3. Bounded detail API (T17 contract)

- `DrainService.get_slot_review(context, rule_version=None, window_start,
  window_end, max_slots=1000)` returns
  `SlotReviewWindow(reviews, start, end, wanted_end, truncated,
  window_slots)`:
  - validated against the pinned publication/cycle BEFORE any fetch;
  - read window = floor-of-start grid anchor to min(wanted_end, cap) — a
    straddling slot keeps its EXACT partial overlap (engine start =
    requested start, minutes clip truthfully; a slot whose start is ≥
    wanted_end is never fetched);
  - the page uses a SMALL window-cycle `IntradayContext` with the REAL
    cycle/compartment identity so the engine's per-slot classification is
    the same code path, and no whole-cycle allocation/episode build happens
    for a page;
  - `max_slots` is validated pre-fetch (`type() is int`, 1..1000 — booleans
    rejected), common caps: the real raw oracle shows EC/pH episodes of
    ~33–61 days (e.g. Reference EC ≈ 80,400 min; AICU pH ≈ 87,370 min).
- `slot_facts(context, window_start, window_end, max_slots)` returns
  `SlotFactsWindow(rows, start, end, wanted_end, truncated, window_slots)`:
  independent FROM the OUT list, clamped to the QUERY CONTEXT window (not
  just the cycle), same exact-clip helper. Review rows carry
  `source_quality` notes with the frozen triplet + original value; source
  fields on `RuleSlotReview` (source_lower/upper/setpoint) stay None for
  drain (no setpoints; optional contract field).
- Episode paging/filtering reuses the T15 `page_episodes` helper with the
  drain metric order; unknown/quality classes never draw as valid.

## 4. Bounded result cache (drain-safe)

- Mirrors the approved climate pattern: LRU ≤ 2 entries, keys
  `('drain',) + (host,port,dbname,user) + publication/version identity +
  clamped cycle context + measured basis + rule CONTENT signature`. The
  signature hashes the FIXED lower/upper of every segment (T15's delta-only
  signature is never reused) and refuses delta segments.
- Validation before lookup (context.matches + cycle resolve), the declared
  drain profile boundary (`validate_drain_profile`) plus revision
  scope/compartment identity checked BEFORE any cache lookup and on all
  paths alike, and the engine pin/versions consistency verified on every
  miss. Namespace-less adapters disable the cache. `use_cache=False`
  bypasses both hit and store (rule-change semantics reuse the T15
  convention).

## 5. Catalog declared-group filter

`query_catalog_schedule_rules.sql` now requires EVERY segment of a revision
to match the DECLARED group (measurement_context + metric list + basis
list): mixed or foreign-group revisions are excluded rather than offered as
a choice that would fail at the service boundary, and valid partial
profiles stay listed. The climate screen passes its climate group (both
requested/realized bases and both climate metrics remain one group).
Future drain revisions can never appear as climate UI options and vice
versa. Every caller declares its evaluation group explicitly.

## 6. Demo seed (opt-in)

`python -m greenhouse.db.demo_rules --kind drain [--apply]` registers
`drain-demo-b03:<compartment>:v1` (EC [2,5] dS/m, pH [5.5,6.5], both
segments at the measured basis, evidence cites BR-03 §3/§7, labelled
`demo_rule`), reuses identical content, and refuses drift.

## 7. Tests (unit layer, all disposable DBs)

- `tests/unit/test_drain_mart.py` contains 16 tests: exact §7 30-minute layer
  on the shared engine (EC 25/15/60% + 83.333…%; pH 25/5 + suspect 5/20%; OR joint
  20/15/75%/66.666…%; events 10+5 high), boundary equality, pure masking
  isolation, pipeline freeze (values + per-measure quality triplet shape,
  576 slots for a two-day grid), day-grid spaces (2,880-minute denominator;
  unknown reasons partition; joint 2,865/10), revision replays +
  single-publication rerun, bounded trace windows (six-slot page with
  exact partial minutes + whole-cycle original metadata; caps; invalid
  caps rejected pre-fetch), foreign revision pre-check (before cache),
  cache content signature/namespace/prefix + bypass, cache-independent
  profile rejection on summary/detail paths, EC/pH delta rejection at
  typed segment construction, legal partial/no-rule evaluations across
  cached/uncached/namespace-less/detail paths, catalog exclusion of mixed
  groups while preserving partial drain and multi-metric/multi-basis
  climate profiles, query-context clamped source facts, sealed-table DELETE
  guard, lineage hash verification + demo seed identity/reuse/drift,
  second workflow reuse (no duplication).
- Full-suite baseline on the initial candidate: tests/unit 272 passed;
  UI battery 60 passed. Review corrections use focused regression checks
  for the affected service, catalog and climate paths.
- Review revision: 16 drain tests passed on the final rerun; 58 climate,
  schedule and demo-rule checks passed in the combined backend run;
  14 climate/catalog UI tests passed in the Docker UI image.
- Main's independent probes (main-owned files, read-only references, NOT
  imported into code) `tmp/gated-delivery/main-T16-interim-probes-*.log`,
  persistence probe
  `main-T16-persistence-3.log` (289 stored incl. the `2020-05-30 00:00`
  endpoint sample with zero duration; three-table DELETE guards; audited
  correction → new publication; byte-stable old evaluation via
  `use_cache=False`; foreign revision under old publication rejected) and
  `main-T16-raw-oracle.json` (all six compartments, EC/pH per-class
  minutes/episodes/joint) exist for T17 reconciliation.

## 8. T17 hooks preserved

- Source-naive date mapping on every slot (raw `observed_at` slot start)
  stays in the mart rows for the water-drilldown day aggregation; lineage
  rows carry file/line + raw hashes through the shared core path.
- The schedule catalog yields drain options explicitly for the planned
  drain UI (described in the dispatch; the demo bounds are NOT agronomically
  calibrated) via the same declared-group query; no drain UI
  exists in T16.
- No AGC pipeline publication used in T16: the current AGC publication
  `804770b6…` (v4 climate) remains current; T17 runs ONE end-to-end
  publication including this bundle.
