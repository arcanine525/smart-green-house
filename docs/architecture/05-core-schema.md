# 05 — Core schema ClickHouse

Các bảng là MergeTree append-only. Quan hệ trong ERD là quan hệ logic được
repository/manifests kiểm, không phải foreign key hoặc unique constraint CH.

```mermaid
erDiagram
    source_revision ||--|{ source_measure : "scope_id + revision_id"
    source_revision ||--o{ load_member : "frozen revision membership"
    load_receipt ||--|{ load_member : "scope_id + load_id"
    load_receipt ||--|{ load_file : "frozen ingestion audit"
    load_receipt ||--o{ load_issue : "quarantine and conflicts"
    source_revision {
        String scope_id
        String snapshot_id
        String source_record_id
        String record_type
        String compartment_id
        String logical_key
        UInt32 revision
        String revision_id
        String content_hash
        String source_timestamp
        Int64 source_clock_us
        String correction_id
        String payload
    }
    source_measure {
        String scope_id
        String revision_id
        String measure_code
        String value_exact
        Decimal256 value_decimal
        String projection_status
        String null_reason
    }
    load_member {
        String scope_id
        String load_id
        String revision_id
    }
    load_receipt {
        String scope_id
        String load_id
        String membership_checksum
        UInt64 membership_count
        String audit_checksum
    }
    load_file {
        String source_file
        String metadata
    }
    load_issue {
        String source_record_id
        String disposition
        String payload
    }
```

Semantic key: Climate/Resources dùng type × compartment × logical time; harvest
còn giữ source record identity để không gộp các record thu hoạch cùng ngày.
Revision ordinal liên tiếp và correction parent được kiểm trước append. Hai
payload khác nhau cho cùng ordinal là conflict; không chọn last-write-wins.
Published membership giữ revision cụ thể, nên correction không đổi pin cũ.

`value_exact` là biểu diễn Decimal canonical; Decimal(76,38) / Decimal256 chỉ là projection
khi round-trip chính xác. Giá trị ngoài miền projection vẫn có exact String,
không truncate hoặc đi qua Float64 để tính tiền/KPI. Source clock lưu string và
microseconds naive; received/operating clock có UTC thật.

Physical retries giống nhau được collapse trước aggregate. Không đợi background
merge để tuyên bố số liệu đúng. `source_disposition` giữ audit dòng không đủ điều
kiện core, không biến quarantine thành missing giả.
