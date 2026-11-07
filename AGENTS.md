# Instructions for coding agents

## Project and reading order

Smart Greenhouse is a local Data Engineering course assignment. It implements
six business requirements over Autonomous Greenhouse Challenge (AGC) 2019–2020
data, plus a sensor replay path. It is an analytics system; replay is not a live
hardware measurement or an actuator/control system.

Start with these files, then inspect the implementation relevant to the request:

1. [README](readme.md): current setup and architecture.
2. [Continuation state](docs/agents/PROJECT_STATE.md): dated delivery/runtime snapshot,
   accepted publication IDs, outstanding manual work and evidence.
3. [Business requirements](docs/br/): the six contracts and acceptance examples.
4. [ADR-003](docs/adr/003-clickhouse-warehouse.md) and
   [architecture diagrams](docs/architecture/README.md).
5. [Requirements matrix](docs/implementation/requirements-matrix.md) and
   [evaluation index](docs/evaluation/README.md): what was actually checked.

The implementation and current ADR/README take precedence over early scaffold
status in historical component notes. ADR-001/002 and PostgreSQL evaluation
reports retain their original context; they do not describe the default engine.
Do not infer completion from a diagram, a task checkbox or the presence of a test.

## Current architecture

- **Default warehouse: ClickHouse**, single node. `compose.yaml` includes
  `compose.clickhouse.yaml`. It must run without a PostgreSQL server or PG credentials.
- **Historical native batch: Python**, using shared parsers/semantic transforms.
  Do not describe this path as Spark batch processing.
- **Sensor: simulator → SQLite outbox → MQTT QoS1 → durable collector/spool →
  atomic raw-ready files → Spark Structured Streaming → native ClickHouse sink**.
- **UI: Streamlit + Plotly**, nine screens: six BRs, overview, quality and replay.
  Services/DTOs provide the calculations and exports; the UI does not define KPI math.
- PostgreSQL is an explicit compatibility, comparison and rollback backend in
  `compose.postgres.yaml`, with a separate Compose project/volumes/checkpoints.
  Backend errors must not silently fall back to PostgreSQL or fixture data.
- Runtime is local/offline after preparing images, dependencies and the locked ZIP.
  Internal app/job/DB networks have no egress; the localhost relay has one fixed target.

| Location | Responsibility |
|---|---|
| `src/greenhouse/cli.py`, `__main__.py` | Public CLI and backend dispatch |
| `src/greenhouse/ingestion/` | ZIP/snapshot integrity, CSV parsing, raw provenance |
| `src/greenhouse/transform/` | Shared quality, units and canonical semantics |
| `src/greenhouse/contracts/` | Typed identities, contexts, Decimal and exports |
| `src/greenhouse/queries/` | Shared business/query/command services |
| `src/greenhouse/clickhouse/` | HTTP client, schema, loader, adapters, journals and publication |
| `src/greenhouse/db/` | PostgreSQL adapters, SQL and migrations |
| `src/greenhouse/sensor/` | Simulator, collector, spool, replay and Spark entry points |
| `src/greenhouse/app/` | Production Streamlit screens and presentation |
| `src/greenhouse/kimball/` | Separate PostgreSQL architecture comparison |
| `tests/clickhouse/`, `tests/unit/`, `tests/ui/` | Native, shared/PG and UI checks |
| `scripts/clickhouse_repro/`, `scripts/clickhouse_cutover/` | Guarded rehearsal/recovery tools |
| `tools/walkthrough/` | Real Playwright recording and unrecorded rehearsal |
| `docs/submission/`, `project-submit/` | Report/deck sources, identity and deliverables |
| `tmp/`, `data/` | Ignored local state/dataset/evidence; not disposable by default |

## Business contracts that must survive changes

| BR | Meaning and important guard |
|---|---|
| BR-01 | Climate temperature/CO₂ episodes, duration and coverage; requested and realized are separate bases |
| BR-02 | Irrigation/drain by day/week/cycle; ratio of paired sums, not average daily ratios |
| BR-03 | Drain EC/pH episodes and quality; gaps/unknown/rule boundaries break episodes |
| BR-04 | Energy and kWh/kg **A**; aggregate sources separately before joining; zero A → NULL |
| BR-05 | Harvest record/day/week/cycle and paired B/(A+B); no event is not automatically zero |
| BR-06 | Historical tariff and immutable price scenarios with frozen quantities/context |

- Keep source, scope, cycle, record/event, revision, publication and config identities.
  Historical, replay and fault scopes must not be combined into one KPI.
- Source time is a naive wall clock until verified. Received/operating time is UTC.
  Do not assign UTC to AGC timestamps or confuse raw timestamps with normalized slot cursors.
