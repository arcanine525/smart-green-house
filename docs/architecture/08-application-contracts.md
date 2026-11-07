# 08 — Application và contracts

Streamlit là thin UI trên shared typed services. Mỗi action tạo một connection,
pin publication/context và dùng cùng DTO cho chart, table, drilldown và export.
Không có phép tính KPI riêng trong HTML/Plotly.

```mermaid
flowchart LR
    USER["Filters / explicit command"] --> UI["Streamlit pages"]
    UI --> RT["Backend runtime / namespace identity"]
    RT --> SVC["Catalog, water, harvest, climate, drain, energy, intensity, cost services"]
    SVC --> DTO["Contracts: publication, context, Decimal, quality, basis"]
    SVC --> CH["ClickHouse adapters + stdlib HTTP client"]
    SVC -. "explicit postgres selection" .-> PG["PostgreSQL adapters"]
    DTO --> PLOT["Plotly / tables"]
    DTO --> EXPORT["CSV / typed JSON"]
```

```mermaid
sequenceDiagram
    participant U as User
    participant A as UI / CostService
    participant R as Native repository
    participant J as Coordinator / journal
    participant D as ClickHouse
    U->>A: Scenario command, request ID, frozen context
    A->>R: Validate publication, baseline, changes
    R->>J: Acquire shared lock and reconcile pending query
    J->>J: Persist intent before SQL
    R->>D: Append complete scenario + rates envelope
    D-->>J: Synchronous success or terminal evidence
    J->>J: Durable outcome
    R-->>A: Same immutable result for identical retry
    A-->>U: Comparison DTO and export with context/versions
```

Default backend là ClickHouse; error không fallback PG. Namespace gồm backend,
host, port, database và user, không có password. Đổi namespace xóa widget,
historical context và write intent cũ. Old publication vẫn có semantic API pin;
catalog UI chọn complete generation hiện hành của scope.

Replay refresh ghim một publication cho toàn bộ render. Historical và replay
không chia sẻ scope; rule lịch sử chỉ map khi snapshot/compartment hợp lệ.
App không tự migrate/publish/seed fixture khi thiếu dữ liệu. Missing data,
connection error và unverified raw mount có trạng thái riêng.
