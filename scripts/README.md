# Scripts

- `record_usability.py`: ghi phiên quan sát thật từng BR vào JSON local;
  chuẩn bị câu hỏi, đáp án và context theo [protocol](../docs/evaluation/usability.md).
- `check_submission.sh`: kiểm cấu trúc PDF/video và Compose theo Group ID;
  không thay thế xem thủ công nội dung hoặc kiểm tra quyền truy cập.
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
