# 06 — Metadata, cấu hình và journal

Một rule/tariff/scenario version lưu trong một complete envelope: header và
segments/rates không bị lộ một phần. Request ID và content hash được kiểm dưới
shared writer lock. Publication đóng băng config pins, không đọc latest config
để thay ngược lịch sử.

```mermaid
erDiagram
    configuration_envelope }o--o{ publication_state : "frozen pins in payload"
    load_receipt ||--o{ publication_state : "load_id in envelope"
    publication_state ||--o{ water_header : "publication_id"
    publication_state ||--o{ harvest_header : "publication_id"
    publication_state ||--o{ threshold_header : "publication_id"
    publication_state ||--o{ energy_header : "publication_id"
    configuration_envelope {
        String kind
        String scope_id
        String version_id
        String request_id
        UInt16 schema_version
        String content_hash
        String payload
    }
    publication_state {
        String scope_id
        String workflow_id
        String publication_id
        UInt64 generation
        String stage
        String content_hash
        String payload
    }
    load_receipt {
        String load_id
        String membership_checksum
        String audit_checksum
    }
```

Các family gồm snapshot, area basis, quality/bundle configuration, irrigation/
harvest rules, threshold schedules, tariffs, cost scenarios, replay config và
migration/rebuild provenance. Scenario ghim publication, context, baseline,
changes, derived rates và request/version chain. Rule mới không thành nguồn
xác minh nông học; area assumption có status/evidence riêng.

```mermaid
flowchart LR
    CMD["Supported writer"] --> L["Shared flock"]
    L --> Q["pending-write.json + query ID fence"]
    L --> J["SQLite cutover-journal.sqlite<br/>session / intent / outcome"]
    J --> B["Baseline hash + namespace + sequence/hash chain"]
    Q --> T["Terminal query_log reconciliation"]
    T --> O["Durable outcome before acknowledgment"]
    J --> COPY["SQLite backup API + checksum/cursor"]
```

SQLite journal ở coordinator filesystem, không phải bảng ClickHouse. Bản
PostgreSQL rollback giữ archive native trong `meta.clickhouse_cutover_archive`,
config envelopes trong `meta.clickhouse_configuration_archive` và source/target
scenario mappings riêng. Trùng ID giữa hai lineage không cho phép ghi đè version
bất biến; mapping giữ original IDs và kiểm retry.
