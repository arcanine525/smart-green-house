# Kiến trúc Smart Greenhouse — PostgreSQL trước, ClickHouse sau

> Thiết kế triển khai local cho Group 29. Các sơ đồ là Mermaid source, không phải bằng chứng hệ thống đã chạy. Sáu [BR](../br/) là hợp đồng nghiệp vụ; sensor replay là mở rộng BR-01/03, còn ClickHouse là roadmap tùy chọn.

| Tài liệu | Sơ đồ | Task liên quan |
|---|---|---|
| [01 — Tổng quan](01-overview.md) | Nguồn, batch/stream, warehouse, ứng dụng | T02, T31 |
| [02 — Triển khai local](02-local-deployment.md) | Services, network, volumes | T04, T28 |
| [03 — Sensor ingestion](03-sensor-ingestion.md) | Simulator/collector; sequence ACK và lưu bền | T32–T33 |
| [04 — Processing và publication](04-processing-publication.md) | Transform dùng chung; publish/recovery | T06–T08, T14, T34 |
| [05 — Core schema](05-core-schema.md) | ERD nghiệp vụ và chất lượng theo measure | T07, T18 |
| [06 — Metadata và cấu hình](06-metadata-schema.md) | Lineage/publication; rule/tariff/scenario | T05, T08, T20 |
| [07 — Data marts](07-data-marts.md) | Bus dimensions và sáu luồng mart cho sáu BR | T09–T21 |
| [08 — Ứng dụng và contracts](08-application-contracts.md) | UI/service/adapter; sequence scenario | T10, T21–T23, T31, T35 |
| [09 — Chuyển ClickHouse](09-clickhouse-migration.md) | Backfill, parity, cutover/rollback | T37–T39, tùy chọn |

## Quy ước dùng chung

- Engine đầu tiên: PostgreSQL thuần. Không bắt buộc TimescaleDB, Kafka hoặc database thứ hai.
- `data_scope_id`: phân biệt snapshot lịch sử, từng replay và từng fault run. `source_record_id` xác định dòng nguồn ổn định; `event_id` xác định sự kiện trong một replay, không thay đổi khi retry.
- `revision_id`: phiên bản bản ghi đã chuẩn hóa/correction, bất biến. Core giữ revisions; một publication chọn chính xác revision nào được dùng. Grain nghiệp vụ không bao gồm các bản sao revision khi tính KPI.
- `publication_id`: bộ dữ liệu được kiểm tra và công bố, bao gồm lựa chọn core revisions, versions của cấu hình và marts. App pin ID này cho cả màn hình/export. Bản mới không sửa dữ liệu của publication cũ.
- Timestamp nguồn chưa rõ timezone giữ nguyên dưới dạng `timestamp without time zone` và serial gốc; `published_at`, `received_at`, `created_at` là `timestamptz`. Không tự gắn UTC cho thời gian AGC.
- NULL khác 0. Quality theo từng measure; coverage, completeness, basis và reason codes đi cùng KPI.
- Mỗi ERD là schema logic rút gọn, chưa phải DDL đầy đủ. FK/composite UNIQUE, hiệu lực nửa mở và các kiểm tra liên bảng phải được triển khai bằng migration cùng tests.
- Tên bảng dùng snake_case; tên BR có thể dùng PascalCase tương ứng. Tất cả diagrams dùng Mermaid `flowchart`, `sequenceDiagram` hoặc `erDiagram`.

## Thứ tự triển khai

Nền tảng + contracts → BR-02 xuyên suốt → BR-05 → BR-01/03 → BR-04/06 → tích hợp sensor và nghiệm thu → bàn giao. Simulator/collector có thể làm song song với các mart khi contracts đã ổn định. Truy vết BR, task và bằng chứng dự kiến nằm trong [ma trận yêu cầu](../implementation/requirements-matrix.md); quyết định stack nằm trong [ADR-001](../adr/001-local-deployment-stack.md).
