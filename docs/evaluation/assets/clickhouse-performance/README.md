# Local engine comparison evidence

All report numbers derive from these immutable captures. `manifest.json` records
checksums; `candidate-code.json` and processing-code-before/after distinguish the
uncommitted measured code from its parent HEAD. Final CH warm reads use compact
transport after explicit, timed non-deduplicating compaction. Initial p95 misses
and physical-layout guard failure remain separate files.

`pg-*`/`ch-*` JSON retain raw samples and DTO fingerprints. `fresh-candidate-parity`
is full six-compartment correctness; latency uses Automatoes only. Sensor is one
30-event burst per engine, not full-file streaming. Tests: 84 discovered,
75 executed, 9 explicit specialized skips. Separate T47/T48 fault evidence is
not counted as newly executed here.

`diagnostics/` contains exact-fixture, read-only investigation scripts; their
hardcoded IDs identify this historical run and are not a generic seed recipe.
`profile_workloads.py` under scripts/clickhouse is a general instrumented probe;
its timings do not replace acceptance samples. Reproduction prerequisites and
limits are in ../../clickhouse-performance.md.

Logs have trailing whitespace normalized. `log-provenance.json` preserves their
original byte counts/hashes. Dataset/config export/video/backup bytes remain
local outside Git. No supplied credential or full metadata dump is included.
