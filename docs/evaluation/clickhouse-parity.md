# Đối soát full AGC: PostgreSQL → ClickHouse

Bản ứng viên dùng toàn bộ cửa sổ lịch sử của sáu khoang, từ 16/12/2019
đến trước 30/05/2020. PostgreSQL là đối chứng chỉ đọc; ClickHouse chạy trong
project Docker riêng. Chuyển backend mặc định, benchmark, backup/restore và
walkthrough là các bước tiếp theo, không được suy ra từ kết quả correctness.

## Định danh và phạm vi

| Thành phần | Định danh |
|---|---|
| ZIP SHA-256 | `c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68` |
| PostgreSQL publication | `892b4cd58cdf4d099510974d91fa64ab` |
| ClickHouse publication | `8572a2e99a0a4a839aacb41f8f669916` |
| Candidate load | `9abecd871a194cddb4543bc105e4d5d9` |
| Workflow | `full-agc-pg-metadata-v1` |
| Frozen source metadata hash | `f0731bf316455b6a8e4c0dbb7bce1f66c262c1946dc43df0273465bd975d72a1` |
| Cấu hình | `pg-water-v1`, `pg-harvest-v1`, `pg-threshold-stream-v2`, `pg-energy-v1` |

ZIP và raw snapshot được kiểm hash trước backfill. Manifest kiểm kê 45 file:
24 file được parser đọc (288.050 dòng: 288.049 accepted, một quarantine),
21 file deferred theo phạm vi sáu BR đã chốt. Sáu file LabAnalysis có tổng
60 dòng staging, giữ ngoài canonical core; BR-03 dùng số đo process computer.
Không tuyên bố tất cả file ZIP đã thành fact. Raw giữ nguyên cả phần deferred.

Core có 287.989 revision: 286.854 climate, 996 Resources và 139 Production.
Mỗi file climate có 47.809 mẫu, gồm mẫu đóng vụ. Khoảng KPI nửa mở chứa
47.808 slot/khoang. Một dòng Production của Reference ngoài vụ được quarantine;
không xóa dòng hoặc ép thành sản lượng bằng 0. Typed core có 14.062.934 measures.
Threshold marts có 2.868.540 measures, water 1.992, harvest 278, energy 2.988.
Số dòng physical giữa hai schema không phải điều kiện đối soát.

## Hợp đồng đã đối chiếu

| Nhóm | Kiểm tra |
|---|---|
| BR-01, BR-03 | 18 nhóm: requested/realized climate và measured drain × sáu khoang; 67.829 episode gồm cả biên vụ; metric DTO, duration, unknown, suspect, joint coverage và fingerprint giá trị/quality từng slot |
| BR-02, BR-05 | Sáu vụ đầy đủ: daily/weekly, tổng lượng, cơ cấu A/B, coverage, basis và quality |
| BR-04, BR-06 | Lượng nhiệt/điện, intensity trên sản lượng A, biểu giá, baseline và thay đổi giá điện +20%; giữ nhãn basis giả định |
| CSV | 60 CSV nghiệp vụ + 36 CSV threshold; fingerprint nội dung của production exporter |
| Lineage | 30 lượt kiểm năm subtype × sáu khoang: source/file/line/raw hash, giá trị, units, provenance |

Các nhóm này đã khớp PostgreSQL; threshold và quantities còn đối chiếu oracle
đọc CSV độc lập. Không điều chỉnh expected KPI để làm phép so sánh đạt.
Chỉ ánh xạ định danh vật lý publication/revision/load của hai engine, bỏ header
chú thích provenance khỏi phép so CSV và chuẩn hóa biểu diễn Decimal bằng nhau.
NULL và 0 vẫn phân biệt. Units được so theo measure code vì collation PostgreSQL
và Python có thứ tự khác nhau; không bỏ cặp unit nào.

## Phiên bản cấu hình và scenario

Export chạy trong PostgreSQL transaction `REPEATABLE READ, READ ONLY`, đồng thời
đặt `default_transaction_read_only=on`. Toàn bộ bảng metadata gốc được lưu trong
immutable `migration_source`, gồm tám publication header/seal lịch sử. Publication
lịch sử này được lưu như chứng cứ nguồn, không tạo tám candidate pin giả.

Native inventory khớp 84 basis, 14 rule schedule revision (40 segment), 12 harvest
rule và bảy tariff (21 rate). Nguồn có hai basis assumption, không có irrigation
rule/correction; các bảng rỗng vẫn được lưu trong archive. Basis hoạt động lấy từ
42 pin `shared_analysis` của publication đối chứng, không suy ra từ latest config.

174 scenario version thuộc 167 family được import sau khi full six-BR parity
đạt. Giữ scenario/version/request, rates/changes, author và thời điểm tạo. Chỉ
ánh xạ publication và publication ID của analysis context sang candidate đã
chứng minh. Mapping envelope lưu original/target ref, metadata hash và proof hash.
Import từ chối source archive khác, chain thiếu version, family phân kỳ hoặc
reference chưa được chứng minh. Native inventory đọc lại toàn bộ phiên bản.

Audit `information_schema` của DB nguồn hiện tại không có sensor schema. Vì vậy
không có event sensor cũ để chuyển từ DB này; replay AGC mới là kiểm tra bổ sung,
không phải chứng cứ đã chuyển event vốn không tồn tại.