- Missing, NULL, zero, suspect, invalid, partial and quarantine are distinct.
  Preserve quality/reason codes and coverage/completeness alongside quantities.
- Use exact Decimal/contract serialization for quantities and money. The CH exact
  String representation is authoritative; numeric projection is only used when
  lossless. Do not introduce float rounding to make parity pass.
- The 62.5 m² `AGC_SHARED_BASIS_V1` area is **assumed_unverified**. Rule demos and
  historical tariffs are labeled. Do not call them verified agronomy or a real bill.
- Resources is daily energy/quantity, not instantaneous power. Heat MJ → kWh
  divides by 3.6; do not multiply daily energy by hours again.
- Corrections append audited revisions. They must not rewrite raw or change an
  old publication's membership. Lineage and exports must retain the chosen versions.
- A saved scenario may restore its old context with an explicit warning. Do not
  apply new sidebar filters/publications silently to its frozen quantities.

See the relevant BR and contract before changing a formula or data grain.

## Persistence, recovery and cache rules

Read [migration contract](docs/implementation/clickhouse-migration-contract.md),
[publication](docs/implementation/clickhouse-publication.md),
[sensor](docs/implementation/clickhouse-sensor.md), and
[cutover/rollback](docs/implementation/clickhouse-cutover-rollback.md) before
changing any write, replay or recovery path.

- ClickHouse logical relations are validated by repositories/manifests, not SQL
  foreign keys or PostgreSQL-style multi-statement transactions.
- Every supported writer uses the shared local POSIX coordinator lock. Do not
  remove/recreate its lock inode while processes are active or split coordinators
  between app/job/stream. This protocol is not a distributed/NFS lock.
- Persist intent/query ID before SQL; wait for synchronous writes. Unknown/lost
  ACK outcomes require terminal query-log reconciliation before another attempt.
  Do not clear fences, disable guards or guess that a timed-out write failed.
- Identical physical retries collapse only with matching identity/full payload.
  Conflicting hashes/ordinals are errors. Background merges and finite retry
  dedup windows are not correctness/completeness proofs.
- Full immutable config/scenario envelopes include segments/rates. Readers must
  never see a scenario header without its rates.
- Verify all bundle receipts/membership/config before COMPLETE. One action pins
  one publication/context for charts, tables, lineage and CSV/JSON. Incomplete
  attempts are not queryable business results; old pins remain immutable.
- Process proof caches are bounded and guarded by namespace/server/schema/parts/
  mutations/config. Never bypass validation to make a cold proof or benchmark faster.
  Cold proof can take minutes; the warm query target is not a startup SLA.
- After an accepted cutover, keep `GREENHOUSE_CH_REQUIRE_CUTOVER_JOURNAL=1` and
  the matching journal. Missing/paused/wrong-namespace/unreconciled state must block writes.
  The default `0` is only for a genuinely fresh, separate bootstrap/test warehouse.
- PUBACK is not a warehouse receipt. Replay completeness needs manifest, cursor,
  sequence and checksum evidence. A new warehouse needs a new checkpoint generation.
- Backup at a consistent stopped-writer boundary: DB/config/raw/manifests,
  coordinator/journal, broker, SQLite DB/WAL/SHM, spool/ready and checkpoints.
  Copying only SQLite main files or only raw telemetry is insufficient.

## Local environment and protected data

Inspect `git status`, the selected Compose project, `.env` paths and running
services before starting work. Never print secrets from `.env` or container env.
An existing checkout may point at a retained acceptance deployment; the README's
fresh-bootstrap commands are not permission to rebuild that deployment.

- Do not overwrite an existing `.env`, original PostgreSQL databases, accepted
  raw snapshots, publications, backup archives or coordinator/checkpoint state.
- Keep tests and rehearsals in uniquely named **owned** projects/databases/paths.
  Use `--env-file` for an isolated environment and distinct image tags where needed.
- Avoid broad `down -v`, `docker volume prune`, `docker system prune`, DB drops,
  checkpoint deletion or cleanup across unrelated projects. Any fixture reset must
  have explicit identity/ownership guards and a verified recovery path.
- If Docker storage causes a problem, the user authorizes clearing **build cache**:
  `docker builder prune -af`. This does not authorize pruning data volumes, raw,
  WAL/SHM, backups or other projects. Check headroom before large rebuilds/proofs.
- Keep source Python at **3.12** as pinned in `pyproject.toml` and Docker images.
  Spark/JRE/Python deps belong in prepared images; do not assume the host has them.
  Read current digest/lock files rather than substituting latest versions.
- The Docker build context is allowlisted by `.dockerignore`. Code/tests are
  copied into images: rebuild relevant images from current source before claiming
  tests cover a code change. Do not accidentally test an old acceptance image.

