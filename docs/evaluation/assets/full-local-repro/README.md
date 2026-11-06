# Full AGC offline và backup/restore — 2026-11-07

[summary.json](summary.json) là kết quả kiểm tra tổng hợp, không phải số đo hiệu
năng so sánh. Source ứng dụng từ clone local `--no-hardlinks` tại HEAD
`32156a9850efae1209b88deda5a84652c13d9129`. Không dùng `.env` cá nhân hoặc database
đã chuẩn bị: clone dùng `.env.example`, ZIP AGC đúng checksum và DB/volumes mới.
Build/pull diễn ra ở pha chuẩn bị có Internet, có sử dụng Docker cache; riêng
processing/sensor cũng tải dependency trong build. Không tuyên bố build hoàn toàn
không cache. Docker 29.9.2, Compose 5.6.1; image ID ở [runtime-images.txt](runtime-images.txt).

Runtime dùng mạng backend/frontend internal, `--pull never`, không build hay
truy cập cloud. Kết nối IP ngoài trả mã 101 trước/sau restore. Host vẫn online;
phạm vi offline là các container ứng dụng, không phải toàn bộ máy.

## Kết quả

- Pipeline full từ ZIP/raw vào DB trống: ingest → core → Spark → publish đều
  completed; [workflow steps](workflow-steps.jsonl). Publication lịch sử
  `3e4a051009084e11ab19efdd2343c9d3` được giữ sau resume từ output đã khôi phục.
- **420 backend** và **100 UI test** pass. Tám màn hình lịch sử chạy trước/sau;
  KPI raw oracle, rule khí hậu/drain/thu hoạch và scenario điện +20% khớp.
- **71 bảng, 6.273.255 dòng lưu trong các bảng** khớp SHA-256 và số dòng trước/
  sau restore. Đây là tổng dòng across core/mart/metadata, không phải số event
  AGC duy nhất. Database trước backup: **3.410.441.907 byte**. Không có sequence
  PostgreSQL trong schema này.
- **47 file raw/manifest và 1 ZIP đầu vào** khớp checksum. Cả sáu volume khớp:
  outputs 110 file; broker 1; outbox 6; spool 3; ready 3; checkpoint 9.
- **30 dòng AGC Automatoes** qua simulator → MQTT → collector → Spark. Broker
  phục hồi **28 message queue trước publisher retry**. SQLite base không WAL có
  0 row, bản có WAL khôi phục hợp lệ. Spark tiếp tục batch **1** (56 delivery),
  không chạy lại batch 0 (10 delivery). Kết quả 66 delivery, **30 event duy nhất**,
  envelope nguồn khớp exact, complete và checksum_verified đều true.
- Màn hình sensor sau restore chọn đúng publication, có 30 event, hai rule demo,
  bốn chart và CSV; [sensor-ui.json](sensor-ui.json).

Các file `database-before/after`, `raw-before/after`, `import-before/after` và
`before/after-<volume>` là inventory. `match-*.json` ghi phép so; `backup-inventory`
ghi checksum archive. Hash mỗi hàng DB dùng JSON của PostgreSQL → SHA-256;
hash bảng dùng chuỗi hash hàng theo khóa chính. Không chứa bản dump hoặc dữ liệu
chi tiết từng hàng trong Git. Các log AppTest ở local giữ thêm output đầy đủ.

## Tái lập và vị trí local

[Hướng dẫn và harness](../../../../scripts/repro/README.md).
Lượt chạy dùng harness đang được xây dựng; các bước backup ZIP, kiểm ZIP trong
container phục hồi và kiểm sensor UI được chạy bổ sung. Bản script cuối tích
hợp các bước đã thực thi đó. Source ứng dụng trong image giữ nguyên suốt lượt.
Hash các bản harness thực chạy được ghi trong summary; bản sao và ghi chú bổ
sung nằm ở `evidence/harness/` trong thư mục local:

`tmp/full-repro/20261106T182007Z/`

Thư mục này giữ clone, raw, backup và logs. Project nguồn thử
`gh-agc-20261106182240-7578` đã được dọn sau khi kiểm archive. Project đích
`gh-agc-20261106182240-7578-restored` được giữ **đã dừng**, với volumes còn nguyên.
App, DB và broker của project người dùng vẫn chạy, không bị thay thế hoặc dừng.

Giới hạn: sensor là cửa sổ 30 dòng lịch sử qua thiết bị giả lập, không phải
sensor vật lý hoặc replay toàn vụ. Các thành phần replay chạy bằng class
production trong processing harness, qua broker thật và hai process Spark trước/
sau phục hồi. Không suy rộng thành kiểm mọi lỗi broker, mất điện hay SLA. Rule,
scenario và diện tích giả định có nhãn; không phải khuyến nghị nông học/hóa đơn.
