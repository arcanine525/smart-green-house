# Diễn tập full AGC và phục hồi local

Chạy từ clone riêng với `.env.example`, không dùng `.env` hoặc database đang vận
hành. Cần Git, Docker/Compose và các tiện ích shell thông thường; Python, Java,
Spark và PostgreSQL chạy trong container. Dành ít nhất **16 GiB trống sau khi
chuẩn bị image** cho workload snapshot khóa; đây là ngưỡng của bài diễn tập,
không phải bảo đảm dung lượng cho dataset lớn hơn. Không tự prune cache/volume.

## 1. Chuẩn bị có Internet

Clone repository, tạo `.env` từ mẫu và đặt ZIP AGC đã khóa vào `data/import/`
như README. Archive là đầu vào ngoài Git; snapshot và pipeline kiểm SHA-256.
Tạo image riêng, không thay tag của ứng dụng đang dùng:

```bash
cp .env.example .env
run_id=$(date -u +%Y%m%dT%H%M%SZ)
export REPRO_ROOT="$PWD/tmp/full-repro/$run_id"
export REPRO_PROCESSING_IMAGE="greenhouse-repro-processing:$run_id"
export REPRO_UI_IMAGE="greenhouse-repro-ui:$run_id"
export REPRO_SENSOR_IMAGE="greenhouse-repro-sensor:$run_id"
docker build -t "$REPRO_PROCESSING_IMAGE" -f docker/processing/Dockerfile .
docker build -t "$REPRO_UI_IMAGE" -f docker/ui/Dockerfile .
docker build -t "$REPRO_SENSOR_IMAGE" -f docker/sensor/Dockerfile .
docker compose --profile sensor pull db broker
```

Có thể dùng Docker build cache sau khi đã tải dependency; runtime không truy cập
Maven/pip/dataset server. Với máy hoàn toàn offline, chuẩn bị và chuyển image,
repository cùng ZIP trước, dùng `docker save`/`docker load` theo Docker.

## 2. Runtime không có Internet

```bash
./scripts/rehearse_agc_batch.sh
./scripts/rehearse_agc_restore.sh
```

Script thứ nhất tạo project/volumes mới, snapshot, migration, full pipeline,
biểu giá lịch sử và app. Cả backend/frontend đều `internal: true`; mỗi lệnh
runtime dùng `--pull never`, không build. Kiểm network và một kết nối IP ngoài
bị từ chối. Host vẫn có thể online: phạm vi cách ly là **container ứng dụng**.

Script thứ hai:

1. Chạy unit/backend và UI tests trong môi trường riêng.
2. Đăng ký rõ rule demo khí hậu/drain/thu hoạch, tạo scenario điện +20% trong
   database thử. Kiểm tám màn hình, KPI đối chiếu raw, rule và scenario qua AppTest.
3. Replay 30 dòng AGC Automatoes qua MQTT; xử lý phần đầu bằng Spark, gửi phần
   còn lại vào persistent session khi collector dừng. Giữ SQLite WAL/SHM.
4. Dừng writer, fingerprint từng hàng của mọi bảng ứng dụng, `pg_dump`, lưu ZIP đầu vào, raw
   và toàn bộ sáu volumes: outputs, broker, outbox, spool, ready, checkpoint.
5. Kiểm archive rồi xóa **chỉ project nguồn thử nghiệm do script tạo**, nhằm giảm
   dung lượng đồng thời; restore vào project mới. So số dòng/SHA-256 bảng và
   checksum mọi file của ZIP/raw/volumes trước khi khởi động writer.
6. Xác minh broker giao các hàng đã queue **trước khi publisher retry**; resume
   Spark với batch ID mới, kiểm 30 event duy nhất và envelope/checksum nguyên vẹn.
7. Resume workflow lịch sử từ outputs đã restore, kiểm lại UI/KPI/scenario và
   health và màn hình sensor với 30 event, hai rule và bốn chart. Giữ project phục hồi ở trạng thái dừng để có thể kiểm tra thêm.

Đây là kiểm thử trên toàn bộ snapshot lịch sử và một cửa sổ sensor giới hạn;
không phải test mọi lỗi broker, mất điện hay replay toàn vụ. Các fault khác có
bằng chứng riêng trong báo cáo sensor. Rule và diện tích vẫn là cấu hình demo/
giả định có nhãn. UI dùng AppTest trong lượt này; video browser ở T26 là lượt riêng.

## 3. Artifact, lỗi và dọn tài nguyên thử

`$REPRO_ROOT/evidence/` giữ log, inventory và kết quả UI trước/sau. Backup ở
`backup/`, metadata sensor ở `state/` và `restored/state/`. Không ghi credential
vào báo cáo; `.env` riêng phải được giữ an toàn. Backup và dataset ở `tmp/`,
ngoài Git. Muốn giữ lâu, sao chép chúng sang nơi lưu trữ phù hợp.

Nếu có lỗi, script dừng và giữ trạng thái để chẩn đoán; không tự lặp lại một
rehearsal cũ hoặc tự sửa checkpoint. Đọc `source-project.txt` và
`restore-project.txt` để xác định đúng tài nguyên đã tạo. Sau khi kiểm xong,
dọn project thử bằng tên chính xác đã ghi:

```bash
repro_project=$(cat "$REPRO_ROOT/restore-project.txt")
docker compose -p "$repro_project" -f compose.yaml -f "$REPRO_ROOT/runtime.yaml" \
  --profile '*' down -v --remove-orphans
```

Không dùng tên project đang vận hành hoặc chạy cleanup toàn máy. Những image
tag `greenhouse-repro-*`, archive và clone vẫn giữ local; chỉ xóa khi không còn
cần bằng chứng hoặc khả năng tái lập. Xem [báo cáo](../../docs/evaluation/local-repro.md).

Fingerprint DB dùng SHA-256 của từng hàng JSON, theo thứ tự khóa chính, rồi
SHA-256 của chuỗi hash; so thêm số dòng. Sequence (nếu có) kiểm last_value và
is_called. Không xuất nội dung từng hàng DB vào evidence Git.
