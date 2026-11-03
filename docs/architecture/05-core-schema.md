# 05 — Core schema PostgreSQL

Core giữ đúng grain nguồn, metadata nghiệp vụ và lịch sử correction. Trong ERD, `revision_id` là khóa bất biến của một phiên bản; một logical record có thể có nhiều revision, nhưng publication chỉ chọn một. Truy vấn core cho UI phải qua membership của publication, không đọc tất cả revision rồi SUM.

```mermaid
erDiagram
    SITE ||--o{ COMPARTMENT : contains
    COMPARTMENT ||--o{ GROWING_CYCLE : hosts
    TEAM ||--o{ GROWING_CYCLE : operates
    CROP ||--o{ GROWING_CYCLE : grown_in
    GROWING_CYCLE ||--o{ RECORD_REVISION : contextualizes
    DATA_SCOPE ||--o{ RECORD_REVISION : isolates
    SOURCE_RECORD ||--o{ RECORD_REVISION : derives
    RECORD_REVISION ||--o| CLIMATE_OBSERVATION : typed_as
    RECORD_REVISION ||--o| DRAIN_OBSERVATION : typed_as
    RECORD_REVISION ||--o| RESOURCE_DAY : typed_as
    RECORD_REVISION ||--o| HARVEST : typed_as
    COMPARTMENT ||--o{ AREA_BASIS_VERSION : has
    SITE {
        string site_id PK
        string name
        string source_time_basis
    }
    COMPARTMENT {
        string compartment_id PK
        string site_id FK
        string name
    }
    TEAM {
        string team_id PK
        string name
        boolean is_reference
    }
    CROP {
        string crop_id PK
        string species
        string cultivar
    }
    GROWING_CYCLE {
        string cycle_id PK
        string compartment_id FK
        string team_id FK
        string crop_id FK
        timestamp start_source_time
        timestamp end_source_time_exclusive
    }
    DATA_SCOPE {
        string data_scope_id PK
        string source_snapshot_id FK
        string mode
        string replay_id
    }
    SOURCE_RECORD {
        string source_record_id PK
        string source_file_id FK
        int row_number
        string raw_hash
    }
    RECORD_REVISION {
        string revision_id PK
        string data_scope_id FK
        string cycle_id FK
        string source_record_id FK
        string record_type
        string logical_record_key
        int revision_number
        string content_hash
        string correction_version
    }
    CLIMATE_OBSERVATION {
        string revision_id PK, FK
        timestamp slot_start
        decimal source_time_serial
        decimal tair
        decimal co2air
        decimal t_heat_sp
        decimal t_vent_sp
        decimal co2_sp
        decimal t_heat_vip
        decimal t_ventlee_vip
        decimal co2_vip
    }
    DRAIN_OBSERVATION {
        string revision_id PK, FK
        timestamp slot_start
        string measurement_context
        decimal ec_drain
        decimal ph_drain
    }
    RESOURCE_DAY {
        string revision_id PK, FK
        date resource_date
        decimal irrigation_l_m2
        decimal drain_l_m2
        decimal heat_mj_m2
        decimal elec_high_kwh_m2
        decimal elec_low_kwh_m2
    }
    HARVEST {
        string revision_id PK, FK
        date harvest_date
        decimal prod_a_kg_m2
        decimal prod_b_kg_m2
    }
    AREA_BASIS_VERSION {
        string basis_version_id PK
        string compartment_id FK
        string measure_context
        date valid_from
        date valid_to_exclusive
        decimal area_m2
        string basis_kind
        string verification_status
        string evidence_reference
    }
```

Một source row Climate sinh logical records climate và drain riêng, cùng source reference. Các subtype của `RECORD_REVISION` loại trừ nhau theo `record_type`; đây là invariant cần constraint/validation, không suy ra tự động từ các cạnh ERD.

| Entity | Grain nghiệp vụ trong một scope | Kiểm tra bắt buộc |
|---|---|---|
| ClimateObservation | cycle × slot 5 phút | Requested/realized riêng; điểm cuối vụ không tạo thêm duration |
| DrainObservation | cycle × slot × context | Context drain process computer; quality EC/pH độc lập |
| ResourceDay | cycle × ngày | Một nguồn tổng ngày; không SUM counter 5 phút vào cùng total |
| Harvest | cycle × source record | Không mặc định một harvest/ngày; giữ record ID |
| AreaBasisVersion | compartment × measure context × hiệu lực × version | Một mapping hợp lệ cho measure/ngày; không tự lấy floor area |

`RECORD_REVISION` unique `(scope, record_type, logical_record_key, revision_number)`. Trong publication, logical record chỉ chọn một revision; chỉ đúng scope/cycle/source được phép liên kết. FK đảm bảo quan hệ; slot uniqueness, subtype và temporal overlap cần kiểm tra thêm. Các bảng fact có thể giữ cột NULL; dùng trạng thái quality để quyết định accepted thay vì bỏ cả row.

```mermaid
erDiagram
    RECORD_REVISION ||--o{ MEASURE_QUALITY : assessed_by
    QUALITY_POLICY_VERSION ||--o{ MEASURE_QUALITY : applies
    RECORD_REVISION ||--o{ DQ_ISSUE : explains
    SOURCE_RECORD ||--o{ QUARANTINE_RECORD : retained_as
    RECORD_REVISION {
        string revision_id PK
        string logical_record_key
        string content_hash
    }
    QUALITY_POLICY_VERSION {
        string policy_version_id PK
        string definition_hash
        string evidence_reference
    }
    MEASURE_QUALITY {
        string revision_id PK, FK
        string measure_code PK
        string policy_version_id PK, FK
        string status
        string reason_code
    }
    DQ_ISSUE {
        string issue_id PK
        string revision_id FK
        string measure_code
        string reason_code
        string evidence_reference
    }
    SOURCE_RECORD {
        string source_record_id PK
        string raw_reference
    }
    QUARANTINE_RECORD {
        string quarantine_id PK
        string source_record_id FK
        string raw_reference
        string event_id
        string reason_code
    }
```

Với payload chưa parse được, `source_record_id` có thể chưa xác định: quarantine vẫn phải giữ raw reference và delivery identity. MEASURE_QUALITY dùng bốn trạng thái accepted/missing/invalid/suspect; đánh giá ngoài ngưỡng nghiệp vụ nằm ở mart, không phải lỗi cảm biến mặc định.
