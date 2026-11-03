# 06 — Metadata, lineage và cấu hình

Schema `meta` ghi nguồn, ingestion, publication và tiến độ replay; schema `config` giữ cấu hình có version. Đây là dữ liệu cần chuyển cùng raw khi migrate, đặc biệt các scenario do người dùng tạo.

```mermaid
erDiagram
    DATASET_SNAPSHOT ||--o{ SOURCE_FILE : includes
    SOURCE_FILE ||--o{ SOURCE_RECORD : contains
    DATASET_SNAPSHOT ||--o{ DATA_SCOPE : anchors
    DATA_SCOPE ||--o{ PIPELINE_RUN : processes
    DATA_SCOPE ||--o{ SENSOR_EVENT : receives
    SOURCE_RECORD ||--o{ SENSOR_EVENT : replayed_as
    PIPELINE_RUN ||--o{ PROCESSED_BATCH : commits
    PIPELINE_RUN ||--o{ PUBLICATION : creates
    PUBLICATION ||--o{ PUBLICATION_MEMBER : selects
    RECORD_REVISION ||--o{ PUBLICATION_MEMBER : included_in
    DATA_SCOPE ||--o| CURRENT_PUBLICATION : points_to
    PUBLICATION ||--o| CURRENT_PUBLICATION : current_for
    PUBLICATION ||--o{ COMPLETENESS : documents
    DATASET_SNAPSHOT {
        string snapshot_id PK
        string archive_checksum
        string source_url
    }
    SOURCE_FILE {
        string source_file_id PK
        string snapshot_id FK
        string relative_path
        string checksum
        int expected_rows
    }
    SOURCE_RECORD {
        string source_record_id PK
        string source_file_id FK
        int row_number
    }
    DATA_SCOPE {
        string data_scope_id PK
        string snapshot_id FK
        string mode
        string replay_manifest_ref
    }
    SENSOR_EVENT {
        string data_scope_id PK, FK
        string event_id PK
        string source_record_id FK
        string payload_hash
        timestamp source_observed_at
        timestamptz published_at
        timestamptz first_received_at
    }
    PIPELINE_RUN {
        string run_id PK
        string data_scope_id FK
        string status
        string code_version
        string transform_version
    }
    PROCESSED_BATCH {
        string stream_id PK
        string checkpoint_generation PK
        bigint batch_id PK
        string run_id FK
        string publication_id FK
    }
    PUBLICATION {
        string publication_id PK
        string run_id FK
        string data_scope_id FK
        string quality_policy_version
        string metric_bundle_version
        string config_bundle_hash
        timestamp observed_through
        timestamptz committed_at
        string finality_status
    }
    PUBLICATION_MEMBER {
        string publication_id PK, FK
        string record_type PK
        string logical_record_key PK
        string revision_id FK
    }
    RECORD_REVISION {
        string revision_id PK
        string data_scope_id FK
        string logical_record_key
    }
    CURRENT_PUBLICATION {
        string data_scope_id PK, FK
        string publication_id FK
    }
    COMPLETENESS {
        string publication_id PK, FK
        string cycle_id PK
        string source_type PK
        string status
        string evidence_reference
    }
```

SENSOR_EVENT là bản envelope hợp lệ canonical; raw delivery log giữ mọi lần giao, duplicate và conflict. Cùng event ID khác payload không được ghi đè canonical im lặng. Bảng `PROCESSED_BATCH` và publication cùng transaction; publication membership phải kiểm tra revision thuộc đúng scope và logical key.

Completeness Resources/Production là bằng chứng đủ snapshot và xử lý nguồn; không suy ra từ tỷ lệ ngày có harvest. Finality của replay và completeness từng nguồn là các trạng thái riêng.

```mermaid
erDiagram
    RULE_VERSION ||--o{ RULE_SCOPE : applies_to
    TARIFF_VERSION ||--o{ TARIFF_RATE : contains
    TARIFF_VERSION ||--o{ SCENARIO_VERSION : baseline_for
    SCENARIO_VERSION ||--|{ SCENARIO_RATE : defines
    PUBLICATION ||--o{ PUBLICATION_CONFIG : pins
    RULE_VERSION {
        string rule_version_id PK
        string metric_code
        string basis
        string unit
        decimal lower_value
        decimal upper_value
        decimal tolerance
        boolean is_demo
        string evidence_reference
    }
    RULE_SCOPE {
        string rule_scope_id PK
        string rule_version_id FK
        string cycle_id FK
        timestamp valid_from
        timestamp valid_to_exclusive
    }
    TARIFF_VERSION {
        string tariff_version_id PK
        string currency
        string evidence_reference
    }
    TARIFF_RATE {
        string rate_id PK
        string tariff_version_id FK
        string component
        string price_unit
        decimal rate
        date valid_from
        date valid_to_exclusive
    }
    SCENARIO_VERSION {
        string scenario_version_id PK
        string scenario_id
        int version_number
        string baseline_tariff_version_id FK
        string name
        string assumptions
        timestamptz created_at
    }
    SCENARIO_RATE {
        string scenario_rate_id PK
        string scenario_version_id FK
        string component
        string price_unit
        decimal rate
        date valid_from
        date valid_to_exclusive
    }
    PUBLICATION {
        string publication_id PK
        string config_bundle_hash
    }
    PUBLICATION_CONFIG {
        string publication_id PK, FK
        string config_type PK
        string config_version_id PK
    }
```

`PUBLICATION_CONFIG` là biểu diễn logic của tập tham chiếu version: DDL dùng junction tables có FK cụ thể cho rule, quality, metric, basis, correction và tariff; không dùng một polymorphic ID không kiểm tra được tham chiếu. Scenario được pin thêm trong query/result identity; khi materialize kết quả scenario phải giữ publication nguồn.

Rate intervals nửa mở, không overlap theo component/version; gap ngăn tính canonical total. Baseline không bị sửa khi tạo scenario. Giá heat lịch sử giữ đúng `0.0083 EUR/MJ`; tiền dùng decimal. Scenario chỉ thay giá, giữ quantities và area basis cùng publication; nguồn daily không hỗ trợ đổi lịch giờ cao/thấp điểm.

RULE_VERSION có metric-specific parameters: BR-01 dùng setpoint ± tolerance; BR-03 dùng lower/upper profile. Không áp cột lower/upper tĩnh cho toàn bộ cơ chế setpoint BR-01.
