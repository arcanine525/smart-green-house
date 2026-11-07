# Diễn tập ClickHouse local và phục hồi

Chạy tại main, trên project riêng có tiền tố `gh-ch-repro-`. Không kết nối hoặc
ghi PostgreSQL gốc. Ảnh/dependency được chuẩn bị trước; runtime dùng hai network
`internal`. File `.env` lấy từ `.env.example`, không sao chép credential cá nhân.
Raw, backup, state và video nằm ngoài Git. Xem kết quả thực tế trong
`docs/evaluation/clickhouse-local-repro.md` khi bài diễn tập được nghiệm thu.

## Chuẩn bị

Đầu vào là snapshot AGC/ZIP đã khóa, metadata PostgreSQL export read-only bằng
`scripts/clickhouse/export_postgres.py`, và bốn JSON tham chiếu đã đối soát ở
`docs/evaluation/clickhouse-parity.md`. Cần Docker Desktop đang chạy, image upstream khả
dụng khi build và ít nhất 10 GiB dung lượng host trống. Giữ thêm headroom nếu
đồng thời giữ nhiều fixture; không prune volume người dùng. Nếu thiếu storage,
kiểm `docker system df` rồi dọn **build cache** bằng `docker builder prune -af`.

`run.py` khóa tuần tự mọi helper trong cùng rehearsal, kể cả giữa source/restore/
rebuild. Trước mỗi stage cần ít nhất 3 GiB trống; full backfill/raw rebuild cần
6 GiB. Không chạy cold browser/AppTest hoặc pipeline ngoài runner cùng lúc.
Build cache đã hết không có nghĩa host được trả ngay block trống của Docker
Desktop. Nếu cần thao tác host đặc quyền, phải được chấp thuận riêng; harness
không tự chạy privileged container hoặc prune volume người dùng.

```bash
python3 scripts/clickhouse_repro/prepare.py \
  --source "$PWD" --root "$PWD/tmp/my-ch-rehearsal" \
  --raw /absolute/path/to/locked/agc-snapshot \
  --archive /absolute/path/to/autonomous-greenhouse-challengeagc-2nd-2019.zip \
  --metadata /absolute/path/to/source-metadata.json \
  --references /absolute/path/to/four-reference-jsons

export SETUP="$PWD/tmp/my-ch-rehearsal/setup.json"
repro() { python3 scripts/clickhouse_repro/run.py --setup "$SETUP" "$@"; }
manage_repro() { python3 scripts/clickhouse_repro/manage.py --setup "$SETUP" "$@"; }
export AGC_SHA=c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68
export RAW="/data/raw/agc-2nd-2019/$AGC_SHA"
manage_repro database
manage_repro broker
repro --label backfill -- python /repro/initial_backfill.py --workflow offline-historical
repro --label settle -- python /repro/settle_parts.py --output /state/settle.json
```

`state/historical.json` ghi publication thực, không dùng UUID từ một lần chạy
khác. Mọi lệnh sau phải đọc ID đó. `run.py` từ chối ghi đè log: khi thử lại dùng
label mới để giữ nguyên bằng chứng lỗi. `settle_parts.py` không DEDUPLICATE;
kiểm số hàng trước/sau và ba physical fingerprint cách nhau 10 giây.

```bash
export GH_STATE=$(python3 -c 'import json,os; print(json.load(open(os.environ["SETUP"]))["state"])')
export PUB=$(python3 -c 'import json,os; print(json.load(open(os.environ["GH_STATE"]+"/historical.json"))["publication_id"])')
repro --label parity -- python /migration/check_parity.py \
  --publication-id "$PUB" --transient-rules --raw "$RAW" \
  --coordinator /state/coordinator --references /references --output /state/parity
repro --label offline -- python /repro/offline_probe.py
repro --label prepare-state -- python /repro/prepare_state.py
repro --label historical-ui --service ch-ui-test \
  --env "GREENHOUSE_WALKTHROUGH_PUBLICATION=$PUB" \
  --env GREENHOUSE_WALKTHROUGH_OUTPUT=/state/historical-ui.json \
  -- python /repro/check_ui.py
repro --label inventory -- python /repro/logical_inventory.py --output /state/inventory.json
```

`prepare_state.py` nhập scenario gốc qua parity gate thật, thêm profile và
scenario native, chạy 10/30 event qua MQTT/Spark rồi để phần còn lại trong queue
persistent. Reader transaction giữ dữ liệu đã commit trong SQLite WAL/SHM;
mọi MQTT/Spark worker dừng trước crash-like exit. Archive `native-state.json`
ghi đúng backend ClickHouse, DTO và checksum, không giả export PostgreSQL.
Nếu một bước lỗi, đọc log và chỉ tiếp tục bước chưa hoàn thành; không xóa state
để che lỗi hoặc chạy lại seed trên outbox đã có dữ liệu.

Suite fault-injection phải chạy trong **project/database khác**, không chạy
trên warehouse lịch sử. Canonical `ch-ui-test` là suite có skip tường minh cho
các bài cần harness MQTT/Spark/restart riêng. Bài rehearsal sensor ở đây bổ sung
MQTT, Spark và physical restart thật; không đổi nhãn skip thành pass.

## UI trên Docker Desktop với network hoàn toàn internal

```bash
manage_repro app
python3 scripts/clickhouse_repro/local_ui_tunnel.py --setup "$SETUP"
```

Mở `http://127.0.0.1:18582`. Với cả hai network internal, Docker Desktop có thể
giữ `HostConfig.PortBindings` nhưng không tạo port thực. Tunnel chỉ bind
loopback host và chuyển stdio qua Docker exec tới đúng app/port 8501. Nó không
thêm network hoặc mở egress cho container. Dừng tunnel bằng Ctrl-C trước backup.
Với target restored/rebuilt thêm `--target`; port lần lượt 18583/18584.

