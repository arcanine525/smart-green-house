# 02 — Services và lưu trữ local

Một máy chạy Docker Compose. Spark dùng local mode trong container; không dựng master/worker cluster cho bản đầu. Tên service/volume dưới đây là thiết kế để hiện thực ở T04.

```mermaid
flowchart TB
    BROWSER["Browser<br/>localhost:8501"]
    CLI["Makefile / Python CLI<br/>setup, batch, replay, test, backup"]
    subgraph HOST["Máy local — Docker Compose network"]
        APP["app<br/>Streamlit + query services"]
        DB["db<br/>PostgreSQL :5432 nội bộ"]
        JOB["batch-job<br/>PySpark + coordinator"]
        STREAM["stream-worker<br/>PySpark + coordinator"]
        SIM["simulator<br/>Python + MQTT client"]
        BROKER["broker<br/>Mosquitto :1883 nội bộ"]
        COL["collector<br/>Python + durable writer"]
        TEST["test-runner<br/>fixture + fault suite"]
        PGV[("pg_data volume")]
        RAW[("raw snapshot / raw-ready<br/>host bind mount")]
        SPOOL[("collector_spool volume")]
        CP[("spark_checkpoints volume")]
        BV[("broker_state volume")]
        OUT[("simulator_outbox volume")]
    end
    BROWSER --> APP --> DB
    CLI --> JOB
    CLI --> SIM
    CLI --> TEST
    JOB --> DB
    STREAM --> DB
    SIM --> BROKER --> COL
    SIM --- OUT
    BROKER --- BV
    COL --- SPOOL
    COL -->|"publish file hoàn chỉnh"| RAW
    RAW -->|"read only"| JOB
    RAW -->|"read only"| STREAM
    RAW -->|"snapshot read only"| SIM
    STREAM --- CP
    DB --- PGV
    TEST -.->|"test scope riêng"| DB
    TEST -.-> BROKER
```

| Profile/lệnh dự kiến | Thành phần cần chạy |
|---|---|
| Batch | db, app; batch-job chạy theo lệnh |
| Sensor | db, app, broker, collector, stream-worker; simulator theo lệnh |
| Test | Dịch vụ tùy test, database/scope/volume kiểm thử riêng |

- Chỉ app cần expose mặc định trên `127.0.0.1`; cổng DB/MQTT cho debug nếu bật cũng bind localhost. Giao tiếp container dùng service name, không dùng localhost của host.
- Healthcheck và retry giới hạn xử lý thứ tự khởi động; `depends_on` đơn thuần không phải cơ chế phục hồi.
- Image chứa sẵn Python/Java, JDBC và MQTT dependencies tương thích; không tải package lúc demo. Credentials qua cấu hình local không commit.
- Raw-ready và thư mục file tạm dùng cùng filesystem để atomic rename. File đã công bố không append/sửa tiếp.
- Backup nhất quán dùng điểm dừng/drain; giữ raw + manifests + cấu hình để có thể dựng lại DB. Không phục hồi checkpoint tiến xa hơn DB nếu chưa replay/đối soát phần thiếu.
- Hạn mức CPU/RAM, kích thước batch, disk budget và backpressure được cấu hình và đo tại T25/T36; chưa khẳng định cấu hình tối thiểu.
