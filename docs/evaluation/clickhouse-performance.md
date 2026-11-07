# PostgreSQL và ClickHouse trên cùng workload local

## Kết quả

Trong lượt đo cuối, cả sáu workload ClickHouse đạt mục tiêu **warm summary p95
≤2.000 ms**, với năm warm-up và 30 mẫu mỗi BR; mọi mẫu khớp fingerprint nghiệp
vụ PostgreSQL. PostgreSQL nhanh hơn trong các workload đã đo. ClickHouse có
footprint bảng thấp hơn, đồng thời chịu chi phí mở process, xác minh publication
và ghi scenario lớn hơn. Đây là kết quả của hai implementation Hybrid thực chạy,
không phải khẳng định chung cho mọi ứng dụng của hai DB.

[Manifest và checksum](assets/clickhouse-performance/manifest.json) liên kết raw
samples, image/code hashes, cấu hình, suite tests, profiler và các lượt thất bại.
T49 đạt trong phạm vi benchmark này; offline/restore, video và cutover là các
mốc riêng T50/T51/T39, chưa được suy ra từ kết quả performance.

## Điều kiện và tính đúng

- Snapshot AGC SHA-256 `c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68`;
  toàn snapshot được dựng vào DB mới. Không nhân bản dữ liệu synthetic.
- Latency dùng Automatoes toàn vụ 2019-12-16 đến trước 2020-05-30: 166 ngày,
  47.808 climate slots. Không suy latency của một compartment thành cả sáu.
- Rule native giống nhau: `climate-demo-b01:Automatoes:v1` và
  `drain-demo-b03:Automatoes:v1`; climate basis requested. Tariff, Decimal,
  NULL/quality và giả định diện tích 62,5 m² **assumed_unverified** giữ nguyên.
- Full correctness trên image cuối: sáu BR, 96 CSV, 30 lineage views, 18 nhóm
  threshold và 67.829 episode kể cả biên vụ khớp PG/raw. Oracle rules tạm chỉ
  dùng trong bộ nhớ; registry đo latency giữ cùng 14 rule native.
- 174 scenario nguồn được seed/map vào mỗi fixture, giữ request, author/time,
  rates và context; chỉ ánh xạ publication/context ID. Native CH import đi qua
  parity gate thật và xác minh archive; không giả lập proof để qua gate.
- PG gốc chỉ được đọc. Benchmark chạy DB/project riêng, hai engine lần lượt.
  Các fixture riêng được dọn sau khi lưu bằng chứng; không prune volume người dùng.

## Môi trường và phương pháp

Apple M4, 10 logical CPU, RAM 16 GiB. Docker 29.9.2: 4 CPU, khoảng 7,62 GiB RAM.
PostgreSQL 17.10 và ClickHouse 25.8.33.6 có cùng **1 CPU/2 GiB**; processing cùng
image dependencies, **1,5 CPU/2 GiB**, Spark driver 768 MiB. Python 3.12.12,
Spark 4.0.1, psycopg 3.3.7. Inspect/cgroup thực chạy đi kèm evidence.

PostgreSQL: shared buffers 512 MiB, effective cache 1.536 MiB, work memory 4 MiB,
parallel gather 0. ClickHouse dùng local.xml đã khóa; streaming reader một
thread/block 1.024, external sort/group 64 MiB. Table/index/sort/partition DDL và
background merge tại thời điểm inventory có trong `pg-storage.json`/`ch-storage.json`.

Mỗi action mở connection mới; timer gồm connect, query/service và close.
Fingerprint/ghi artifact ở ngoài timer. PG enforced REPEATABLE READ, READ ONLY.
Climate/drain pure evaluation cache **tắt**. CH bật process proof cache giống
production UI; source parts/schema/pending mutations và toàn registry/pin cấu
hình vẫn được kiểm, không dùng lại evaluation result. P50 là median, p95 nearest
rank; giữ đủ cả 30 mẫu. OS/database cache không được flush.

Lần mở process/proof đầu đo riêng. DB mới không đồng nghĩa cold disk. Các mẫu
warm là lặp trong một phiên, không phải 30 deployment độc lập hoặc confidence
interval. Chưa đo concurrent users hay dataset lớn hơn AGC.

