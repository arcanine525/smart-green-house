# Dashboard và CLI với hai backend

`GREENHOUSE_BACKEND=postgres|clickhouse` chọn backend cho toàn bộ dashboard.
CLI nhận cùng biến môi trường và cho phép ghi đè bằng `--backend`. Mặc định
là **ClickHouse** sau [cutover đã kiểm](clickhouse-cutover-rollback.md). PostgreSQL
chạy bằng `compose.postgres.yaml` và project riêng khi cần đối chứng/rollback. Không có fallback từ ClickHouse sang PostgreSQL.

## Dashboard

Chín màn hình dùng cùng các adapter factory: sáu BR, sensor replay, tổng quan
vụ và chất lượng dữ liệu. Catalog đọc receipts của publication complete để
khám phá các scope; adapter xác minh đầy đủ pin được chọn trước khi đọc KPI
hoặc xuất file. Không tính lại mọi scope khi mở danh sách. Overview báo readiness
từng mart; readiness không đảm bảo đủ quan sát hoặc có rule. Quality đếm quyết định trên membership đã
ghim, không dùng số quyết định làm coverage. Dòng quarantine/conflict/duplicate
lấy từ audit của lần nạp publication; audit cũ không tự bổ sung kết quả từ lần
nạp mới. Dòng không có thời gian vẫn có nhãn và được giữ trong audit.

Historical filters truyền publication, cycle và ngày giữa các màn hình tương
thích. Namespace là backend + host + port + database + user, không có password.
Khi namespace thay đổi, UI xóa widget, historical context và write intent cũ.
Connection mới mỗi action. UI bật bounded process proof cache (tối đa hai entries)
cho immutable publication/load; guard namespace/server identity, table/schema/parts,
pending mutation và cấu hình vẫn kiểm ở mỗi action. INSERT, merge, mutation hoặc
recreate làm proof bị invalidate; không tái dùng business result để bỏ validation.
Command writer không bật read proof cache. Cold proof phải đọc/verify lại, có thể
mất vài phút. Evaluation cache riêng của
climate/drain còn ghim namespace, publication, scope, config và rule content.
Client dùng cho command ghi không bật action cache.

Replay chọn bằng scope/replay/compartment ổn định; khi refresh hoặc fragment
10 giây chạy lại, nó chọn publication mới và giữ nguyên replay đang xem. Mỗi
lần render ghim một ID cho KPI, quality, charts và CSV. Fragment tự mở/đóng
connection theo backend. Late conflict tạo pin mới provisional; pin cũ còn
nguyên. Rule lịch sử chỉ được map sau khi kiểm snapshot và compartment.

Sai cấu hình, warehouse chưa migrate, chưa có publication, chưa có replay và
không kết nối được có trạng thái riêng. Không hiện metric giả hoặc error chứa
credential. UI không tự migrate, publish, backfill hoặc đăng ký biểu giá.

Chuẩn bị image có mạng trước khi chạy offline:

```bash
docker compose -f compose.clickhouse.yaml --profile app --profile tools \
  build ch-client ch-app ch-job
mkdir -p tmp/clickhouse-state/coordinator tmp/clickhouse-state/output
chmod 777 tmp/clickhouse-state tmp/clickhouse-state/coordinator tmp/clickhouse-state/output
docker compose -f compose.clickhouse.yaml -p greenhouse-ch-dev --profile app \
  up -d --wait --no-build --pull never clickhouse ch-app ch-web
```

Mở <http://127.0.0.1:18581>. CH app và DB chỉ vào network internal; localhost
relay có một đích cố định. Chọn `GREENHOUSE_CH_STATE_PATH` khác thì app, job và
stream phải dùng chung coordinator; không chia coordinator theo service.
Các [biến kết nối](clickhouse-local-environment.md) cần được đặt trước khi chạy.

## CLI

`ch-job` dùng processing image, raw/import mount chỉ đọc, output và coordinator
trong `/candidate`; không khai báo PGHOST/PGDATABASE/PGUSER/PGPASSWORD. Ví dụ
pipeline toàn sáu BR từ ZIP/raw AGC đã khóa, basis diện tích giả định có nhãn:

```bash
docker compose -f compose.clickhouse.yaml -p greenhouse-ch-dev --profile tools \
  run --rm --no-deps --pull never ch-job python -m greenhouse migrate
docker compose -f compose.clickhouse.yaml -p greenhouse-ch-dev --profile tools \
  run --rm --no-deps --pull never ch-job python -m greenhouse run-pipeline \
  --scope-id agc-candidate --workflow-id all-six-v1 --agc-shared-basis
```

