# T43 — BR-02 ClickHouse

[Manifest](manifest.json) ghi scope, checksum và giới hạn. Source HEAD baseline
lúc chạy `7eb78c6`, implementation T43 đang phát triển; source ứng dụng ở các
image cuối được kiểm bằng fixture/UI suite và resume publication AGC.

- 27 tests ClickHouse/production AppTest pass; 100 UI tests PostgreSQL pass trên
  PostgreSQL project mới, đã dọn sau kiểm. Database người dùng không bị sửa.
- 996 Resources records, sáu cycles. Không nạp Climate/Production vào core nước.
- [AGC oracle](agc-check.json) và [đối soát engine](engine-parity.json): sáu khoang,
  cửa sổ 02–03/03/2020 và toàn vụ 166 ngày; quantities/ratio Decimal khớp chính xác.
  [PG reference](postgres-reference.json) đọc bằng transaction read-only.
- [Browser](browser-check.json) kiểm Chromium thật, chọn Automatoes, xác nhận
  code ClickHouse trên trang và tải CSV 166 ngày. CSV giữ local, hash trong manifest.
  [Screenshot](browser-water.png) thể hiện KPI toàn vụ và nhãn giả định diện tích.
- [Resume](resume.json) giữ nguyên publication/receipts. App egress bị chặn, mã101;
  localhost health `ok`. Docker không publish cổng trên internal-only network;
  relay TCP đích cố định mở localhost, app và warehouse giữ internal. Không tuyên
  bố relay bridge đã bị chặn mọi egress. Thử tắt masquerade trước đó không chặn
  app egress và đã được thay bằng relay; không dùng lượt thất bại làm bằng chứng pass.

Pipeline ban đầu chạy trong lúc phát triển; final image và network/relay/browser
được kiểm bổ sung trên dữ liệu đã tạo. Harness cuối tích hợp những bước đó, không
khẳng định toàn bộ script cuối đã chạy nguyên văn trong một lượt. Full local logs,
state và project tại `tmp/clickhouse-water/gh-ch-water-20261106194637-20076/`.

Full six-BR/sensor parity, performance, backup/restore và sáu video ClickHouse
thuộc các task sau. Đây là nghiệm thu riêng luồng nước, không thay nghiệm thu đó.
