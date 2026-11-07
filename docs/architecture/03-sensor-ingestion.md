# 03 — Sensor simulation, ingestion và replay

Simulator phát lại Climate AGC thật, giữ source provenance và sequence. MQTT
QoS 1 có thể giao trùng; correctness dựa vào durable state, canonical identity
và sealed receipts, không dựa vào giả định transport exactly-once.

```mermaid
sequenceDiagram
    participant S as Simulator
    participant O as SQLite outbox
    participant M as Mosquitto QoS 1
    participant C as Collector + SQLite spool
    participant F as Atomic ready files
    participant K as Spark checkpoint
    participant W as Native sink + coordinator
    participant D as ClickHouse
    S->>O: Commit original envelope + sequence
    O->>M: Publish pending message
    M->>C: Deliver, có thể duplicate
    C->>C: Durable spool commit trước ACK
    C-->>M: ACK
    C->>F: Flush JSONL + atomic manifest
    F->>K: Spark file source / foreachBatch
    K->>W: Stream ID, generation, batch ID, records
    W->>W: Lock + reconcile + durable rollback intent
    W->>D: Deliveries → verified batch receipt
    W->>D: Reconcile cursor/conflicts → replay seal/pointer
    D-->>W: Synchronous terminal success
    W->>W: Durable outcome trước callback return
    W-->>K: Batch callback complete
    K->>K: Commit checkpoint
```

Event ID + payload hash phân biệt duplicate và conflict; conflict không chọn
first arrival. Partial/late/control mismatch làm replay provisional. Source
clock 2019–2020 khác received UTC; completed replay vẫn có thể có collector
stale sau khi finite producer dừng.

Checkpoint bind backend, namespace, scope, stream, generation và config hash.
Không tái dùng checkpoint PostgreSQL cho CH hoặc khi rebuild vào DB mới.
Backup SQLite cần WAL/SHM nhất quán hoặc SQLite backup API; ready/checkpoint,
broker, config và DB phải thuộc cùng cutoff. Xem
[sensor native](../implementation/clickhouse-sensor.md) và
[proof replay](../evaluation/sensor-replay.md).
