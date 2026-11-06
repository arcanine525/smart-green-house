# Structured Streaming cho sensor replay

Tài liệu này mô tả phần xử lý stream và phạm vi đã kiểm tra. Nghiệm thu đường
sensor đầy đủ, fault matrix và đo độ trễ published-to-queryable vẫn thuộc T36.

## Luồng và danh tính

Collector đóng các file `batch-*.jsonl` bất biến sau khi lưu spool. Stream đọc
file text bằng Spark Structured Streaming, giới hạn số file/rows trong batch,
và lưu checkpoint riêng. Checkpoint gắn với stream ID, generation, source và
PostgreSQL endpoint; không chứa mật khẩu. Lần chạy mới với checkpoint khác
cần generation riêng.

`foreachBatch` ghi các delivery, event variants, batch marker, membership và
publication pointer trong cùng transaction PostgreSQL. Batch ID chạy lại cùng
nội dung là no-op; cùng ID với nội dung khác bị từ chối. Identity của event
được tách theo replay; cùng identity với payload khác được giữ làm conflict,
không chọn bản đến trước làm kết quả. File raw-ready cũ không có byte gốc
base64 được quarantine, không tự dựng lại bytes từ JSON đã parse.

Publication được seal và pin membership bất biến. Khi event đến trễ hoặc xuất
hiện conflict, publication mới phản ánh trạng thái mới, publication cũ vẫn đọc
được. Event sai scope, nguồn, thời gian hoặc trùng slot không được đưa vào
membership dùng cho KPI; evidence vẫn nằm trong delivery/event variants.

Cursor tiến theo control sequence liên tục, không lấy maximum timestamp của
telemetry. Manifest không tự đóng replay: cần cursor completed, đủ sequence,
checksum payload theo thứ tự outbox, không có conflict/quarantine chưa xử lý,
và không có slot/scope/time conflict. Trường hợp chưa đủ bằng chứng được giữ
provisional. Event-time AGC được đưa về cùng grid 5 phút với parser batch;
received time được giữ riêng cho liveness.

## BR-01 và BR-03

`replay_evaluation.evaluate_publication` đọc đúng membership đã pin, chuyển
về các slot contracts hiện có và dùng chung quality policy, hàm dựng slot và
rule-duration engine với batch. Mỗi lần đánh giá tính lại cycle đến cursor,
không dùng phần tương lai để tính expected coverage. Rule revision phải được
chọn tường minh; `map_historical_rule` chỉ ánh xạ rule khi snapshot và khoang
nguồn khớp, và ghi rõ nguồn mapping trong evidence.

Một late event có thể nối/ngắt episode. Cờ provisional của evaluation áp dụng
cho toàn bộ kết quả khi replay chưa hoàn tất; chi tiết hiển thị episode mở và
live view hiển thị riêng theo publication replay. Không coi historical event time là thời điểm sensor thật.

## Kiểm tra đã thực hiện

- Test PostgreSQL riêng: retry cùng batch, reset generation, conflict, rollback
  sau lỗi publication, publication bất biến và pointer sau late conflict.
- Test Spark: đọc file giới hạn theo batch, restart sau commit DB trước
  checkpoint, simulator → collector → Spark → PostgreSQL và đối soát checksum.
- Test quality/episode: dùng chung quality với batch; event trễ nối hai episode;
  EC âm/pH zero và setpoint off-mode giữ đúng verdict; cursor và Excel serial
  có sai số microsecond được chuẩn hóa đúng slot.
- Chạy test dưới giới hạn 1 CPU/1,5 GiB cho processing và PostgreSQL local.
  Đây là test chức năng, chưa phải kết quả throughput hay latency T36.

Lệnh chạy và cấu hình scope xem [hướng dẫn sensor](../../src/greenhouse/sensor/README.md).
