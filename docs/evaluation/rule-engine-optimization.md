# Tối ưu tính thời lượng rule — phép đo follow-up

Lượt đo 2026-11-06 so sánh **trước và sau một thay đổi engine dùng chung**:
trong vòng lặp slot đầy đủ, dùng lại cadence/thời lượng nguyên phút và chỉ tính
phép chia Decimal khi cửa sổ cắt qua biên slot. Không thay input, quality, rule,
publication, cách dựng episode hay công thức. Grid vẫn lấy số slot nguyên theo
phép chia lấy phần nguyên như trước; test riêng kiểm cadence bảy phút và cửa sổ
cắt ở nửa phút. Thay đổi áp dụng cho cả BR-01 và BR-03.

## Kết quả đo

Dùng publication `df3e4bcbacab45a5b86fc37d2f53c00a`, Automatoes, cả vụ,
rule `climate-demo-b01:Automatoes:v1`, basis requested, cache kết quả **tắt**;
đọc DB PostgreSQL chỉ đọc. Mỗi lượt có 5 warm-up và 30 mẫu service end-to-end;
p95 nearest-rank là mẫu thứ 29 sau khi sắp xếp. Job 1,5 CPU/2 GiB, DB 0,5
CPU/512 MiB; cùng host và database với [benchmark trước](performance.md). Hai
lượt chạy **tuần tự trước → sau**, không kiểm soát toàn bộ hoạt động host/OS cache.

| Kết quả | Trước | Sau | Thay đổi quan sát |
|---|---:|---:|---:|
| Median BR-01 | 1.911,05 ms | 1.543,71 ms | −19,22% |
| p95 BR-01 | 2.450,09 ms | 1.770,93 ms | −27,72% |
| Số episode khí hậu | 3.959 | 3.959 | Bằng |
| Số episode drain | 447 | 447 | Bằng |

SHA-256 của JSON contract **đầy đủ** của 18 phép đánh giá đều khớp trước/sau:
sáu khoang × khí hậu requested, khí hậu realized và drain measured. Các hash
bao gồm slot deviation, episode, coverage, rule và provenance; [trước](assets/rule-engine/all-cycles-before.json),
[sau](assets/rule-engine/all-cycles-after.json), [script đối chiếu](assets/rule-engine/all_cycles_parity.py).
Bộ local sau thay đổi đạt **420 backend, 97 UI, 2 test công cụ usability và
19 sensor/recovery**, gồm phép thử khôi phục tiến trình thật; [số liệu test](assets/rule-engine/test-results.json).
Hai phép đánh giá Automatoes trong lượt timing cũng có hash trùng khớp. Raw 30 mẫu, hash và thời điểm: [trước](assets/rule-engine/before.json),
[sau](assets/rule-engine/after.json). BR-03 ở đây được kiểm fingerprint, không
được đo p95 trước/sau. Một lượt đo cho mỗi phiên bản và một khoang không đủ kết
luận SLA hay hiệu năng trên mọi máy.

Benchmark trước đó ghi Hybrid BR-01 **4.060,81 ms** và Kimball **3.028,13 ms**;
đó là phép đo khác và được giữ nguyên, không thay bằng số mới. Lượt follow-up này
đạt ngưỡng tham chiếu 2 giây **trong điều kiện được nêu**; thời gian trước/sau
khác nhau có thể chịu ảnh hưởng cache và tải nền. Chưa đo lại Kimball hoặc cold
cache; không kết luận mức cải thiện cho hai trường hợp đó.

## Chạy lại

Cần database benchmark chứa publication trên và image processing đã build, `.env`
local và các source checkout tương ứng. Runner [benchmark.py](assets/rule-engine/benchmark.py)
kiểm `transaction_read_only`, không ghi database. Tạo bản source trước thay đổi
vào thư mục tạm (không checkout/đổi working tree):

```bash
mkdir -p tmp/engine-before tmp/engine-measurements
chmod 777 tmp/engine-measurements
git archive c902107c08f5cf0e16337361a07bf1ed66a14626 src | tar -x -C tmp/engine-before
```

Chạy hai lệnh tuần tự, không chạy test hoặc benchmark khác đồng thời. Chỉ đường
mount source và tên file output thay đổi; các điều kiện khác giữ nguyên:

```bash
GREENHOUSE_JOB_CPUS=1.5 GREENHOUSE_JOB_MEMORY=2g docker compose --profile test run --rm --no-deps \
  -e PGDATABASE=greenhouse_hybrid_bench_t25_20261106_104814 \
  -e PGOPTIONS='-c default_transaction_read_only=on' -e PYTHONPATH=/workspace/src \
  -e PROFILE_OUTPUT=/evidence/before.json \
  -v "$PWD/tmp/engine-before/src:/workspace/src:ro" \
  -v "$PWD/docs/evaluation/assets/rule-engine/benchmark.py:/tmp/bench.py:ro" \
  -v "$PWD/tmp/engine-measurements:/evidence" test-runner python /tmp/bench.py

GREENHOUSE_JOB_CPUS=1.5 GREENHOUSE_JOB_MEMORY=2g docker compose --profile test run --rm --no-deps \
  -e PGDATABASE=greenhouse_hybrid_bench_t25_20261106_104814 \
  -e PGOPTIONS='-c default_transaction_read_only=on' -e PYTHONPATH=/workspace/src \
  -e PROFILE_OUTPUT=/evidence/after.json \
  -v "$PWD/src:/workspace/src:ro" \
  -v "$PWD/docs/evaluation/assets/rule-engine/benchmark.py:/tmp/bench.py:ro" \
  -v "$PWD/tmp/engine-measurements:/evidence" test-runner python /tmp/bench.py
```

Đối chiếu cả hai `climate_digest`, `drain_digest` và số episode trước khi so
thời gian. Source hiện tại có thể tiếp tục thay đổi; pin commit khi tái đo. Các
fixture, fault/recovery và UI được chạy riêng, không lấy hash một khoang để
thay cho toàn bộ kiểm thử correctness.

### Đối chiếu cả sáu khoang

Dùng cùng setup và hai mount source như trên, đổi `PROFILE_OUTPUT` thành
`PARITY_OUTPUT=/evidence/parity-before.json` / `parity-after.json`, thay mount
runner bằng `-v "$PWD/docs/evaluation/assets/rule-engine/all_cycles_parity.py:/tmp/bench.py:ro"`
và giữ `test-runner python /tmp/bench.py`. Mỗi lượt xuất 18 hash JSON contract
đầy đủ, gồm climate requested/realized và drain của từng khoang. Sau hai lượt so
trường `results` và `publication_id` trong hai file JSON; khác nhau là lỗi,
không được lấy kết quả thời gian làm bằng chứng correctness.
