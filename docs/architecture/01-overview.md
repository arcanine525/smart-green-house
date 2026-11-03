# 01 — Kiến trúc tổng quan

Kiến trúc Hybrid: raw độc lập database → staging → core chuẩn hóa có version → dimensional marts. Sensor giả lập và nguồn batch hội tụ vào cùng quy tắc chất lượng/nghiệp vụ; người dùng chọn historical hoặc replay scope.

```mermaid
flowchart TB
    subgraph INPUT["Nguồn local"]
        AGC["AGC snapshot<br/>Climate, Resources, Production"]
        CFG["ReadMe / Economics<br/>Rule và basis có nguồn"]
    end
    subgraph INGEST["Tiếp nhận"]
        BATCH["Batch importer<br/>checksum + manifest"]
        SIM["Python simulator<br/>replay Climate + fault schedule"]
        MQTT["Mosquitto<br/>MQTT QoS 1"]
        COL["Python collector<br/>durable spool + atomic files"]
    end
    RAW[("Filesystem raw<br/>snapshot + event files")]
    subgraph PROCESS["Xử lý local"]
        SB["Spark batch"]
        SS["Spark Structured Streaming<br/>file source + checkpoint"]
        SEM["Shared transforms<br/>time, units, quality, episode"]
        PUB["PostgreSQL writer<br/>validate + publish"]
    end
    subgraph DB["PostgreSQL"]
        STG[("staging<br/>theo run / batch")]
        CORE[("core<br/>revision + business entities")]
        META[("meta / config<br/>lineage, rules, publications")]
        MART[("mart<br/>facts, summaries, KPI")]
    end
    APP["Query / command services<br/>PostgreSQL adapter"]
    UI["Streamlit + Plotly<br/>6 BR + live replay + CSV export"]
    USER["Người vận hành / kỹ sư / quản lý"]
    AGC --> BATCH --> RAW
    AGC --> SIM --> MQTT --> COL --> RAW
    RAW --> SB --> SEM
    RAW --> SS --> SEM
    SEM --> STG --> PUB
    CFG --> META
    META --> PUB
    PUB --> CORE
    CORE -->|"SQL mart builders"| MART
    PUB -->|"publish sau khi marts đạt"| META
    CORE --> APP
    MART --> APP
    META --> APP
    APP --> UI --> USER
    USER -->|"tạo scenario"| APP
    APP -->|"version mới"| META
```

Ứng dụng chỉ đọc publication đã công bố; các mũi tên core → mart không có nghĩa app được phép đọc mart đang xây. State của pipeline, broker và DB đều nằm local. Sau bước tải dataset/images/dependencies, demo và test phải chạy khi ngắt Internet.

Raw Climate có thể nạp trực tiếp để phân tích lịch sử hoặc phát lại qua MQTT để kiểm chứng đường sensor. Hai đường có scope riêng; không UNION chúng để tăng số lượng quan sát. Resources/Production tiếp tục giữ grain nguồn, không giả lập thành cảm biến tần suất cao.

Các phần đã chọn: PostgreSQL, Python/PySpark, Mosquitto, Streamlit/Plotly, filesystem, Docker Compose. ClickHouse chỉ xuất hiện trong [roadmap chuyển đổi](09-clickhouse-migration.md).
