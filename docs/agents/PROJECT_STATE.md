# Project continuation state

Last reviewed: **2026-11-07**. This is a handoff snapshot, not permission to mutate
existing deployments. Verify Git, `.env` project/paths, services and current DB
receipts before relying on machine-specific details.

## Delivery status

- All **52 agent implementation/delivery tasks** were completed. The local,
  untracked `TODO.MD` keeps T53 pending for the user; it must not enter Git.
- **T53 is user offline work:** fill three member names/student IDs, confirm actual
  contributions, record/review the overall 20–30 minute presentation with audio,
  refresh artifacts/checksums and validate final submission/access. The destination
  for submitting the assignment has not been specified. Do not auto-upload it.
- Group ID is **29**; three names/IDs intentionally remain placeholders in
  `docs/submission/group.json`. The proposed A/B/C roles are not fabricated contribution records.
- Default native ClickHouse implements all six BRs and sensor replay. PostgreSQL
  compatibility remains available; do not treat it as an unfinished migration.

| Milestone commit | Meaning |
|---|---|
| `82c5a98` | Default ClickHouse, durable cutover journal, verified reverse import/PG rollback |
| `307ac4e` | Native documentation, 10 architecture documents/13 Mermaid diagrams, clean clone smoke |
| `ad849b9` | Group 29 report/deck/editable sources and 24-minute presentation script |
| `666200e` | Pre-video package, safe source ZIP and actual read-only final rehearsal |
| `7689c37` | User-requested Git tracking for seven accepted MP4s and review assets |

These are past milestones. Inspect current HEAD rather than checking out/resetting
one of them. New user requests are new work; do not restart completed tasks.

## Where the useful artifacts are

- `project-submit/README.md`: delivery index and manual completion steps.
- `project-submit/29_technical_report.pdf`: **8 A4 pages**.
- `project-submit/29_slides.pdf`: **20 pages**; DOCX/PPTX editable versions accompany them.
- `project-submit/29_video_script.md`: planned **24 minutes**, three roles.
- `project-submit/demo-videos/clickhouse/index.html`: seven accepted MP4s, now
  tracked with player/metadata/step images/downloads. MP4 total **82.69 MiB**;
  raw WebM/trace and failed/previous recordings are still ignored.
- `project-submit/29_application.zip`: generated local source archive, ignored;
  regenerate after source changes using the packaging script. The delivery manifest
  is tracked, but a clone does not automatically contain this generated ZIP.
- `project-submit/29_delivery_manifest.json`: current artifact/video hashes and
  source-base identity. ZIP per-file hashes identify the candidate bytes; a base
  commit alone is not a claim that the source was a clean checkout of that commit.

The overall `29_video.mp4`/`.webm` presentation has **not** been recorded by the
agent. Seven automated walkthroughs do not replace that video or a human user study.

## Accepted data contexts: do not mix these

Locked AGC ZIP SHA-256:
`c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68`.
There are six compartments; Climate is five-minute slots, Resources daily,
Production harvest records. Full parity covers 287,989 revisions and 14,062,934
source measures, not every row in the 622,899-row source inventory.

| Context | Publication | Automatoes 2020-03-02–03 |
|---|---|---|
| Original native acceptance/videos | `4b08bd51c092465783304f887d766d47` | Irr 9.36 / drain 3.89 L/m² → 41.56% |
| Native after audited cutover exercise | `5d455686a06649e789908b9708a9d436` | Irr 10.36 / drain 3.89 → 37.55% |
| Verified PG reverse import | `c51402d9d25f45c182140b072397de78` | Matches corrected native business results |

The exercise correction `cutover-resource-20261107` adds **1 L/m² Irr on March 2**.
It is not an original AGC observation. Raw and the original pin remain unchanged.
The catalog shows the current complete generation; it is not a selector for every
old publication. Explicitly opening an old saved scenario restores its frozen
context with a warning, and exports must keep that old publication/window.

