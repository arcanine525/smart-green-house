# Bằng chứng walkthrough Playwright — T26

Phạm vi được chốt ngày 2026-11-07: sáu video thao tác trên UI thật, có chú thích
tiếng Việt, số bước và điểm thao tác; MP4 1920×1080, không âm thanh. Chỉ các bản
quay `PACE=1` được đưa vào bộ này; các lượt probe không phải sản phẩm bàn giao.

## Mở video trên máy đã quay

Từ root repository:

```bash
open tmp/walkthrough/20261106T173153Z-9455/videos/index.html
```

Trang có sáu player, download MP4 và nút chuyển đến từng bước. Video, trace,
ảnh toàn bộ bước và file tải từ dashboard nằm trong thư mục trên, không đưa vào
Git. Khi chuyển máy cần sao chép thư mục `videos/` cùng các thư mục BR bên trong;
clone Git chỉ có code, metadata và ảnh minh chứng được chọn.

## Đọc bằng chứng

- `summary.json`: danh mục sáu video, thời lượng, số bước, chương, checksum MP4
  và download. `artifact_root` tính từ root repository trên máy đã quay.
- `br-XX.json`: toàn bộ bước, chú thích, thời gian, metrics và danh mục file.
  Đường dẫn screenshot/download/video bên trong tính từ `artifact_root/br-XX/`.
- `br-XX-frame.png`: một khung hình giải mã từ MP4 chính thức, được xem để kiểm
  độ rõ của chú thích, số liệu và nội dung. Không dùng ảnh từ lượt probe.
- `context.json`: bản sao database, publication/snapshot, rule nước demo bổ sung
  và oracle chi phí tính bằng Decimal từ hai dòng raw đã kiểm checksum.
- `verification.json`: kết quả ffprobe, giải mã toàn bộ MP4 và thời điểm lấy ảnh.
- `recorder-revisions/`: bản recorder đúng SHA trong từng kết quả. BR01–02,
  BR03–05 và BR06 được quay lần lượt khi bổ sung thao tác BR06; các bản cũ giữ
  để kiểm toán, runner được hỗ trợ hiện tại nằm trong `tools/walkthrough/`.

Ứng dụng chạy source commit `ae9303d` (full SHA trong từng kết quả), image
`sha256:3f211bd9912e10dae27166ebb903115d6f3080d26e559b384f473a2842ebc79e`.
Database nguồn `greenhouse_hybrid_bench_t25_20261106_104814` được clone sang
`test_walkthrough_20261106T173153Z_9455`; container demo
`greenhouse-walkthrough-20261106T173153Z-9455` chạy ở cổng `18572`.
Container demo đã dừng sau khi quay. Có thể mở lại bằng
`docker start greenhouse-walkthrough-20261106T173153Z-9455` rồi truy cập
`http://127.0.0.1:18572`; xem video không cần chạy container.
BR06 lưu scenario qua UI chỉ trong bản sao này. Các scenario từ lượt thử cũng
có trong bản sao; video đặt tên riêng và mở lại đúng scenario vừa tạo.

## Thao tác và đáp án được kiểm tra

Mọi video chọn publication/khoang/vụ, đổi ngày, đọc KPI, xem chi tiết/lineage và
tải CSV/JSON. JSON phải chứa đúng publication đã khóa; file tải và MP4 có SHA-256.

| BR | Thao tác riêng và đáp án |
|---|---|
| 01 | IUACAAS; rule khí hậu demo; lọc temperature; đợt dài nhất 1.180 phút; tương tác chart; đổi realized/requested; giữ đợt sau lọc 21–22/05/2020. |
| 02 | Automatoes; chart và bảng tuần; 02–03/03/2020 có tưới 9,36, drain 3,89 L/m², tỷ lệ paired 41,56%; chọn rule nước và ngày chi tiết. |
| 03 | Reference; profile EC/pH demo; đợt EC 80.400 phút; chuyển pH/EC; chart; chọn ngày và slot nguồn, xác minh raw. |
| 04 | Automatoes; portfolio chart và công thức; 02–03/03/2020 có 308,078 kWh, 0 kg A và tỷ số NULL; chọn revision và xác minh raw. |
| 05 | Digilog; chart, rule B demo; 19/02/2020 có A 0,05, B 0,10 kg/m², share B 66,67%; đổi grain record/day/week; mở record và lineage. |
| 06 | Automatoes; giá hiệu lực, lượng × giá; đổi ngày/tuần; lineage hash hợp lệ; tăng hai giá điện 20%, giữ giá nhiệt; baseline 17,60 → 20,84 EUR; lưu, bỏ chọn, mở lại và xuất scenario. Bảng đóng góp hiện có thay cho chart. |

Rule, basis diện tích 62,5 m² và scenario đều có nhãn/giới hạn được trình bày
trong video. Các câu trả lời này chỉ áp dụng cho publication và cửa sổ đã pin.
Đây không phải kiểm thử toàn bộ tổ hợp UI hay xác nhận ngưỡng nông học. Không có
người tham gia; thời lượng video gồm chờ hệ thống và khoảng dừng đọc chú thích,
không phải thời gian sử dụng của con người.

Hướng dẫn [chuẩn bị và quay lại](../../../../tools/walkthrough/README.md).