## Sáu workload summary

| Workload | PG p50 (ms) | PG p95 (ms) | CH p50 (ms) | CH p95 (ms) |
|---|---:|---:|---:|---:|
| BR-01 climate evaluation | 1451.99 | 1497.88 | 1667.42 | 1786.48 |
| BR-02 irrigation summary | 9.37 | 11.53 | 93.14 | 108.25 |
| BR-03 drain evaluation | 993.82 | 1052.17 | 1193.74 | 1362.62 |
| BR-04 energy intensity | 18.81 | 20.06 | 108.26 | 118.41 |
| BR-05 harvest summary | 4.94 | 5.18 | 86.93 | 99.19 |
| BR-06 baseline cost | 14.32 | 16.86 | 112.08 | 134.12 |

Cold process/proof + catalog: PG **0.167s**;
CH **141.242s**. Mốc ≤2s áp dụng
warm summary; không tuyên bố app startup/cold proof cũng đạt mốc đó.

Lượt CH 30 mẫu trước tối ưu có BR-01 p95 2.283,00 ms và BR-04 2.337,87 ms;
`ch-queries-before-optimization.json` giữ nguyên các mẫu đó. Query log ghi một
query đọc sáu hàng mất 1.469 ms, trong đó CPU wait khoảng 1.240 ms, I/O nhỏ.
Profiler ghi GC khoảng 250–400 ms/call BR-01 và khoảng 34,42 MB slot response.
Control một thread không cho cải thiện ổn định nên không đổi cấu hình production.

Transport cuối deduplicate **full physical tuple**, rồi gửi các trường DTO cần;
projection-only conflict vẫn tạo duplicate code và bị từ chối. Probe cùng 47.808
slot giữ nguyên hash value/quality/identity, giảm 34.419.960 xuống 29.118.839 byte.
Probe n=1 mất 0,690 → 0,596s; đây là diagnostic, không thay p95 30 mẫu.
Full AGC và 84 tests (75 chạy đạt, 9 heavy skip tường minh) xác minh lại sau đổi.

## Background merge và trạng thái đo cuối

Một lượt compact-code bị guard `WarehouseChanged` từ chối sau cả hai proof,
chưa tạo warm sample. Part log xác nhận source_measure merge trong proof thứ hai;
đây là thay đổi vật lý bình thường nhưng làm proof cũ không còn dùng được.
Không tắt guard, tăng retry vô hạn hay sửa expected output.

Fixture cuối chạy OPTIMIZE FINAL không DEDUPLICATE trên năm bảng còn nhiều part,
giữ nguyên mọi physical row. Chi phí maintenance gồm 5.143s
SQL compaction và 20s probe ổn định (tổng 25.218s).
Ba fingerprint sau đó giống nhau. Phép đo cuối ở trạng thái **post-compaction**;
read cache guard còn bật. Ghi riêng bước này ngoài pipeline; không quy toàn bộ
thay đổi p95 cho riêng giảm payload. Sau ingest/correction, merge có thể làm
proof phải xác minh lại; warm target không là bảo đảm latency lúc đang nạp dữ liệu.

## Lifecycle

| Đường thực thi, một lượt mỗi engine | PostgreSQL (s) | ClickHouse (s) |
|---|---:|---:|
| Fresh pipeline | 360.290 | 561.298 |
| Native resume cùng workflow hoàn tất | 4.449 | 170.453 |

PG fresh: ingest 16,797s, core 228,354s, Spark 29,384s, publish 82,341s;
query-time metadata seed sau timer chỉ khoảng 0,028s. CH fresh: ingest/core
201,968s, freeze membership 69,916s, water 2,778s, harvest 2,698s, climate 34,787s,
drain 13,285s, energy 3,145s. Publication total 285,492s **bao gồm** verification
163,794s và các builder: không cộng các timer lồng nhau thành tổng mới.

