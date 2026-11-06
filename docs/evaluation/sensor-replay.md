# Nghiệm thu sensor replay

Ngày đo: **06/11/2026 UTC**. Các kiểm thử chạy local trên Docker, dùng broker và
PostgreSQL cô lập cho bài fault và đo hiệu năng. Không có sensor vật lý hay
điều khiển thiết bị. Các kết quả dưới đây là bằng chứng cho phạm vi đã chạy,
không phải SLA cho mọi kích thước dữ liệu hay mọi cấu hình.

## Đối soát batch–streaming

- **Fixture synthetic, 30 slot:** cùng file, snapshot, quality policy và rule;
  pipeline batch thực tế được so với simulator → MQTT → collector → Spark →
  PostgreSQL. Giá trị từng slot, verdict quality, metric/joint duration và
  episode BR-01/03 đều khớp. Batch test dùng stage artifact fixture, không đo
  thời gian Spark batch. [Trace và số đo từng event](assets/sensor-replay/live.json).
- **AGC thật, cửa sổ Automatoes 00:00–02:30 ngày 16/12/2019:** 30 slot từ
  snapshot AGC khóa; replay đi qua outbox → collector spool → Spark, sau đó so
  với publication lịch sử T25 qua kết nối PostgreSQL **read-only**. Lineage source record/dòng/raw hash, giá trị,
  quality từng measure, metric/joint duration và episode BR-01/03 khớp. Rule
  demo và quality version được pin trong [AGC parity và lineage từng dòng](assets/sensor-replay/agc-window.json).
  Phép này không đi qua MQTT; đường MQTT thực được kiểm tra trên fixture trên.
  Đây là một cửa sổ có giới hạn, không phải chứng minh parity toàn bộ vụ AGC.

## Fault matrix

Các ca dưới đây dùng 6 event synthetic, 100% xác suất cho fault tương ứng;
số delivery gồm telemetry **và** control. Oracle tính trên dữ liệu sau fault:
đối với fixture đầy đủ, 30 phút hợp lệ, nhiệt độ ngoài dải 20 phút và EC ngoài
 0 phút. Ca thiếu một event đầu còn 25 phút hợp lệ; conflict/drop không được
coi là giá trị 0 hợp lệ. File JSON giữ ID, payload hash, audit, state và raw
bytes synthetic cần đối chiếu.

| Tình huống | Delivery | Unique dùng KPI | Missing | Quarantine | Kết quả |
|---|---:|---:|---:|---:|---|
| [Duplicate](assets/sensor-replay/fault-duplicate.json) | 17 | 6 | 0 | 0 | Khử trùng, checksum và KPI khớp |
| [Delay / đảo thứ tự](assets/sensor-replay/fault-delay.json) | 11 | 6 | 0 | 0 | Sequence thực sự đảo; kết quả cuối khớp |
| [Conflict cùng ID](assets/sensor-replay/fault-conflict.json) | 17 | 0 | 6 | 0 | Giữ 6 conflict, loại khỏi KPI, provisional |
| [Drop có chủ đích](assets/sensor-replay/fault-drop.json) | 5 | 0 | 6 | 0 | Audit chỉ rõ 6 sequence bị bỏ, provisional |
| [Malformed bytes](assets/sensor-replay/fault-malformed.json) | 11 | 5 | 1 | 1 | Giữ nguyên byte lỗi, KPI theo 5 event |
| [Sai schema/unit](assets/sensor-replay/fault-invalid-schema.json) | 11 | 5 | 1 | 1 | Quarantine và giữ byte lỗi, KPI theo 5 event |

[Late event sau publication](assets/sensor-replay/fault-late.json): publication
tạm có gap sequence 2 và hai episode; khi event đến qua MQTT/Spark, publication
mới có một episode 15 phút. Publication cũ giữ nguyên. Không có double-count.
Các ca lỗi chưa được giải quyết không tự đổi thành complete chỉ vì manifest đến.

## Restart và phục hồi

- [Spark process](assets/sensor-replay/spark-process-restart.json): hai tiến
  trình Python/Spark độc lập dùng lại checkpoint; hai batch được ghi một lần.
- [PostgreSQL restart](assets/sensor-replay/db-recovery.json): dừng DB riêng sau
  batch đầu, quan sát batch sau thất bại, khởi động DB và chạy lại từ checkpoint;
  2 batch, 3 delivery, 3 variants, không nhân bản. Kiểm thử này kiểm tra landing;
  rollback chung landing/publication được kiểm tra trong test transaction riêng.
- [Broker/collector/simulator](assets/sensor-replay/broker-recovery.json): simulator
  pause/resume; dừng collector, publish vào persistent session, restart broker
  riêng, xác minh **9 outbox rows** đã queue đến collector trước publisher retry;
  sau retry có 6 event unique, checksum khớp. 21 delivery gồm bản redelivery.