## Physical backup/restore

Dừng cả project bằng `manage_repro stop`. Chỉ sau đó dùng
`volume_archive.py backup --project <project> --volume <project>_ch_data
--archive <root>/backup/ch_data.tar.gz --image <client-image>`;
làm tương tự cho `ch_logs`, `ch_broker`. Chạy
`bind_archive.py backup --setup "$SETUP"` để giữ state, WAL/SHM, outbox/spool/
ready, checkpoint, output, raw/ZIP, references và config server/broker.

Mỗi tar có receipt bytes/SHA256. Không overwrite snapshot. Kiểm tất cả receipt
trước khi cân nhắc xóa một fixture; không có lệnh dọn volume tự động trong
harness. Nguồn dữ liệu dự án thật và các ứng viên trước phải được giữ nguyên.

1. `bind_archive.py restore --setup "$SETUP"` trích vào `<root>/restored`,
   kiểm từng file byte-for-byte trước khi bất kỳ SQLite reader nào mở DB.
2. `manage_repro --target restored prepare`, rồi `manage_repro --target restored create`
   tạo container dừng và volume **mới**; từ chối nếu project đã có volume.
3. Dùng `volume_archive.py restore` cho từng target volume `<project>-restored_ch_*`,
   cùng archive/client image. Tool kiểm project label, SHA, target rỗng,
   giữ UID/GID/mode và relative symlink an toàn.
4. `manage_repro --target restored database`; chạy inventory với
   `repro --target restored --label inventory ... --output /state/restored-inventory.json`.
   `compare_inventory.py` phải xác nhận toàn bộ row/schema digest trước resume.
5. `manage_repro --target restored broker`; chạy
   `repro --target restored --label sensor-resume -- python /repro/agc_sensor.py restore`.
   Helper kiểm WAL/SHM, integrity/base-rows, queue đến **trước khi** tạo publisher,
   sau đó retry và resume checkpoint cũ. Cần đủ 30 event duy nhất, envelope
   nguyên vẹn, complete/checksum và business fingerprint chuẩn.
6. Chạy `verify_configurations.py --mode physical --archive /state/native-state.json`,
   UI tám màn hình như trước và `check_sensor_ui.py` bằng service `ch-ui-test`.

## Dựng lại từ raw

`manage_repro --target rebuilt prepare` chỉ chép raw/reference/native archive và
ready envelopes từ bản restore đã kiểm. Coordinator, DB, SQLite và checkpoint
không được mang sang. `manage_repro --target rebuilt create`, rồi `database`.

Chạy `repro --target rebuilt --label raw-rebuild -- python /repro/native_state.py
rebuild --archive /state/native-state.json --raw "$RAW" --workflow raw-rebuild`.
Sau đó maintenance và full parity với publication **mới** (output `/state/parity`).
Chỉ sau khi parity đạt mới chạy `native_state.py import-scenarios` với
`--archive /state/native-state.json --raw "$RAW" --proof /state/parity/parity.json`.
Helper kiểm thêm business projection native source/target và toàn bộ baseline,
rates, request, version chain, author/time, sau đó đọc lại chính xác các DTO.

Chạy `agc_sensor.py rebuild`: toàn bộ ready files được đọc vào generation và
checkpoint mới. Replay config ánh xạ explicit source publication/config hash.
Chạy `verify_configurations.py --mode rebuilt --archive /state/native-state.json`,
rồi UI tám màn hình và sensor UI. Giữ original envelopes cùng mapping riêng;
không tuyên bố raw rebuild có cùng publication ID hay cùng physical DB hash.

Raw-rebuild harness này hỗ trợ snapshot chưa có correction. Nó từ chối source
revision >1/correction hoặc config kind chưa hỗ trợ, thay vì bỏ dữ liệu. Physical
backup vẫn giữ toàn bộ bytes; workflow chuyển delta/correction thuộc cutover.


## Metric log trong runtime 2 GiB

Cấu hình hiện hành tắt collector `system.metric_log` tùy chọn. T51 đo được
một merge bảng log rộng dùng 848 MiB và làm truy vấn UI nhỏ bị hủy; giảm block
đọc không giải quyết nguyên nhân. `system.metrics` và `system.asynchronous_metrics`
vẫn đọc được theo nhu cầu. **Giữ `query_log` bật** vì recovery pending-write cần
terminal query ID/type; không thay nó bằng suy luận từ process list rỗng.

Database mới dùng `docker/clickhouse/local.xml` đã cập nhật. Với bản rehearsal
cũ còn `system.metric_log`, dừng writer và dùng helper có guard project/database
để lưu DDL, inventory/checksum part và tắt riêng automatic merge bảng log cũ:

```bash
repro --target rebuilt --label preserve-metric-log -- \
  python /repro/preserve_metric_log.py --output /state/metric-log-preservation.json
```

Nạp lại XML mới bằng recreate **container cùng volume**, rồi kiểm config
preprocessed có `query_log` và không có `metric_log`; so volume identity và
part inventory trước/sau. Helper không xóa log cũ, không đổi memory limit hay
merge settings của bảng nghiệp vụ. `max_bytes_to_merge_at_max_space_in_pool=0`
được mô tả là tắt automatic merge trong `system.merge_tree_settings` của đúng
ClickHouse 25.8. `SYSTEM STOP MERGES` tạm thời một mình không phải cấu hình bền
vững qua restart. Các số benchmark T49 giữ nguyên nhãn cấu hình lúc đo.
