# Scripts

- `test_sensor_acceptance.sh`: chạy parity, fault matrix và restart sensor trên
  broker/PostgreSQL test riêng, sau đó dọn container/anonymous volume do chính
  script tạo. Cần Docker, Compose, `.env`, processing image đã build và backend
  network của project. Evidence lưu tại `tmp/sensor-recovery/<run-id>`; xem
  [báo cáo sensor replay](../docs/evaluation/sensor-replay.md).

Các lệnh runtime khác dùng trực tiếp `docker compose`; không cần cài Python,
Java hoặc Spark trên host.
