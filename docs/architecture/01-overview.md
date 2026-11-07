# 01 — Kiến trúc tổng quan

Warehouse Hybrid: raw bất biến → canonical core có revision → typed marts theo
publication. ClickHouse là engine mặc định; PostgreSQL là adapter đối chứng và
rollback riêng. Native batch dùng Python; Spark thực sự chạy ở sensor streaming.

```mermaid
flowchart TB
    subgraph SOURCE["Nguồn local"]
      AGC["AGC ZIP, ReadMe, Economics"]
      CFG["Quality, basis, rule, tariff versions"]
    end
    AGC --> RAW["Locked raw + checksummed manifest"]
    RAW --> PARSE["Shared parser / quarantine / source identity"]
    RAW --> SIM["Simulator + SQLite outbox"]
    SIM --> MQTT["MQTT QoS 1"] --> COL["Durable collector spool"]
    COL --> READY["Atomic ready JSONL"] --> SPARK["Spark Structured Streaming"]
    PARSE --> WRITER["Shared coordinator: flock + pending fence + rollback journal"]
    SPARK --> WRITER
    CFG --> WRITER
    WRITER --> CORE["ClickHouse source revisions / exact measures"]
    CORE --> MART["Water, harvest, climate, drain, energy typed marts"]
    MART --> SEAL["Verify all bundles → COMPLETE publication"]
    SPARK --> REPLAY["Verified replay batches / immutable replay pins"]
    SEAL --> SVC["Shared typed query / command services"]
    REPLAY --> SVC
    SVC --> UI["Streamlit / Plotly / CSV / JSON"]
    SEAL -. "freeze, export, map, rebuild, parity" .-> PG["Optional PostgreSQL rollback"]
```

Historical resource/production facts và replay Climate không được gộp scope.
Replay bổ sung BR-01/03; không tạo dữ liệu năng lượng hoặc thu hoạch từ sensor
Climate. Cost/intensity là service trên quantities có nguồn, không phải số giả
trong UI. Diagram không bao gồm cloud, actuator hoặc mô hình dự đoán.

[Deployment](02-local-deployment.md) · [schema](05-core-schema.md) ·
[marts](07-data-marts.md) · [recovery](10-durable-recovery.md).
