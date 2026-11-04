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

## Metadata đã triển khai và phần tiếp nhận sau

[Migrations/contract hiện tại](../../src/greenhouse/db/README.md) triển khai các entity cần cho một load core từ staging T06:

- `meta.schema_migration`: file version/SHA-256/applied_at, transaction + advisory lock; skip checksum khớp, đổi checksum fail.
- `meta.dataset_snapshot`, `data_scope`, `source_folder_mapping`, `source_file`: snapshot identity, scope historical/replay/fault và mapping có nguồn; không tự đặt số khoang/area. Scope composite FK gắn đúng snapshot.
- `meta.pipeline_run`, `file_ingestion`, `source_record`: run manifest gốc, ingestion/core statuses riêng, counts cân, output-consumability, stable source ID, file/physical line/raw hash/reference/serial/time precision. Loader không glob; failed/deferred/outputs_consumable=false không thành core loaded. Run failed chỉ lưu metadata blocked; run unfinished bị từ chối.
- `meta.dq_issue`, `quarantine_record`, `correction`: giữ parse issues và per-measure quality issues ở hai lớp; số issue không phải số record lỗi duy nhất. Correction yêu cầu source, old/new, actor/evidence/version, append-only; không tự sửa năm. Reference ngoài vụ còn quarantine nên T12 phải giữ completeness false. `meta.harvest_ingestion_state` chỉ chứng minh tình trạng giải quyết source rows, chưa phải kết luận completeness nghiệp vụ.
- `config.measure_definition`, `quality_policy_version`: unit/type catalog; policy effective intervals, definition/hash, evidence/demo label; version bất biến. MEASURE_QUALITY còn có applicability unknown để giữ off_mode_unverified riêng với numeric validity.
- `config.area_basis_version`, `basis_assumption_version`, `assumption_application`, `basis_application`: Production verified 62.5; shared analysis `AGC_SHARED_BASIS_V1` assumed_unverified; phương án `AGC_FLOOR_96_SENSITIVITY_V1` riêng, tắt mặc định. Selection tường minh theo scope/khoang/measure/context và hiệu lực; không chọn latest hay thăng assumption thành verified. Tắt assumption/application mở NULL path; version cũ giữ nguyên.
- `meta.publication`, `publication_member`, `publication_basis`, `current_publication`: version/membership/config pin/pointer. Composite FK chặn sai scope/key/revision/basis; publication không bị sửa. `core.published_measure` dùng quality policy và basis được pin, không lấy toggle hiện tại. Không có publication được tạo bởi loader T07.
- `meta.sensor_event`: schema dự phòng theo ERD, source_observed_at naive, published_at/first_received_at timestamptz; chưa có producer/collector/replay ghi vào bảng này.

Các entity rule/tariff/scenario, processed_batch, publication completeness và junction config bổ sung vẫn là thiết kế cho T14–T21/T32–T36, chưa được tạo hoặc chạy ở T07. T07 chỉ pin quality policy và basis vì chưa có metric/rule/tariff versions để tham chiếu; không dùng polymorphic config ID giả. Loader nạp một transaction, từ chối run trùng; không triển khai resume/upsert correction/recovery. Coordinator bổ sung bên dưới; T31 tiếp nhận adapter SQL/DTO.

## Workflow và publication thực thi

Migration 004 bổ sung `meta.workflow_run` (scope/snapshot/input/versions/status/ingestion/publication), `workflow_step` (bốn step, attempt/status/times/counts/versions/errors/artifact checksum), `workflow_candidate` (frozen revision IDs), `workflow_basis` (frozen basis/context), `core_baseline_receipt` (parser/policy/exact-value fingerprint/count) và `publication_seal` (publication/workflow/membership checksum/count/Spark artifact checksum). Receipt và seal bất biến. Typed values và quality cũng append-only để baseline receipt/historical publication giữ nguyên ý nghĩa.

Run mới cùng snapshot ghi metadata reused khi raw/hash/key/values không đổi; không tạo source/revision mới. Correction tường minh append cả audit và một revision n+1, raw/source identity không đổi. Snapshot mới có scope riêng và basis cần quyết định riêng. Publication pin quality và basis; membership/basis bị khóa sau seal. Pointer chỉ nhận publication đã seal. Workflow/step completed cập nhật trong cùng transaction publication, tránh ghi “failed” giả nếu client bị ngắt ngay sau commit. Xem [contract vận hành](../../src/greenhouse/db/README.md#workflow-chạy-lại-và-publication).
