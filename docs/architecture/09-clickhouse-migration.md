# 09 — Roadmap chuyển PostgreSQL sang ClickHouse

> T37–T39 tùy chọn sau MVP. Sơ đồ này không đưa ClickHouse vào deployment bắt buộc hiện tại.

Không đổi simulator, MQTT, raw format, source identity hoặc định nghĩa sáu BR chỉ để chuyển engine. Thiết kế vật lý, SQL marts, dedup, publication và cách lưu scenario cần adapter riêng; đây không phải thao tác thay connection string.

```mermaid
flowchart TB
    RAW[("Raw snapshot / events<br/>stable source identity")]
    CONF[("Export rule, tariff, scenario<br/>basis, correction, metadata versions")]
    SEM["Shared semantic transforms"]
    RAW --> SEM
    SEM --> PG["PostgreSQL adapter<br/>baseline MVP"]
    SEM --> CH["ClickHouse adapter<br/>ứng viên sau MVP"]
    CONF --> PG
    CONF --> CH
    PG --> PGR["6 BR + fault suite results"]
    CH --> CHR["6 BR + fault suite results"]
    PGR --> CHECK{"Parity, lineage, recovery<br/>và benchmark đạt?"}
    CHR --> CHECK
    CHECK -->|"Chưa đạt"| KEEP["Giữ PostgreSQL<br/>sửa ứng viên"]
    CHECK -->|"Đạt"| FREEZE["Dừng producer và config writes<br/>drain, khóa cutoff manifest"]
    FREEZE --> DELTA["Nạp phần cuối<br/>đối soát publication"]
    DELTA --> SWITCH["Đổi app query / command adapter<br/>sang ClickHouse"]
    SWITCH --> OBS["Theo dõi correctness và latency"]
    OBS -->|"Cần quay lại"| REPLAY["Nạp raw và config versions mới<br/>về PostgreSQL, kiểm chứng"]
    REPLAY --> BACK["Đổi adapter về PostgreSQL"]
```

| Giữ nguyên | Thiết kế lại / kiểm chứng |
|---|---|
| Raw và event contracts | DDL/types, sorting key, partition và compression |
| PySpark transforms thuần nghiệp vụ | Sink batch, retry/dedup và query SQL theo engine |
| BR formulas, fixtures và expected outputs | Correction/version selection trước tổng hợp |
| UI và DTO contracts | Publication nhất quán khi không có cùng transaction model |
| Rule/tariff/scenario definitions | Command persistence, idempotency và khôi phục cấu hình |

Không dựa vào background merge để bảo đảm KPI hết duplicate ngay. Không cộng incremental aggregate từ một nguồn còn có thể phát duplicate/correction mà chưa có cơ chế sửa tổng. Bản đầu của adapter có thể tái tính marts từ tập revision canonical theo publication, sau đó tối ưu bằng bằng chứng benchmark.

Khi so PostgreSQL/ClickHouse, giữ Hybrid và workload giống nhau; khi so Hybrid/Kimball ở T24, giữ engine giống nhau. Công bố riêng hai thí nghiệm để không quy sai nguyên nhân cải thiện.

Chuyển đổi ban đầu dùng backfill từ raw và khoảng dừng có kiểm soát, không cần CDC/dual-write. Mọi lần quay lại sau cutover phải mang theo events và configuration versions mới; PostgreSQL snapshot cũ một mình không đủ. Không xóa nguồn hoặc DB đối chứng trước khi nghiệm thu.
