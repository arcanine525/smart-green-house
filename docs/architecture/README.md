# Kiến trúc Smart Greenhouse — ClickHouse local

Các sơ đồ Mermaid mô tả hệ thống đã chạy: Hybrid core/marts trên ClickHouse,
Python historical batch, MQTT và Spark Structured Streaming cho sensor replay.
PostgreSQL là adapter đối chứng/rollback với project và checkpoint riêng.

| Tài liệu | Nội dung |
|---|---|
| [01 — Tổng quan](01-overview.md) | Raw, hai đường ingest, warehouse và ứng dụng |
| [02 — Deployment](02-local-deployment.md) | Services, internal network, relay, bind/named volumes |
| [03 — Sensor](03-sensor-ingestion.md) | Durable outbox, MQTT, collector, ACK và checkpoint |
| [04 — Publication](04-processing-publication.md) | Revision, bundle proof, immutable complete pin |
| [05 — Core schema](05-core-schema.md) | Logical keys, exact measure, membership và audit |
| [06 — Metadata/config](06-metadata-schema.md) | Full envelopes, versions, request identity và journal |
| [07 — Marts](07-data-marts.md) | Grain/công thức/dữ liệu phục vụ sáu BR |
| [08 — Application](08-application-contracts.md) | UI/services/adapters/DTO và lưu scenario |
| [09 — Migration](09-clickhouse-migration.md) | Cutoff, delta, reverse import và parity |
| [10 — Recovery](10-durable-recovery.md) | Quiescent backup, WAL/checkpoint và restore/rebuild |

ERD rút gọn diễn tả **quan hệ logic**, không khẳng định ClickHouse có SQL FK hoặc
UNIQUE constraint. Append-only retries chỉ collapse khi identity và full payload
khớp; conflict phải báo lỗi. Publication giữ revision/config cụ thể, không tự đọc
latest vào pin cũ. Source time chưa xác minh timezone khác operating clock UTC.
Missing, NULL, zero, suspect, invalid và partial giữ nghĩa riêng.

[Sáu BR](../br/) là hợp đồng nghiệp vụ; [ADR-003](../adr/003-clickhouse-warehouse.md)
ghi lựa chọn và giới hạn. [Ma trận yêu cầu](../implementation/requirements-matrix.md)
và [evidence](../evaluation/README.md) ghi phiên bản, lệnh và phạm vi đã kiểm.
Sơ đồ không thay cho bằng chứng chạy thật. ADR-001/002 giữ lịch sử PostgreSQL.