- [MQTT fault tests](assets/sensor-replay/mqtt-faults.log): 8 test broker thật
  về disconnect trước/sau persist, redelivery, receipt và quarantine đạt.

Các container được harness đặt tên và label riêng, có caps riêng, rồi xóa cùng
anonymous volumes do chính harness tạo. Broker, DB và volumes của project đang
chạy không bị restart hoặc xóa.

## Độ trễ và tài nguyên

Lượt synthetic không lỗi có 30 event, 1 replay, cursor mỗi 5 event, collector
batch 5, timeout 0,2 giây, Spark tối đa 20 file/batch. Đo bằng monotonic từ
ngay trước lời gọi MQTT publish đến lần polling đầu tiên thấy event trong
current publication. Polling mỗi khoảng 0,1 giây tạo thêm sai số quan sát;
không dùng timestamp AGC lịch sử để tính độ trễ. Đây là 30 event trong một lượt,
không phải 30 lần chạy độc lập hay phép đo tải nhiều client.

| Chỉ số | Kết quả |
|---|---:|
| p50 publish → queryable | 1.212,57 ms |
| p95 nearest-rank | 1.491,60 ms |
| Throughput, từ publish đầu đến event cuối quan sát được | 20,04 event/s |
| Delivery đã spool / đã batch / còn unbatched | 38 / 38 / 0 |
| Backlog unbatched lớn nhất **quan sát được** | 11 |
| Processing CPU time / wall time của pha stream | 6,17 s / 6,36 s |
| Processing cgroup memory peak | 421,16 MiB / 1.536 MiB |
| Broker CPU / memory lớn nhất trong 19 mẫu | 0,20% / 2,44 MiB |
| PostgreSQL CPU / memory lớn nhất trong 19 mẫu | 2,21% / 62,23 MiB |

Collector, publisher, Spark và polling chạy chung processing cgroup 1 CPU;
không tách được CPU/RAM từng thành phần trong phép này. Memory peak còn bao gồm
fixture batch trước pha stream. CPU% của broker/DB là mẫu Docker, không bảo đảm
là peak thực; phần chuẩn bị và cleanup nằm trong khoảng lấy mẫu. Host Apple M4,
10 logical CPU, 16 GiB; Docker Desktop 29.9.2 được cấp 4 CPU và khoảng 7,62 GiB.
PostgreSQL 17.10 giới hạn 0,5 CPU/512 MiB; Mosquitto 2.0.22 giới hạn 0,1 CPU/
128 MiB. [Cấu hình, phiên bản, hash test và thống kê mẫu](assets/sensor-replay/environment.json).

## Tái lập

Sau khi chuẩn bị `.env`, build processing image và có Docker network backend của
project (xem [môi trường local](../implementation/local-environment.md)):

```bash
bash scripts/test_sensor_acceptance.sh
```

Lệnh tạo broker/DB test riêng, chạy 19 test gồm parity/timing, fault matrix,
redelivery và restart, rồi lưu evidence trong `tmp/sensor-recovery/<run-id>`.
Nó không yêu cầu cloud trong lúc chạy nếu các image/dependency đã được chuẩn bị.
Có thể đặt `SENSOR_TEST_NETWORK` nếu Compose backend network có tên khác.

Để lặp lại riêng đối soát cửa sổ AGC thật, cần snapshot đã mount và một DB
historical có publication cùng snapshot, đủ climate/drain marts cùng hai demo
rule Automatoes. Ví dụ với DB benchmark T25 đã chuẩn bị:

```bash
mkdir -p tmp/sensor-parity
docker compose run --rm --no-deps \
  -v "$PWD/src:/workspace/src:ro" -v "$PWD/tests:/workspace/tests:ro" \
  -v "$PWD/tmp/sensor-parity:/evidence" \
  -e PYTHONPATH=/workspace/src:/workspace/tests \
  -e GREENHOUSE_REFERENCE_DATABASE=greenhouse_hybrid_bench_t25_20261106_104814 \
  -e GREENHOUSE_REFERENCE_PUBLICATION=df3e4bcbacab45a5b86fc37d2f53c00a \
  -e GREENHOUSE_REAL_PARITY_EVIDENCE=/evidence/agc-window.json \
  job python -m pytest tests/integration/test_sensor_live_replay.py::test_real_agc_replay_window_matches_historical_publication -q
```

Reference DB được mở read-only; tên DB/publication là ví dụ của lượt đã đo,
không được tạo giả nếu không tồn tại. Kiểm thử tạo và dọn database tạm riêng.
