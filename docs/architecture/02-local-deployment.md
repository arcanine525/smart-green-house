# 02 — Deployment local

`compose.yaml` include `compose.clickhouse.yaml`. App mặc định không có dependency
PostgreSQL. `compose.postgres.yaml` là cấu hình riêng; dùng project khác khi chạy.

```mermaid
flowchart LR
    B["Browser localhost"] --> R["ch-web: fixed target relay<br/>127.0.0.1:18581 mặc định"]
    subgraph INTERNAL["Docker backend internal"]
      UI["ch-app: Streamlit<br/>1 CPU / 1536 MiB"]
      DB["clickhouse<br/>1 CPU / 2 GiB"]
      JOB["ch-job: native batch tools<br/>1 CPU / 1536 MiB"]
      MQ["ch-broker: Mosquitto<br/>128 MiB"]
      STREAM["ch-stream: Spark + native sink<br/>1 CPU / 1536 MiB"]
      UI --> DB
      JOB --> DB
      MQ --> STREAM --> DB
    end
    R --> UI
    RAW["Read-only raw bind"] --> UI
    RAW --> JOB
    STATE["Shared state / coordinator bind"] --> UI
    STATE --> JOB
    STATE --> STREAM
    VOL["ch_data / ch_logs / ch_broker volumes"] --- DB
    PG["Optional PG project / volume"] -. "explicit rollback configuration" .-> UI
```

Chỉ relay join frontend bridge; app/DB/job/stream không có runtime Internet.
Snapshot preparation có writable raw mount riêng. `.env` chọn paths, project,
port và credential local; không commit. Không đổi project/volume như cách
chuyển backend vì có thể nhìn vào một warehouse rỗng.

Images và apt/pip/JDBC/browser dependencies được chuẩn bị trước runtime offline.
Processing image chung giữ PostgreSQL compatibility; native sink dùng HTTP
stdlib, không gọi PostgreSQL/JDBC. CH server/config và resource caps được pin
trong Compose; benchmark ghi caps thực tế riêng.

Sau cutover, app/job/stream cần cùng coordinator và
`GREENHOUSE_CH_REQUIRE_CUTOVER_JOURNAL=1`. Host filesystem phải hỗ trợ flock;
không dùng cơ chế này như distributed lock. Không prune data volumes khi thiếu
storage; kiểm cache và headroom trước các read/rebuild lớn.
