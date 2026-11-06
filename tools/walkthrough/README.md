# Quay walkthrough T26

Smoke điều hướng backend ClickHouse (chín màn hình, không quay video):

```bash
PUBLICATION_ID=PUBLICATION_DA_NGHIEM_THU APP_URL=http://127.0.0.1:18582 \
  node tools/walkthrough/check-backends.cjs
```

Lệnh kiểm đúng publication hiển thị, export availability, quality details hoặc
empty state và replay empty state trên candidate AGC. Populated replay dùng
AppTest với bytes simulator thật trong suite T48. Sáu walkthrough video chi
tiết ClickHouse vẫn là T51; xem [UI/CLI](../../docs/implementation/clickhouse-ui-cli.md).

Phạm vi user chốt ngày 2026-11-07: ≥6 video, mỗi BR có luồng đầy đủ; chú thích
Tiếng Việt, số bước, vị trí thao tác, không âm thanh; MP4 1920×1080. Các video
là thao tác tự động Playwright trên trình duyệt thật. Không có người tham gia,
không tính thời gian automation như thời gian thao tác của con người.

## Chuẩn bị

Runtime ứng dụng chạy Docker local. Công cụ tạo video cần Node.js ≥20, ffmpeg và
ffprobe trên máy quay. Cài dependency theo lockfile và browser một lần (cần
Internet ở bước chuẩn bị):

```bash
npm ci --prefix tools/walkthrough
tools/walkthrough/node_modules/.bin/playwright install chromium
./scripts/prepare_walkthrough.sh greenhouse_hybrid_bench_t25_20261106_104814
```

Database đầu vào phải có publication `df3e4bcbacab45a5b86fc37d2f53c00a`, raw AGC,
profile khí hậu/drain/thu hoạch và baseline như phiếu facilitator. Đây là dữ liệu
chuẩn bị có sẵn của project, không phải lệnh dựng từ database trống. Script clone
sang database `test_walkthrough_*`; PostgreSQL từ chối nếu template đang bận.
Không tự đóng kết nối của người dùng. Nếu clone thành công nhưng bước sau lỗi,
đường dẫn database/container được lưu trong thư mục evidence để xử lý tiếp.

Môi trường demo bổ sung rule nước `walkthrough-water:Automatoes:v1` với dải
20–40% drain và 0–20 L/m² tưới; đây là ngưỡng synthetic minh họa, có nhãn demo,
không phải khuyến cáo nông học. Chỉ BR06 ghi scenario qua UI và chỉ trong bản sao.

## Quay

Thay `<run-id>` bằng đường dẫn in từ script chuẩn bị:

```bash
APP_URL=http://127.0.0.1:18572 \
DEMO_CONTEXT=tmp/walkthrough/<run-id>/context.json \
EVIDENCE_DIR=tmp/walkthrough/<run-id>/videos \
node tools/walkthrough/record.cjs
```

Có thể chọn `BRS=01,03` để quay một phần vào thư mục output **mới**. Mặc định
`PACE=1`; giá trị nhỏ chỉ dành cho debug, không dùng làm video bàn giao.

Mỗi thư mục BR có MP4, WebM gốc, trace Playwright, ảnh theo bước, download,
`steps.json` và `result.json` (assertion, publication, thời lượng, SHA-256).
Video chỉ được đánh dấu đạt sau khi đóng browser context, chuyển MP4 và kiểm
kích thước bằng ffprobe. Fail giữ trace/ảnh lỗi, không có `result.json` pass.
Đọc [cơ chế recording của Playwright](https://playwright.dev/docs/videos).

Chú thích và vòng tròn trỏ chuột chỉ thêm lớp trình bày vào bản quay; các nút,
form, lựa chọn và download sử dụng hành động Playwright qua UI. Script không
thay số liệu hoặc giả lập server response. Đáp án có trước lấy từ phiếu
`docs/evaluation/usability-facilitator.md`; file export được kiểm publication.

Các MP4/WebM và trace/data thu thập nằm trong `tmp/`, giữ ngoài Git. Khi commit
bằng chứng, chỉ chép metadata/kết quả/ảnh đã xem xét vào `docs/evaluation/`.
Sau quay, dừng container có tên chính xác được in bởi bước chuẩn bị; bản sao DB
và video được giữ để mở lại kiểm tra. Không chạy cleanup rộng hay xóa DB nguồn.

## Trang xem video và kiểm tra artifact

Sau khi đủ sáu BR thành công:

```bash
node tools/walkthrough/build-index.cjs tmp/walkthrough/<run-id>/videos
open tmp/walkthrough/<run-id>/videos/index.html
```

Trang local có sáu player và nút chuyển tới từng bước. Script dựng index kiểm
đủ sáu kết quả pass, checksum MP4/download và ảnh theo bước; không gộp các lượt
probe vào bản bàn giao. Thời gian video bao gồm chờ render, thao tác và khoảng
dừng để đọc. BR06 hiện có bảng đóng góp thay cho biểu đồ Plotly; video dùng
đúng bảng của ứng dụng, các BR còn lại có tương tác biểu đồ.

Lượt T26 đã ghi có metadata tại
[`docs/evaluation/assets/playwright-t26`](../../docs/evaluation/assets/playwright-t26/README.md).
Các revision recorder dùng trong lượt đó được giữ để đối chiếu SHA; runner
hiện hành vẫn là `tools/walkthrough/record.cjs` cùng lockfile tại đây.

Candidate ClickHouse có smoke browser riêng cho water/harvest, chưa phải bộ video
T51. Khi app candidate đã chạy qua ch-web và có publication tương ứng:

```bash
APP_URL=http://127.0.0.1:18582 node tools/walkthrough/check-water.cjs
APP_URL=http://127.0.0.1:18582 node tools/walkthrough/check-harvest.cjs
```

Helper harvest kiểm Digilog toàn vụ, tải records CSV thật và xem Reference với
nhãn snapshot chưa hoàn chỉnh. Artifact ở tmp, không tự tạo video thuyết trình.
