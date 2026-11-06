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
