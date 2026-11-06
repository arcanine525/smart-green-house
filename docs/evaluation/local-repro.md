# Chạy lại local: bằng chứng từng phần

Lượt kiểm tra ngày 2026-11-06 dùng bản export `git archive` của HEAD
`192aee190874436d203df6a92bbcdf778735809c`, cộng README/backup hướng dẫn đang
soạn, `.env` mẫu và các image đã build sẵn; đây không phải một clone mới tải
image/dependency. Compose project test riêng có database/volumes riêng. Hai
network backend/frontend được đặt `internal: true`, kiểm bằng `docker network
inspect`; các image PostgreSQL/Mosquitto/processing/UI đã có sẵn, không pull lúc
chạy. Các tài nguyên test do lượt chạy tạo đã được dọn sau đó, không xóa database
hay volume project gốc.

| Kiểm tra | Kết quả | Phạm vi |
|---|---|---|
| Migration | 001–016 áp dụng thành công lên PostgreSQL mới | Database trống riêng |
| UI | Streamlit health `ok`; 1 test empty-state pass | Không chứng minh sáu BR với AGC trong DB mới |
| Backend | 21 test pipeline rerun/correction và contracts pass | Fixture synthetic; stage pipeline cho test điều phối được thay bằng artifact nhỏ |
| Sensor | 19 test: MQTT parity 1, Spark restart 1, fault matrix 7, QoS 8, PostgreSQL restart 1, broker/collector/simulator restart 1 | Fixture và broker/DB riêng, không có AGC toàn vụ |
| Backup/restore | Dump/restore DB synthetic có một hàng; copy và so raw manifest; tar/restore một volume marker thành công | `./scripts/test_backup_restore.sh`; không kiểm toàn bộ snapshot, SQLite WAL hoặc checkpoint thực |

Ổ host lúc bắt đầu còn **6,1 GiB**, trong khi [benchmark](performance.md) đo
một DB Hybrid khoảng **3,319 GiB**, chưa bao gồm toàn bộ WAL/staging và các
volume khác. Vì vậy không thực hiện full AGC rebuild trong lượt này để tránh
làm đầy ổ và ảnh hưởng database đang dùng. Chưa có bằng chứng từ clone mới hoàn
toàn, tải trước dependency rồi ngắt Internet, nạp toàn AGC, xem đủ sáu BR và
khôi phục toàn bộ trạng thái sensor thực. Những phần đó vẫn cần diễn tập khi có
đủ dung lượng, lưu nguyên kết quả pass/fail và version. Mạng `internal` chỉ là
bằng chứng cách ly của các container trong lượt thử; không phải chứng minh toàn
bộ máy bị ngắt Internet.

## Lượt smoke offline synthetic bổ sung

Lúc 16:05 UTC ngày 2026-11-06, sau khi build và chuẩn bị image, chạy
`./scripts/test_offline_smoke.sh` từ source HEAD
`03b2a6f4c8c8936b137b461a84e7c5c69cec206b` (script điều phối mới chưa commit
lúc chạy). Hai network Compose đều `internal=true`; application health `ok`;
**21 test batch/contract pass**, trong đó test Spark batch và resume dùng JVM/Spark
thật; **97 UI test pass**; **19 sensor/fault/recovery test pass**. Sensor dùng
broker/PostgreSQL test riêng và thử restart thật. Kết quả:
[JSON](assets/local-repro/offline-smoke-20261106.json). Tất cả volume/project
do lượt thử tạo được dọn; dữ liệu project gốc không bị xóa.

Script từ chối bắt đầu nếu image chưa có hoặc tên project/volume đã tồn tại;
không build/pull trong smoke. Lệnh được chạy sau bước chuẩn bị image; network của
container là internal nhưng host không bị ngắt Internet. Các bài dùng fixture
synthetic; đây không chứng minh full AGC trên clone mới hoặc restore checkpoint
thật. Những bước đó vẫn cần đủ dung lượng và diễn tập riêng.

## Diễn tập sensor backup/restore synthetic

Ngày 2026-11-06, `./scripts/test_sensor_backup_restore.sh` tạo hai project
Compose và hai database PostgreSQL test riêng. Fixture gồm hai event Climate
tổng hợp, manifest/checksum, **SQLite outbox và spool thật**, raw-ready và một
Spark Structured Streaming checkpoint thật. Test giữ một SQLite read snapshot,
commit dữ liệu vào WAL rồi kết thúc process đột ngột để tái hiện trạng thái
WAL/SHM còn trên đĩa; không mô phỏng mất điện thực tế.

Sau khi writer/Spark dừng, test dump PostgreSQL và lưu **cùng thời điểm** các
file synthetic; phục hồi sang project/database mới với cùng hostname nội bộ
`db`, database/user và đường dẫn source/checkpoint trong container. Ở bản phục
hồi, bản sao chỉ có SQLite base file có **0 row** telemetry/spool; khi kèm WAL,
SQLite `integrity_check` đạt và phục hồi **2 telemetry** cùng **6 spool rows**.
Spark resume checkpoint, báo **batch ID 1 với 5 input rows**, thay vì xử lý lại
batch ID 0; DB cuối có 2 batch, 6 delivery, 2 event duy nhất và publication
complete với checksum khớp. [Kết quả máy đọc được](assets/local-repro/sensor-backup-restore-20261106.json).

Đây là một lượt synthetic nhỏ dùng các thư mục bind được tar, không chứng minh
restore toàn bộ **named volumes**, broker persistence hoặc AGC thật. Các kiểm
tra broker restart riêng được ghi ở [sensor replay](sensor-replay.md). Script từ
chối tên project/volume/network đã tồn tại và chỉ dọn các project test của nó;
backup state và dump test ở `tmp/sensor-backup-restore/<run-id>`.

## Clone local sạch và snapshot AGC

Một lượt khác ngày 2026-11-06 tạo **clone Git local** bằng `--no-hardlinks`,
`--single-branch` tại commit `e3a586ed3c77d59ea0e2c1d397d7dab7912213fa`,
copy `.env.example` thành `.env` trong clone và build image từ clone. Build sử dụng
**Docker cache có sẵn**, không phải xác minh build không cache hay tải từ GitHub.
Sau đó `test_offline_smoke.sh` dùng project/database mới, network backend và
frontend `internal=true`: app health `ok`, **21 batch/contract**, **97 UI** và
**19 sensor/recovery** pass. DB/volume test được dọn; Git status của clone sạch.

Bản ZIP AGC có sẵn được **copy** vào `data/import` của clone. Service `snapshot`
chạy riêng trên network backend internal, xác minh ZIP/snapshot và tạo
**45 file, 622.899 dòng CSV**, snapshot SHA đã khóa. Raw snapshot và ZIP copy nằm
trong thư mục test bị Git ignore, không ghi đè nguồn gốc của người dùng.
[Kết quả máy đọc được](assets/local-repro/clean-clone-20261106.json).

Lượt này chưa chạy full pipeline AGC vào DB mới vì dung lượng host còn khoảng
5,4 GiB; cũng chưa chứng minh build dependency không dùng cache, ngắt Internet
của toàn host hoặc restore dữ liệu/named volume thật. Các giới hạn này vẫn mở.
