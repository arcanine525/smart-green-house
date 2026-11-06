# Scripts

- `rehearse_agc_batch.sh` → `rehearse_agc_restore.sh`: full snapshot AGC trên
  project cô lập, kiểm sáu BR và phục hồi DB/raw/sáu volume cùng replay AGC thật.
  Chuẩn bị image trước; xem [hướng dẫn](repro/README.md).

- `prepare_walkthrough.sh`: clone một database AGC đã chuẩn bị sang database
  demo riêng và khởi động dashboard để quay T26. Xem
  [Playwright walkthrough](../tools/walkthrough/README.md) cho lệnh quay ≥6 video,
  chú thích, assertion và trang review theo từng bước. Video giữ ngoài Git.

- `test_offline_smoke.sh`: chạy batch Spark, UI và sensor synthetic trên project
  và network internal riêng, không pull image; cần chuẩn bị image trước.
- `record_usability.py`: ghi phiên quan sát thật từng BR vào JSON local;
  chuẩn bị câu hỏi, đáp án và context theo [protocol](../docs/evaluation/usability.md).
- `check_submission.sh`: kiểm cấu trúc PDF/video và Compose theo Group ID;
  không thay thế xem thủ công nội dung hoặc kiểm tra quyền truy cập.
- `test_sensor_backup_restore.sh`: diễn tập restore synthetic với SQLite WAL/SHM,
  PostgreSQL và Spark checkpoint thật trên hai database thử nghiệm.
- `test_backup_restore.sh`: kiểm tra dump/restore PostgreSQL và sao chép raw/volume
  synthetic trên project Docker tạm; không thay thế diễn tập phục hồi AGC thật.
- `test_effectiveness.sh`: chạy sáu walkthrough UI trên fixture với JUnit XML;
  thời gian đo là thời gian test, không phải thời gian người dùng.
- `test_local.sh`: build image, chạy toàn bộ unit/contract/recovery, UI/export
  và sensor acceptance tuần tự; log ở `tmp/local-tests/<run-id>`. Cần `.env`
  và các image/dependency đã chuẩn bị; không reset hay xóa dữ liệu project.
- `test_sensor_acceptance.sh`: chạy parity, fault matrix và restart sensor trên
  broker/PostgreSQL test riêng, sau đó dọn container/anonymous volume do chính
  script tạo. Cần Docker, Compose, `.env`, processing image đã build và backend
  network của project. Evidence lưu tại `tmp/sensor-recovery/<run-id>`; xem
  [báo cáo sensor replay](../docs/evaluation/sensor-replay.md).

Các lệnh runtime khác dùng trực tiếp `docker compose`; không cần cài Python,
Java hoặc Spark trên host.
