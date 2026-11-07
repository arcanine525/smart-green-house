# Nghiệm thu ứng viên ClickHouse

Ngày 2026-11-07. **Đạt trong phạm vi T51: sáu BR và sensor**; chưa chốt chuyển backend
mặc định. Kết quả dưới đây tổng hợp các gate độc lập đã có, không phải một lượt
chạy lại toàn bộ test. Cutover/rollback T39 và gói bàn giao có điều kiện riêng.

## Dữ liệu và hợp đồng

Snapshot AGC 2nd edition SHA-256
`c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68`.
Full historical rebuild có 287.989 revision và 14.062.934 measure; một record
quarantine được giữ nguyên. Phần Lab/deferred không tự biến thành fact. Source
clock là naive chưa xác minh timezone. Diện tích 62,5 m² và các rule demo giữ
nhãn assumed/unverified; scenario chi phí không phải hóa đơn hoặc dự báo.

Các [AC của sáu BR](../br/) giữ nguyên khi đổi engine. [Hợp đồng migration](../implementation/clickhouse-migration-contract.md)
quy định Decimal, NULL/0, revision/conflict, quality, duration, source clock,
publication và scenario; không chọn lại tolerance sau khi nhìn kết quả.

## Gate theo engine và phạm vi

| Gate | Kết quả và bằng chứng ClickHouse |
|---|---|
| Runtime/ingestion | [Runtime](assets/clickhouse-runtime/manifest.json), [ingestion](assets/clickhouse-ingestion/manifest.json): image pin, private runtime, Decimal/source clock, revision/retry/conflict, quarantine và config round-trip. |
| Publication/writer | [Fault evidence](assets/clickhouse-publication/manifest.json): crash tại state, mất ACK/SIGKILL, pending-query fence, hai publisher và đọc pin cũ. Không chờ merge để quyết định correctness. |
| BR-02 / BR-05 | [Water](assets/clickhouse-water/manifest.json), [harvest](assets/clickhouse-harvest/manifest.json): summary/coverage/rule, Reference incomplete/quarantine, lineage, native marts, UI và CSV. |
| BR-01 / BR-03 | [Thresholds](assets/clickhouse-thresholds/manifest.json): requested/realized, gap/rule boundary, OR jointly valid, full-cycle episode, profile và typed export. |
| BR-04 / BR-06 | [Energy/cost](assets/clickhouse-energy-cost/manifest.json): quantities chung, temporal tariff/basis, NULL khi mẫu số 0, scenario bất biến và export có version. |
| Sensor BR-01/03 | [Sensor faults/recovery](assets/clickhouse-sensor/manifest.json): actual MQTT/collector/Spark, duplicate/drop/late/conflict/malformed, complete/checksum, replay pins, crash recovery và checkpoint identity. Scope selection 30 event AGC; không suy thành replay toàn vụ. |
| Full migration | [Full AGC parity](clickhouse-parity.md): 6/6 BR, 96 CSV fingerprint, 30 raw lineage, 18 nhóm threshold/67.829 episode; 174 scenario nguồn qua parity gate. Source PostgreSQL chỉ đọc. |
| Performance | [Benchmark paired](clickhouse-performance.md): 5 warm-up + 30 mẫu/BR, p95 warm đều ≤2 giây; giữ cold proof, scenario writes, sensor burst và lần chưa đạt riêng. |
| Offline/recovery | [Offline/restore/raw rebuild](clickhouse-local-repro.md): physical 28 bảng/17.513.148 hàng khớp, SQLite WAL/SHM/MQTT queue/checkpoint; 302 config/175 scenario exact hoặc ánh xạ tường minh; raw rebuild rồi parity/UI/replay đạt. |

Suite T50 cuối có 85 test, 76 chạy đạt và chín heavy integration skip tường
minh; các diễn tập heavy có evidence riêng. Các suite theo task không cộng
thành số test duy nhất: có ca chạy lại và phạm vi khác nhau. Baseline
[PostgreSQL](correctness.md), [benchmark kiến trúc](performance.md) và
[video T26](assets/playwright-t26/README.md) được giữ đúng nhãn engine.

## Walkthrough mới

Đã quay sáu video BR và video sensor bổ sung trên bản sao T50 raw-rebuild,
publication `4b08bd51c092465783304f887d766d47`, load
`b46b4d1fa95641b891c447faed0c07dc`. Chuẩn bị kiểm đúng native engine và không
có PostgreSQL credentials; chỉ relay localhost có frontend bridge. Các bước
chuẩn bị/cold proof chạy tuần tự trước recording, không dùng làm số đo warm.

Các đáp án nước, năng lượng, thu hoạch và chi phí được tính lại bằng Decimal
từ raw CSV đã kiểm SHA. Khí hậu/drain dùng oracle đã khóa và full parity ở trên.
Video phải giữ nguyên thao tác thật: bộ lọc, basis, chart/tab, lineage,
CSV/JSON, lưu và mở lại scenario. Sensor bổ sung phải quan sát provisional
10 event → complete 30 qua MQTT/Spark thật trên cùng checkpoint.

Bảy video đạt 109 bước/21 file xuất, dài tổng 1.642,56 giây (~27 phút 23 giây).
Đã kiểm SHA, giải mã toàn bộ, bảy player/chapter và main review bảy frame MP4.
[Metadata và cách mở local](assets/clickhouse-walkthrough/README.md),
[oracle JSON đầy đủ chữ số](assets/clickhouse-walkthrough/native-export-oracle.json)
và [manifest](assets/clickhouse-walkthrough/manifest.json) lưu version/checksum.
Video không âm thanh, không có người tham gia; không đo usability người thật.

