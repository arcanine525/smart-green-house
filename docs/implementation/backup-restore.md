# Backup và khôi phục local

Quy trình đã được smoke test trên dữ liệu tổng hợp: dump/restore DB, raw và
một named-volume marker bằng `./scripts/test_backup_restore.sh`; thêm SQLite
WAL/SHM của outbox/spool và Spark checkpoint thật bằng
`./scripts/test_sensor_backup_restore.sh`. Lượt sensor dùng **bind directory**
synthetic, không phải restore toàn bộ named volumes hay AGC thật; xem
[bằng chứng và giới hạn](../evaluation/local-repro.md#diễn-tập-sensor-backuprestore-synthetic).
Quy trình không bảo đảm snapshot nhất quán khi còn writer. Không dùng
để ghi đè project/volume đang có dữ liệu. Database, raw và checkpoint cần được
lưu cùng một mốc dừng; riêng database dump không đủ để tiếp tục sensor replay.

## Chuẩn bị và dừng writer

Chọn cửa sổ bảo trì, đảm bảo không có job `run`, người dùng UI, simulator,
collector hoặc stream worker khác đang ghi. Chờ các lệnh một lần kết thúc; chỉ
`docker compose stop` không dừng container `docker compose run` khác. Trên project
`smart-greenhouse`, dừng các writer dài hạn, giữ PostgreSQL chạy để dump:

```bash
docker compose -p smart-greenhouse stop app simulator collector stream-worker broker
docker compose -p smart-greenhouse up -d --wait db
umask 077
backup_dir="$PWD/../greenhouse-backup-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir/volumes"
docker compose -p smart-greenhouse exec -T db sh -c \
  'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup_dir/database.dump"
```

Đảm bảo lệnh dump thành công, file khác rỗng và có đủ dung lượng. Lưu một bản
copy của **toàn bộ raw root** được cấu hình bằng `GREENHOUSE_RAW_PATH`, bao gồm
manifest/checksum và nguồn dùng bởi lineage. Ví dụ khi dùng mặc định:

```bash
cp -a ./data/raw "$backup_dir/raw"
```

Snapshot/archive ZIP gốc, cấu hình và mật khẩu local cũng cần được lưu riêng an
toàn; `.env` có bí mật, không commit hay đưa vào artifact công khai. Các volume
sau chứa output, broker persistence, outbox SQLite, collector spool, raw-ready và
Spark checkpoint. Ảnh PostgreSQL đã build/pull được dùng chỉ để chạy `tar`:

```bash
archive_image='postgres:17.10-bookworm@sha256:9b18b78397054fce88a9552e9d5a3ad5bb7fd258c5b3cc1c5028e46373d6ea8f'
for volume in outputs broker_state simulator_outbox collector_spool raw_ready spark_checkpoints; do
  if docker volume inspect "smart-greenhouse_${volume}" >/dev/null 2>&1; then
    docker run --rm --network none -v "smart-greenhouse_${volume}:/source:ro" \
      -v "$backup_dir/volumes:/backup" "$archive_image" \
      tar -C /source -cf "/backup/${volume}.tar" .
  fi
done
```

Thiếu archive nghĩa là volume chưa tồn tại tại thời điểm backup; ghi lại trạng
thái đó, không tự kết luận dữ liệu rỗng. Sau khi hoàn tất và xác minh file, có
thể khởi động lại các dịch vụ theo [hướng dẫn sensor](../../src/greenhouse/sensor/README.md).
Lưu bản backup ngoài ổ đang chạy và kiểm soát quyền đọc; raw có thể lớn.

## Khôi phục trên project mới

Dùng checkout/config riêng và **tên Compose mới chưa tồn tại**, không trỏ tới
volume hay raw hiện hữu. Copy raw backup vào thư mục mới và đặt
`GREENHOUSE_RAW_PATH` tới thư mục đó trong `.env` mới. Chuẩn bị image đã tải.
Tạo DB mới, sau đó restore logical dump vào database trống:

```bash
restore_project='smart-greenhouse-restore'
# Dừng nếu bất kỳ volume hoặc container đích đã tồn tại.
for volume in pg_data outputs broker_state simulator_outbox collector_spool raw_ready spark_checkpoints; do
  if docker volume inspect "${restore_project}_${volume}" >/dev/null 2>&1; then
    printf 'Volume đã tồn tại: %s_%s\n' "$restore_project" "$volume" >&2
    return 1 2>/dev/null || exit 1
  fi
done
if [ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$restore_project")" ]; then
  printf 'Project đích đã có container\n' >&2
  return 1 2>/dev/null || exit 1
fi
docker compose -p "$restore_project" up -d --wait db
docker compose -p "$restore_project" exec -T db sh -c \
  'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges --exit-on-error' \
  < "$backup_dir/database.dump"
```

Chỉ dùng lệnh trên nếu DB đích thực sự trống. Nếu lỗi, dừng và kiểm tra; không
chạy `--clean` hoặc xóa dữ liệu cũ để ép restore. Với từng archive có sẵn, tạo
volume của project **mới**, gắn label Compose, rồi giải nén khi chưa khởi động
writers:

```bash
for volume in outputs broker_state simulator_outbox collector_spool raw_ready spark_checkpoints; do
  if [ -f "$backup_dir/volumes/${volume}.tar" ]; then
    docker volume create --label "com.docker.compose.project=$restore_project" \
      --label "com.docker.compose.volume=$volume" "${restore_project}_${volume}" >/dev/null
    docker run --rm --network none -v "${restore_project}_${volume}:/target" \
      -v "$backup_dir/volumes:/backup:ro" "$archive_image" \
      tar -C /target -xf "/backup/${volume}.tar"
  fi
done
```

Không phục hồi riêng checkpoint từ một mốc khác với database/spool. Giữ nguyên
stream ID, replay scope, checkpoint generation, đường dẫn ready trong container
và endpoint database (`host`, `port`, `dbname`, `user`) khi resume: chúng được
pin trong marker checkpoint. Compose mới vẫn có thể dùng hostname nội bộ `db`
và đường dẫn `/data/ready` như cũ. Nếu một identity thay đổi, không tự sửa marker
để ép resume; cần một kế hoạch tái dựng và generation mới từ dữ liệu đã bảo toàn.
Thay đổi các identity này không phải cùng lượt replay. Khởi chạy app và kiểm tra publication, raw hash,
KPI/CSV rồi thực hiện replay resume trên bản sao. Đối chiếu manifest và số lượng
receipt/cursor; chỉ sau khi kiểm tra thành công mới xem đây là bản backup sử dụng
được. Nếu checkpoint/spool thiếu hoặc không nhất quán, giữ bản gốc và đánh giá
lại từ raw thay vì tự xóa hoặc gộp scope.