Lệnh AGC kiểm ZIP size/hash và toàn raw snapshot trước khi nạp. Không dùng nó
cho fixture hoặc coi basis giả định là diện tích đã xác minh. `--climate-start`
và `--climate-end` chỉ dành cho rehearsal có cửa sổ source-naive nửa mở, phải
đi cùng nhau. Dùng `--threshold-version` mới khi thay cửa sổ, giữ pin cũ.

Với dataset/fixture có manifest riêng, thay `--agc-shared-basis` bằng
`--config-json /candidate/config.json --snapshot /data/raw/fixture`. JSON phải
có đúng bốn payload `water`, `harvest`, `threshold`, `energy`; mỗi payload giữ
snapshot/cycles/policy/basis explicit như các constructor hiện có. Chọn version
bằng `--water-version`, `--harvest-version`, `--threshold-version`, `--energy-version`.

Resume dùng `--resume all-six-v1` thay `--workflow-id`, giữ nguyên snapshot,
config payload/versions và `--base-load` nếu lần đầu đã chọn. `transform` và
`publish` chạy cùng combined sealed workflow; không suy version từ latest.
`load-core --scope-id SCOPE --snapshot PATH` chỉ tạo candidate load, không
publish. Muốn dùng load có sẵn phải truyền `--base-load LOAD_ID` tường minh.

Các lệnh sau dùng cùng prefix `docker compose ... run ... ch-job python -m greenhouse`:

```bash
setup --snapshot /data/raw/SNAPSHOT_PATH
explain-core --scope-id SCOPE --compartment Automatoes --date 2020-03-02 --measure Irr
lineage --scope-id SCOPE --publication-id PUBLICATION --kind water \
  --revision-id REVISION --snapshot /data/raw/SNAPSHOT_PATH
register-agc-tariff --scope-id SCOPE --publication-id AGC_PUBLICATION
correct-core --scope-id SCOPE --source-record-id SOURCE --record-type resource \
  --old-value /candidate/old.json --new-value /candidate/new.json \
  --actor reviewer --evidence 'Lý do có bằng chứng' --correction-id correction-001
test -v
```

Correction nhận full measure map JSON, Decimal dạng string/null; không cho sửa
source/time/measure set. Old values stale hoặc reuse ID khác nội dung bị từ chối.
Retry cùng correction trả revision đã ghi; publication mới cần workflow khác.
Climate/drain dùng chung source revision CH nên sửa một record phục vụ cả hai
mart. Lineage yêu cầu publication và kind explicit; kiểm raw hash của source.
Tariff command chỉ nhận AGC publication explicit; đây là biểu giá tham chiếu
lịch sử có provenance, không phải giá thị trường hiện tại.

Stream vẫn dùng `stream --backend clickhouse` và immutable replay config như
[hướng dẫn sensor](clickhouse-sensor.md). Parser chính luôn forward backend
tường minh sang stream, kể cả khi ENV chọn PostgreSQL. `simulate`, `collect`,
snapshot/ingest/inventory độc lập DB, giữ nguyên CLI hiện có.

## Kiểm chứng

`tests/clickhouse/test_backend_ui.py` chạy home.py thật với PostgreSQL connect
bị chặn: toàn sáu BR, shared filters, quality audit/CSV, replay/rule mapping,
refresh pin mới, fragment, namespace change và lỗi/empty state. Replay fixture
được simulator thật phát vào SQLite; test nạp **nguyên bytes** outbox, không
serialize lại payload. Fault/driver/MQTT/recovery thuộc suite sensor T47.

`test_backend_cli.py` gọi entrypoint chính: migrate, setup, combined resume,
explain, lineage, correction/retry và publication mới. PostgreSQL settings bị
chặn trong các command CH. Suite UI PostgreSQL chạy riêng trên DB disposable
để phát hiện regression; không dùng DB baseline thật làm fixture test.

Full snapshot, benchmark, offline/backup và sáu video ClickHouse là các gate
tiếp theo. Rehearsal T48 không thay thế các gate đó.

Lượt nghiệm thu T48: 71 tests được discover, 62 thực chạy và 9 heavy integration
skip có chủ đích; 100 PostgreSQL UI tests đạt. Real Spark qua entrypoint chính
đã chết sau publication rồi resume/reopen an toàn. Chromium đi qua chín màn
hình trên candidate AGC với publication pin đúng; replay browser ở trạng thái
rỗng, replay có dữ liệu/rule/refresh/fragment được AppTest kiểm riêng. Xem
[manifest và logs](../evaluation/assets/clickhouse-ui-cli/manifest.json).