## Lỗi cold proof phát hiện khi nghiệm thu

Lượt khởi động mới ban đầu thất bại. Query log của ClickHouse 25.8.33.6 ghi
`MEMORY_LIMIT_EXCEEDED` ở phép sort toàn bộ threshold measure: giới hạn server
1,5 GiB trong container 2 GiB. HTTP đã trả 200 và một phần dữ liệu; server ghi
exception tiếp vào stream. Client cũ đọc frame exception như một dòng dữ liệu,
dẫn tới thông báo sai lệch mart gây hiểu nhầm. Không có bằng chứng dữ liệu gốc
bị thay đổi; không bỏ kiểm chứng publication hoặc tăng giới hạn RAM để vượt gate.

Reader mới đọc lần lượt các cycle theo thứ tự compartment, tận dụng prefix của
sorting key `(scope, publication, bundle, cycle, slot, measure)`. Vẫn kiểm mọi
cycle, so từng dòng với nguồn chuẩn, phát hiện compartment sai và giữ nguyên
checksum/order của publication đã seal. Client yêu cầu plaintext error framing,
chuyển trailer lỗi hoặc JSON lỗi sang `ClickHouseError`; không công nhận prefix
là kết quả đầy đủ. Xem [giao diện HTTP chính thức](https://github.com/ClickHouse/clickhouse-docs/blob/main/docs/integrations/interfaces/http.md)
về lỗi sau khi response header đã gửi.

Regression chạy trên server thật: HTTP 200 rồi late throw phải thất bại;
393.216 measure ở sáu cycle với query budget 64 MiB làm reader cũ thất bại,
reader mới đạt cùng receipt; dữ liệu cycle lạ hoặc compartment sai vẫn bị từ chối.
Bản UI đóng gói mới qua một lượt restart và browser mới: proof hoàn tất trong
189,908 giây. Đây là đo cold process/proof của lượt đó, không phải cold disk,
không thay số đo benchmark T49. Kết quả sau sửa runtime được kiểm riêng bên dưới; bộ video cuối vẫn cần
kiểm player, giải mã và review frame trước khi đóng gate.

Lượt lặp UI phát hiện thêm một nguồn áp lực RAM độc lập: merge nền của bảng
telemetry `system.metric_log` dùng **889.667.257 byte (~848 MiB)**, đưa tracked
memory toàn server lên 1.493.622.089 byte. Truy vấn UI bị chọn hủy chỉ dùng
8–17 MiB. Sampler `system.merges`/`system.metrics` và query log khóa nguyên
nhân này; giảm block metadata từ mặc định xuống 1.024 hoặc 64 vẫn có lỗi nên
thay đổi đó đã được loại bỏ. Dừng riêng merge `metric_log` cho **20/20 lượt UI
đạt** trên cùng image, 136,150 giây, giữ nguyên assertion.

Cấu hình local mới tắt collector metric log tùy chọn; metrics vẫn đọc theo nhu
cầu và **query_log được giữ nguyên** để recovery xác minh terminal query ID.
Với server diễn tập cũ, helper giữ nguyên **28 part/13.458 hàng và checksum**,
chỉ đặt automatic merge của bảng log này về 0; recreate container dùng cùng
volume và cùng memory limit. XML preprocessed đã được kiểm sau restart. Không
đổi merge settings bảng nghiệp vụ hoặc xóa log cũ. Hướng dẫn cho bản rehearsal
cũ nằm trong [runbook](../../scripts/clickhouse_repro/README.md#metric-log-trong-runtime-2-gib).
Sau restart, suite đóng gói đạt 82/82 test chạy (chín heavy skip); full AGC
parity đạt đủ sáu BR/96 CSV/30 lineage/67.829 episode. Cold probe cuối dùng
browser/app process mới đạt sau **356,497 giây**; lượt source-fix trước cấu
hình telemetry đạt 189,908 giây. Hai lượt có điều kiện khác nhau, không tính
thành phân phối benchmark hay SLA. Số đo T49 giữ đúng cấu hình lúc đo.

## Giới hạn vận hành được công bố

Warm p95 ≤2 giây là workload Automatoes đã đo, không phải SLA cold start hay
concurrent users. Cold proof CH ở benchmark 141,242 giây; UI/harness có thể lâu
hơn theo tài nguyên và config. Scenario write p95 5.634,63 ms, retry 6.166,69 ms;
read 140,05 ms. Sensor p95 2.794,94 ms là một burst tương quan 30 event AGC,
không phải 30 trial độc lập. PostgreSQL nhanh hơn ở các workload đã đo;
ClickHouse active table footprint nhỏ hơn, không gộp logs/WAL/temp vào kết
luận nén. Không có bằng chứng scale-out hoặc dữ liệu lớn hơn AGC.

Storage failure trong raw rebuild được giữ log; retry tuần tự với headroom
guard đạt sau dọn cache/bản sao thử đã xác minh. Không cần privileged reclaim,
không prune volume nguồn. Cần dung lượng cho raw, DB, sort/temp, WAL/checkpoint,
video và backup; không xem active table footprint là dung lượng máy cần có.

## Điều kiện trước chuyển backend

Walkthrough/checksum/frame review T51 đã đạt. Không còn lỗi correctness/recovery
được phát hiện chưa xử lý trong phạm vi các gate được dẫn. T39 tiếp tục cutover và rollback delta đầy đủ event/correction/config/
scenario; đổi connection string về snapshot cũ không đáp ứng rollback.
Report/slides/README/gói ứng dụng chốt sau các gate đó. Video thuyết trình tổng
thể 20–30 phút được user tự quay offline sau cùng, không được giả bằng bộ
walkthrough tự động.