Hai engine giữ Hybrid/công thức nhưng PG dùng Spark candidate stage, CH dùng
Python historical mart builders; Structured Streaming sensor vẫn dùng Spark.
Lifecycle so implementation, không tách riêng tốc độ DB. PG resume kiểm artifact
rồi skip completed steps; workflow_step trong report là metadata lần chạy đầu.
CH resume kiểm DB/marts thực tế. Hai native resume không là cùng lượng công việc.
Cả hai giữ đúng publication đã publish; CH giữ load và receipts.

Lifecycle được đo trên build đầu. Build sau chỉ thay `clickhouse/threshold.py`
ở đường đọc; byte hashes của PG/pure engine/pipeline không đổi. Final query,
scenario, sensor và resume dùng image mới. Hai package inventories/image IDs
được lưu để phân biệt, không chỉ dùng version package 0.1.0 làm định danh code.

## Scenario và sensor

Scenario: 5 warm-up + 30 mẫu tạo version, đọc lại và retry. Mỗi operation mở
connection mới. 174 → 209 versions/engine, exact DTO readback; retry không tạo
thêm version. Timer dưới đây không gồm seed/cold historical proof ban đầu.

| Scenario operation | PG p50 (ms) | PG p95 (ms) | CH p50 (ms) | CH p95 (ms) |
|---|---:|---:|---:|---:|
| write_ms | 6.12 | 6.43 | 4896.68 | 5634.63 |
| read_ms | 3.89 | 4.10 | 105.33 | 140.05 |
| retry_ms | 4.29 | 4.47 | 5007.01 | 6166.69 |

CH cấp version hiện duyệt/validate toàn bộ lịch sử scenario và thực hiện nhiều
HTTP/config reads; chi phí này nằm trong số đo. Đây là điểm cần tối ưu ở adapter
nếu tăng lịch sử hoặc cần ghi tương tác nhanh. Không suy từ số này thành write
microbenchmark thuần engine, và không cam kết scenario write ≤2s.

Sensor là **một burst 30 event AGC thật mỗi engine**, nguồn file checksum giữ
nguyên; không phải full file, thiết bị vật lý hay 30 trial độc lập. Publish →
queryable tính từ publisher call đến lúc đọc sealed publication đã xác minh,
bao gồm poll 100 ms; đối chiếu bytes outbox và slot/value/quality fingerprint PG/CH.

| Sensor | PostgreSQL | ClickHouse |
|---|---:|---:|
| p50 (ms) | 1226.72 | 2562.35 |
| p95 (ms) | 1409.62 | 2794.94 |
| Setup/Spark/proof ngoài event timer (s) | 3.78 | 175.99 |

Cả hai replay complete/checksum, đủ 30 event, cùng business fingerprint.
Raw-oracle sensor correctness rộng hơn nằm trong [T38](clickhouse-parity.md).
Không suy số đo burst thành SLA streaming kéo dài hoặc nhiều thiết bị.

## Storage và tài nguyên

PG database (bảng + index + TOAST/metadata): **3.188 GiB**.
CH active MergeTree parts của DB benchmark: **0.384 GiB**;
inactive parts lúc đo 1.361 MiB.
Tỷ lệ CH active/PG database **12.06%**.
Các metric có ranh giới khác nhau đã ghi rõ; đây không phải tổng dung lượng máy.

PG successful fresh workflow + ingestion artifacts
386.55 MiB;
CH output 87.19 MiB. Bỏ output pilot lỗi khỏi
phép tính. Raw/ZIP dùng chung, anonymous verification spools, WAL và engine logs
nằm ngoài metric active-table/database. Riêng CH text/server log volume khi thu
cuối khoảng 397.84 MiB. File `*-server-cgroup-and-volume.txt`
ghi whole data volume (gồm system data/WAL tương ứng), không trộn với active DB.

Peak processing cgroup của fresh job: PG 1323.09 MiB,
CH 1755.07 MiB. Cgroup memory gồm
page cache, không gọi là riêng RSS. Fresh resource traces và server cumulative
cgroup counters giữ trong evidence; lifetime của hai server khác nhau nên không
so tổng CPU counters như CPU efficiency. Bộ đếm đã thu không ghi OOM-kill.