For that two-day window, energy remains 308.078 kWh with assumed 62.5 m² basis;
A=0 means intensity NULL. Historical baseline cost 17.60006771 EUR becomes
20.840734377 EUR for electricity+20%, i.e. **total cost +18.41%**, not 20%.
Digilog 2020-02-19 harvest A 0.05 / B 0.10 kg/m² gives paired B share 66.67%.
These are specific examples, not farm performance or verified area/price claims.

The final checked replay `walkthrough-live-b3efff6ba5c5` had 30 events and complete
manifest/checksum/sequence. Its normalized source clock was 2019-12-16 02:25:00;
raw source timestamps retain their original precision. Collector stale after the
finite producer stops does not contradict replay completion.

## Evidence already available

- [Six-BR parity](../evaluation/clickhouse-parity.md): 96 CSV, 30 lineage views,
  18 threshold groups and 67,829 episodes, including cycle boundaries.
- [Engine benchmark](../evaluation/clickhouse-performance.md): same snapshot/caps;
  PG was faster in measured warm workloads, CH active table footprint was smaller.
  Final native warm p95 values were all ≤2 s; cold proof/scenario writes are costly.
  Do not merge these results with the separate Hybrid/Kimball PG experiment.
- [Offline/restore](../evaluation/clickhouse-local-repro.md) and
  [native acceptance](../evaluation/clickhouse-acceptance.md).
- [Cutover/rollback](../evaluation/clickhouse-cutover.md): 315 configs, 178 scenarios,
  seven replay pins/four scopes; full parity after reverse import. All replay
  metadata, including normalized clock headers and old partial pins, was verified.
- Final cutover native suite: 100 tests, 91 run/nine heavy skips; 24 PG cost and 8 routing
  pass in separate possibly overlapping suites. Never add those into a fake total.
- [Walkthrough](../evaluation/assets/clickhouse-walkthrough/README.md): seven 1080p
  videos,109 steps, 21 exports, 1642.56 s, full decode/player/chapter review.
- [Pre-video package/rehearsal](../evaluation/assets/submission-package/README.md):
  actual 9 screens/6 BR/7 exports/sensor 30, exact selected/export windows and old frozen
  scenario context; 381.805 s automation. Spoken presentation timing is not measured.
  Corrupt/stale packages and incomplete final submission were correctly rejected.

Historical manifests refer to their recorded snapshots. A newer source ZIP or
video publication commit does not invalidate those past results or justify
rewriting their hashes. Revalidate current artifacts separately.

## Original workstation: inspect before operating

At this handoff the root `.env` selected native project
`gh-ch-repro-20261107032510-rebuilt`, database `greenhouse_repro`, UI
**http://127.0.0.1:18584**. Fresh default port is 18581. Credentials stay in local
`.env`; do not copy them into instructions or logs.

Native raw/state are under `tmp/clickhouse-repro/20261107032510/rebuilt/` on the
original workstation. App/job/stream share its coordinator. Required rollback
journal was **active**, 62 intents, all QueryFinish, head:
`bbfd7017ae3b8c72ca4445544b2ee8012eb0ac48576f579408ba11320fce7a03`.
Read-only final rehearsal left it unchanged. Do not disable the required-journal
flag or replace this state to run a test.

Original `smart-greenhouse-*` PostgreSQL services/volumes and the owned rollback
fixture were stopped and retained. Original business databases are protected;
read-only access is the default. Unrelated containers such as `spiral-db` and
`traefik` are not project cleanup targets. Backups/forensic evidence in `tmp/`
are intentionally retained, even though ignored by Git.

Cold proof previously hit memory limits due to metric_log background merges.
Periodic metric_log collection is disabled in the local CH config; query_log and
terminal write evidence remain enabled. Do not re-enable that writer, remove
pending guards or raise caps to hide a failure without diagnosis and evidence.