## Bộ nhớ, retry và giới hạn

Lần chạy full đầu tiên gặp ClickHouse memory limit (1,5 GiB query limit trong
container 2 GiB) trước khi seal; không phải thiếu Docker storage. Source/load
và trạng thái PREPARING/WRITING được giữ. Sau khi giới hạn stream reader, resume
cùng workflow/load hoàn tất chính publication trên, không nạp lại nguồn.

Canonical source dùng spool tạm trên disk, stream HTTP và kiểm typed measures
theo batch; không trả membership khi response bị ngắt. Threshold v2 build/verify
từng batch, checksum ordered JSONL và phiên bản storage explicit. Publication
v1 của rehearsal cũ vẫn xác minh bằng code mới và thuật toán checksum gốc.
Exact physical retry được collapse, nội dung xung đột vẫn bị từ chối.

Bộ test đóng gói cuối: 77 discovered, 68 executed và chín heavy integration skip
tường minh, đạt trong 157,759 giây. Các test cover interrupted stream, 4.200-row
spool, correction/frozen pin, v1/v2 receipt, tamper và scenario import/retry.
Chín heavy test không được tuyên bố đã chạy lại trong lượt này; xem riêng
[sensor fault evidence](assets/clickhouse-sensor/manifest.json) và
[main CLI Spark crash/resume](assets/clickhouse-ui-cli/manifest.json).

Full verification hiện có chi phí lớn; chưa nghiệm thu p95 UI. Harness correctness
tái sử dụng pin trong cùng action và vừa đọc full slots cho duration engine;
đó không phải phép đo latency. T49 phải đo cold/warm/app riêng và xử lý regression.
Không có prune Docker cache/volume trong backfill này.

## Chạy lại

Các script tại `scripts/clickhouse/` được mount read-only vào `/migration` của
processing image. Chuẩn bị image trước khi offline; dùng project/state mới,
không chạy vào deployment PostgreSQL hiện có. Ví dụ cấu hình compose:

```bash
export GREENHOUSE_CH_STATE_PATH="$PWD/tmp/ch-migration-example"
export GREENHOUSE_CH_RAW="/data/raw/agc-2nd-2019/c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68"
docker compose -f compose.clickhouse.yaml -p ch-migration-example \
  up -d --wait --no-build --pull never clickhouse
```

1. Chạy `export_postgres.py --publication-id SOURCE --output PATH` trên kết nối
   PostgreSQL nguồn chỉ đọc; đặt file kết quả thành `/candidate/source-metadata.json`.
   File này là dump cấu hình local, không commit.
2. Trong `ch-job`, chạy `python /migration/backfill.py --metadata
   /candidate/source-metadata.json --raw "$GREENHOUSE_CH_RAW" --archive
   /data/import/autonomous-greenhouse-challengeagc-2nd-2019.zip --output
   /candidate/output --coordinator /candidate/coordinator --workflow-id NAME`.
   Chạy lại đúng lệnh/config/workflow để resume hoặc kiểm retry.
3. Dùng bốn script `read_*_reference.py` lấy business/CSV/lineage/threshold reference
   trên đúng PostgreSQL publication, giữ `read_only` và snapshot ID.
   Chạy `check_parity.py --publication-id TARGET --raw RAW --coordinator
   /candidate/coordinator --references REFERENCES --output OUTPUT` trên candidate.
4. Chỉ sau parity đạt, chạy `python -m greenhouse.clickhouse.migration --metadata
   /candidate/source-metadata.json --proof PARITY_JSON --coordinator /candidate/coordinator`.
   Chạy lại để xác minh idempotency; `audit_migration.py` kiểm inventory native.
5. Chạy `tests/clickhouse/check_agc_sensor.py` bằng `ch-stream`, truyền target pin,
   raw, coordinator, thư mục state và output riêng. Kiểm này dùng simulator thật
   chọn 30 event từ file gốc, MQTT, durable collector và Spark; checksum file gốc
   giữ nguyên, completion manifest chỉ công bố selection 30 event.

Không dùng lại proof của publication khác; không bỏ gate archive/mapping khi
import scenario. Dữ liệu raw, metadata dump, CSV download và video giữ local.

Lặp một lượt MQTT mới phải dùng `--replay-id` và `--state` mới. Simulator
không phát lại replay đã `completed`; harness kiểm điều kiện này trước khi
đọc historical pin. Cả hai slot view của replay mang 10 quality verdict, còn
historical climate/drain mang 8/2: proof so toàn bộ 10 verdict theo đúng view,
bao gồm reason/applicability và giá trị Decimal, không bỏ qua measure.

## Kết quả gate full backfill

T38 đạt trong phạm vi correctness: full snapshot, sáu BR, phiên bản cấu hình/scenario, fresh AGC30 sensor và retry đều đã kiểm. Kết quả và checksum từng artifact nằm trong [manifest](assets/clickhouse-parity/manifest.json).

Hai lượt mở client mới không bật action cache: rerun workflow mất 303.83 giây; retry import scenario mất 322.24 giây. Publication/load/receipts và physical count mọi bảng giữ nguyên; lần import thứ hai chèn 0 và tái sử dụng đủ 174 phiên bản. Đây là thời gian của diễn tập correctness, không phải benchmark so engine trong T49.
