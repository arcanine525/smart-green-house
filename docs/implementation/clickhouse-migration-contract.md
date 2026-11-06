# Hợp đồng chuyển PostgreSQL → ClickHouse

Thiết kế T37, 2026-11-07. [ADR-003](../adr/003-clickhouse-warehouse.md) là quyết
định điều phối; tài liệu này chỉ ra seam và tiêu chí kiểm. Chưa là bằng chứng triển khai.

## Baseline và dữ liệu đối chứng

- Code PostgreSQL: `da1d13d44922fad1b83b4a8d1a132c57af15610c`.
- ZIP SHA-256: `c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68`.
- [Nghiệm thu sáu BR](requirements-matrix.md), [full AGC restore](../evaluation/assets/full-local-repro/summary.json), [benchmark](../evaluation/performance.md).
- Full historical publication của lượt restore: `3e4a051009084e11ab19efdd2343c9d3`.
  Publication ID engine mới có thể khác; so semantic content/config/window, không
  so UUID hoặc physical table count. Mọi export mang source head/hash/provenance.
- Config cần inventory/export: quality policy, selected basis, irrigation/climate/
  drain/harvest rule envelopes, tariff/rates, scenario/request receipts, correction
  revision và completeness. Nguồn raw không tái dựng được config do người dùng nhập.

## Bản đồ code và thay thế

| Hiện tại | Seam/cơ chế ClickHouse | Task kiểm chứng |
|---|---|---|
| `settings.py`, `cli.py`, app `home.py` mở psycopg | cấu hình/client/factory theo backend, không bắt buộc PG env | T40/T48 |
| `db/migrate.py`, migrations/sql, `db/loader.py` | DDL có version, canonical source/revision loader, validation tham chiếu | T41 |
| `db/pipeline.py`: scope/workflow advisory locks, atomic publish | shared flock, synchronous query lifecycle, complete envelope + receipt manifest | T42 |
| `db/*_mart.py`, `db/spark_stage.py` | tái dùng transform; ClickHouse batch sink/marts theo publication | T43–T46 |
| `db/query_adapter.py`, `catalog.py`, `lineage.py` | ClickHouse Query/Catalog/Lineage adapters, pinned membership, raw hash check | T43/T48 |
| `db/harvest_adapter.py`, `climate_adapter.py`, `drain_adapter.py`, `energy_adapter.py` | cùng Protocol/DTO; SQL theo engine, không gọi lại PG bên trong | T44–T46 |
| `db/rule_schedule.py`, profiles/basis/corrections | envelope bất biến, kiểm overlap/content hash trước append dưới lock | T41/T45 |
| `db/cost_adapter.py`: scenario tx, request locks và rate rows | một envelope header+rates; idempotency/conflict dưới lock | T46 |
| `sensor/stream_landing.py`, replay publication/store | foreachBatch sink mới, batch/event receipts và complete manifest, generation riêng | T47 |
| app `wiring.py`, replay/errors/cache | backend factories, lỗi backend chung, key có engine/scope/pub/config | T48 |
| `kimball/schema.py`, `etl.py`, `adapter.py` | giữ PostgreSQL reference riêng; không dùng làm dependency CH | T49/T52 |
| `scripts/repro/*`, backup/restore harness | inventory logic, ClickHouse backup và consistent sensor state | T50 |

Các `ON CONFLICT`, FK/unique/check/exclusion, registration XID và COPY không được
xem là tính năng đã chuyển chỉ vì DDL khởi tạo thành công. Cần semantic validation
và test qua supported command path; read-only DB role tách khỏi writer.

## Equality và tolerance khóa trước triển khai

- Identity, counts, revision selection, reason codes, quality/applicability,
  membership, config hashes, record dates/source serials và missing/NULL/0:
  **khớp chính xác** sau mapping ID của engine, không tolerance.
- Giá trị nguồn decimal, quantities hữu hạn, giá/rates, tiền hữu hạn và envelope
  Decimal codec: **khớp chính xác numeric Decimal**; trailing zeros được normalize.
- Ratio, intensity và các phép chia không hữu hạn: thực hiện cùng pure service
  context precision 50 của baseline, kỳ vọng Decimal chính xác. Khi kiểm output
  numeric của công cụ khác, absolute tolerance `1e-24`, relative tolerance `0`;
  phải lưu cả unrounded values. UI rounding theo contract hiện hành, không nới
  tolerance cho lỗi units/basis/time window hoặc đổi chính sách missing.
- Episode start/end, duration microseconds, coverage numerator/denominator và
  rule segmentation: khớp chính xác; phần trăm áp dụng quy tắc phép chia ở trên.
- Lineage/raw checksum/source physical line span và CSV semantic rows: khớp chính
  xác; khác thứ tự vật lý được sort theo documented semantic keys trước so.
- Pipeline/sensor operation timestamps, UUID, elapsed timing: không so bằng;
  kiểm loại clock, provenance, monotonicity và quan hệ receipt/cursor đúng.

## Schema vật lý dự kiến

Core append-only typed source keys + revision/hash; dimension/config envelope
immutable; mart theo grain resource-day, harvest-record, climate/drain-slot,
energy-component-day. Publication membership/receipt/command journal tách rõ.
MergeTree sorting bắt đầu scope/publication/cycle và grain phục vụ truy vấn;
khóa dedup không chứa thuộc tính mutable của cùng event. Partition nguồn theo
scope/cycle hoặc thời gian ổn định của identity, không để correction đổi miền.
T41 phải ghi DDL cụ thể và giải thích cardinality/partition; không partition
mỗi event/publication nếu dẫn đến quá nhiều partition/parts. Envelope là metadata,
không thay toàn bộ facts bằng một JSON blob để vượt test.

## Gates và ca lỗi bắt buộc

1. Nạp cùng nguồn hai lần, cùng key khác payload, correction liên tiếp, revision
   trùng ordinal khác hash: không đổi KPI do retry; conflict rõ và audit còn đủ.
2. Hai writer/process lock, writer bị kill khi server còn INSERT, mất ACK sau
   insert, crash mỗi publication transition: reader chỉ thấy complete bundle.
3. Ghi rule/scenario header+segments/rates, crash/retry cùng request, đổi payload
   cùng ID: không tồn tại cấu hình visible thiếu thành phần hoặc mất write đã ack.
4. Reader pin publication rồi publish mới/config mới: chart/summary/CSV cũ giữ
   nguyên; app cache không trộn engine hoặc versions.
5. Sensor commit trước checkpoint, late/out-of-order qua biên batch, restart
   các process và DB: batch–stream parity; không double-count, completeness theo
   cursor+manifest, scope replay không trộn historical.
6. Full raw backfill + config export trên hai engine + independent oracle; không
   suy correctness chỉ từ self-consistency core/mart hoặc physical row counts.
7. Offline clean project, consistent backup/restore, queued MQTT trước retry,
   SQLite WAL và Spark resume, scenario reopen và 8 screens; full historical
   snapshot và cửa sổ sensor được báo phạm vi tường minh.
8. Cutover rồi phát sinh source/config/scenario mới và rollback có delta journal;
   checksum/KPI/acknowledged commands còn đủ trước mở lại reader/writer.

Bằng chứng mới đặt trong `docs/evaluation/assets/clickhouse-*`; raw/dump/video
ngoài Git. Không sửa số liệu hoặc nhãn engine của bằng chứng PostgreSQL cũ.