For a fresh deployment, follow [README quickstart](readme.md#chạy-local), including
locked ZIP, snapshot, migration, explicit basis, pipeline and tariff registration.
Default UI port is 18581; the existing workstation may use a different `.env` port.

## Verification by change scope

Use the smallest meaningful checks, then required broader integration gates.
Never run destructive/integration fixtures against the active warehouse.

For an isolated test environment, set `GH_TEST_ENV` to its env file and
`GH_TEST_PROJECT` to its unique owned project. Ensure paths/image tags are isolated
and prepared images include your current source. Example pure native unit check
(no DB service needs to be started):

```bash
docker compose --env-file "$GH_TEST_ENV" -p "$GH_TEST_PROJECT" --profile tools \
  run --rm --no-deps --pull never ch-client \
  python -m unittest discover -s tests/clickhouse -p test_encoding.py -v
```

- `tests/clickhouse/` uses unittest. Real server tests require
  `GREENHOUSE_CH_INTEGRATION=1` on a private CH fixture. `ch-ui-test` in profile
  `test` runs the native suite with UI/server flags on that fixture; never point
  it at the retained warehouse. Explicit MQTT/Spark heavy flags are separate.
- PostgreSQL/shared tests use pytest (`tests/unit/`, `tests/ui/`).
  `scripts/test_local.sh` explicitly selects a new PG test project and
  `compose.postgres.yaml`; it is not the default ClickHouse acceptance command.
- Full AGC, six-BR parity, real sensor faults and recovery need the documented
  harnesses/evidence, not only unittest discovery. Read the rehearsal's identity
  configuration before running `scripts/clickhouse_repro/` or `clickhouse_cutover/`.
- UI behavior changes need relevant service/AppTest/export checks and real
  Playwright interactions where appropriate. Preserve independent expected values.
  Do not substitute DOM/KPI/server values or fabricate success output.
- Documentation-only edits need link/command consistency checks. Rebuild PDFs
  only if their sources/identity change. Do not claim unchanged old results as a new run.
- Report skips, exact scope, dataset/publication/config/source/image versions and
  limitations. Do not add overlapping suite counts into an invented total.

## Git and delivery workflow

- Work in the main session by default. Do not launch workers or apply
  `gated-delivery` unless the user explicitly requests that workflow again.
- Current delivery branch is `develop`. Inspect the tree first and preserve user
  changes. Use one focused commit per work item with a clear message, without Txx
  task IDs. Review locally, amend fixes before pushing; push `develop` when authorized.
- **Never stage/commit `TODO.MD`**; it is an intentionally local plan. A clone may
  have no TODO: use current state/docs and the user's new request, not an invented backlog.
- Never commit `.env`, credentials, AGC raw/ZIP, dumps, checkpoints, spools or backups.
- The accepted seven ClickHouse MP4s, player, step images, metadata and downloads
  under `project-submit/demo-videos/clickhouse/` are tracked in ordinary Git by
  explicit user request. Raw WebM/trace/recorder snapshots and failed/previous
  recordings remain ignored. Preserve media/export bytes and accepted checksums.
  New recordings use a new directory; do not overwrite accepted clips.
- Final deliverables live in `project-submit/`. Source report/deck/script/identity
  live in `docs/submission/`; `group.json` is the single editable identity source.
  The project README describes a DE assignment; group identity belongs in submission docs.
- `scripts/build_submission.sh` exports PDFs/DOCX/PPTX/script. If identity or source
  changes, rebuild the affected deliverables and refresh package checksums.
- `scripts/submission_package.py build 29 project-submit` selects tracked/staged
  paths and binds working-tree bytes by per-file hashes. Stage intended new source
  files before building. Videos/generated bundle evidence/recursive archives are
  excluded from source ZIP; source ZIP itself remains local/ignored.
- `scripts/check_submission.sh 29 project-submit --pre-video` checks the accepted
  partial delivery. The default final gate requires completed identity and an
  overall 20–30 minute video with audio. Do not create a placeholder video to pass it.
  Pre-video acceptance is not a complete four-product submission.
- Do not rewrite historical evidence manifests to match later changes. Keep prior
  archives/receipts and record a new validation result for the new version.
- User-facing updates and project documentation are normally Vietnamese; code
  identifiers follow existing English domain terminology. Explain material limits
  and what actually ran. Do not upload/submit the assignment without instructions.

## Original workstation tool

The following installation note is provided by the user. Check availability when
working on another machine; it is not an application runtime dependency.

<!-- postman-cli:installation:start -->
Postman CLI is installed on this device, and can be used for API Engineering work.
It is not on PATH; run it as /Users/hiep.le/.local/bin/postman.
<!-- postman-cli:installation:end -->