## Pilot lỗi, giới hạn và tái lập

Hai pilot PG chưa thành công được giữ riêng. Pilot thứ hai dừng sau 444,245s;
Docker đồng thời báo overlay2/BuildKit filesystem chỉ đọc và host còn 236 MiB.
Không kết luận OOM từ memory.max hits. Sau khi dọn 1,845 GB build cache, restart
Docker, reclaim block và dọn bản sao test đã backup/verify, fresh mới hoàn tất.
Không prune volume dữ liệu gốc. Lượt sensor PG đầu lỗi serializer ở harness sau
reconcile; fresh replay mới sau sửa fingerprint được đo, không thay expected.
Bước `du` cuối có race với system part removal; recollect inventory read-only
thành công, không chạy lại hay sửa measurement. Raw failures/phases được giữ.

Để tái lập, chuẩn bị locked raw/ZIP, **frozen read-only metadata export** có byte
SHA `767e73b5cf08e39849b51484f1406091d8fcfe3afa6c701a4ea9ff8ce36e60b3`
và bốn PG reference projections theo [T38](clickhouse-parity.md). Export/data
được giữ local ngoài Git. Export khác tạo workload khác; không hứa số đo giống
nhau. `compose.benchmark.yaml` có network internal, không expose DB ports.

```bash
# Chuẩn bị image từ code/locks; chọn project/state MỚI, không trùng deployment.
docker compose -f compose.clickhouse.yaml --profile tools --profile app build ch-job ch-app ch-client
export GREENHOUSE_BENCH_STATE_PATH=/absolute/path/to/new-benchmark-state
# Đặt source-metadata.json và references/ tại state; tạo pg/output,ch/output,ch/coordinator.
docker compose -f compose.benchmark.yaml -p gh-engine-benchmark-review --profile postgres up -d --wait postgres
# Chạy qua pg-job: benchmark_lifecycle.py --backend postgres --mode fresh ...
# Lấy publication_id từ kết quả; seed_scenarios.py; benchmark_queries.py;
# benchmark_scenarios.py; benchmark_inventory.py; benchmark_sensor.py; resume.
# Dừng PG trước CH, giữ cùng caps. Không sửa script/image giữa lượt đo.
docker compose -f compose.benchmark.yaml -p gh-engine-benchmark-review --profile postgres stop postgres
docker compose -f compose.benchmark.yaml -p gh-engine-benchmark-review --profile clickhouse up -d --wait clickhouse
# Qua ch-job: fresh; check_parity.py --transient-rules; import_scenarios_benchmark.py;
# settle_benchmark_parts.py; benchmark_queries.py --reference /bench/pg/queries.json;
# scenario, inventory trước sensor; sensor --reference /bench/pg/sensor.json; resume.
```

[Runner đầy đủ](../../scripts/run_engine_benchmark.sh) thực hiện đúng chuỗi
trên, lấy publication ID từ output thay vì hardcode. Sau khi chuẩn bị image,
chạy `bash scripts/run_engine_benchmark.sh /abs/source-metadata.json /abs/references`.
Runner cần 12 GiB host space vì giữ cả hai volume riêng, lưu logs/raw JSON và
không tự prune; các helper commands đã thực chạy trong lượt báo cáo, runner
được kiểm syntax và không được tính như một trial mới.

Mỗi helper có `--help` với đường dẫn/ID bắt buộc. Processing mount scripts tại
`/migration`, state `/bench`, raw/import read-only. Sensor cần broker profile;
metadata/reference phải cùng snapshot/pub nguồn. Theo dõi **host df** và guest
space trước mỗi job; metadata fixture writers chỉ chấp nhận DB/user/endpoint
benchmark riêng. Diagnostic scripts/profiler không phải acceptance benchmark.

Đây là đánh giá small-data local, một host, một build/resume/burst mỗi engine.
Chưa đo scale-out/replication, concurrency, cold disk hoặc hiệu quả nông học.
[Hybrid/Kimball trên PostgreSQL](performance.md) là thí nghiệm khác caps/phiên bản;
không gộp các kết quả đó vào tỷ lệ so engine ở đây.
