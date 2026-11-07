# Cutover / rollback evidence

Final status: **passed**, xem [summary.json](summary.json) và
[manifest.json](manifest.json). Giữ engine/publication/context cụ thể cho mỗi
gate; các log trước sửa được giữ cùng phạm vi lỗi của chúng.

- Full six-BR corrected parity, 18 groups/67.829 episodes: `final-six-br-parity.json`.
- All 315 config versions, 7 replay pins với toàn bộ metadata, new correction:
  `rollback-inputs.json`.
- All 178 scenario versions, one explicit collision namespace, immutable old PG
  states và retries: `rollback-verification.json`.
- Actual native nine-screen, corrected PG nine-screen và final native water
  smoke: các thư mục browser có `result.json` và PNG.
- `stages/` và `helpers/`: exact executed helper maps, deduplicated by SHA.
- `source-artifacts.json`: source SHA và artifact SHA; log chỉ normalize newline
  và trailing whitespace. Không đổi số liệu hoặc bỏ lỗi khỏi log.

Dumps, raw, full native archive và journal SQL giữ local tại cutover root trong
`tmp/`; summary có size/checksum. Không có video, credential hoặc dump trong
folder này. Video walkthrough native T51 có [evidence riêng](../clickhouse-walkthrough/README.md).

Initial native nine-screen smoke được ghi trước migration mapping PG bổ sung;
Python business modules giữ nguyên. Final package còn được kiểm native CLI/KPI
sau return. Các helper sửa trong quá trình gate không được gán ngược làm source
của lần chạy cũ; stage snapshots khóa những revision đã ghi nhận.
