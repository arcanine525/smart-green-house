# Walkthrough ClickHouse đã nghiệm thu

Ngày 2026-11-07: bảy video, 109 bước, 21 file xuất, tổng 1642.56 giây
(~27 phút 23 giây). Sáu BR có tổng 93 bước/19 file xuất và 1.271,88 giây.
Video sensor bổ sung BR-01/03, không tạo BR thứ bảy.

| Video | Bước | File xuất | Giây | Kết quả |
|---|---:|---:|---:|---|
| BR-01 | 16 | 3 | 218,64 | Pass |
| BR-02 | 14 | 3 | 165,20 | Pass |
| BR-03 | 15 | 3 | 193,44 | Pass |
| BR-04 | 13 | 4 | 150,16 | Pass |
| BR-05 | 15 | 3 | 205,24 | Pass |
| BR-06 | 20 | 3 | 339,20 | Pass |
| Sensor (BR-01/03) | 16 | 2 | 370,68 | Pass |

## Xem trên máy có artifact

Từ root repository:

    open project-submit/demo-videos/clickhouse/index.html

MP4 1920×1080 H.264, không audio, PACE=1; có nút nhảy tới từng bước.
MP4, WebM gốc, trace, screenshots từng bước và downloads ở thư mục local trên,
không commit Git. Các lần quay trước/lỗi được giữ riêng trong previous-candidate
và failed; chỉ bảy clip trong summary hiện hành được tính đạt.

## Kiểm chứng

- [Summary và SHA từng clip](summary.json), [giải mã và visual review](verification.json),
  [bảy player và chapter](player-verification.json).
- [Native JSON vs oracle raw](native-export-oracle.json): chín đối chiếu số, giữ
  tolerance khóa trước triển khai; finite money exact, chia lặp absolute 1e-24.
  Zero A cho NULL; hai giá điện +20%, giá nhiệt giữ nguyên.
- [Full AGC parity sau sửa](fixed-full-parity.json): sáu BR, 96 CSV, 30 lineage,
  18 nhóm threshold và 67.829 episode. [Suite/gates](fixed-verification.json).
- [Cold app/browser cuối](cold-browser-startup/result.json): 356,497 giây.
  [Root cause và regression](cold-fix/root-cause.json), [metrics config](metric-log-runtime-fix.json),
  [log parts được giữ](metric-log-preservation.json).
- [Context](context.json), [runtime/image/source SHA](runtime.json),
  [partial10](live-partial.json), [complete30](live-complete.json), [manifest](manifest.json).

Historical publication 4b08bd51c092465783304f887d766d47; sensor có scope/publication
riêng. Provisional10 → complete30 qua publisher/MQTT/collector/Spark thật trên
cùng checkpoint. SHA nguồn, original envelopes, checksum/sequence và fingerprint
khớp. Completed phản ánh đủ dữ liệu; Stale phản ánh tuổi nhận theo UTC sau khi
collector của replay hữu hạn đã dừng. Hai trạng thái có thể cùng xuất hiện.

Sources lưu mã tại lượt kiểm chứng; helper-revisions giữ các bản trước theo SHA.
Code gốc của image dựa trên parent commit 3aef71a và hai module sửa có SHA
trong runtime/context, server XML có SHA riêng. Không gắn commit parent như thể
không có working-tree changes. Helper trong repo có thêm guard để lặp lại với
image/server config hiện hành; không đổi nhãn artifact cũ.

Đây là walkthrough tự động, không có người tham gia và không đo usability người
thật. T53 là video thuyết trình 20–30 phút do user tự thực hiện offline. T39
cutover/rollback và gói bàn giao vẫn có gate riêng.
