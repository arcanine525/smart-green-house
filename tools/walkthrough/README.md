# Quay walkthrough PostgreSQL và ClickHouse

Công cụ trên máy quay: Python ≥3.11, Node.js ≥20, ffmpeg và ffprobe.
Chuẩn bị dependency/browser theo lockfile khi có Internet, trước runtime local:

```bash
npm ci --prefix tools/walkthrough
tools/walkthrough/node_modules/.bin/playwright install chromium
```


## ClickHouse — walkthrough mới T51

Bộ ClickHouse được quay lại trên bản sao raw-rebuild đã qua toàn bộ kiểm tra
T50. Cần hoàn tất [`scripts/clickhouse_repro/README.md`](../../scripts/clickhouse_repro/README.md)
trước; `--setup` trỏ tới `current.json` của lần diễn tập đó. Database phải là
`greenhouse_repro`, project riêng `gh-ch-repro-*`; các lệnh từ chối project nguồn.
Đây là bản sao demo có ghi rule, replay và scenario, chỉ dùng cho walkthrough.

```bash
python3 scripts/prepare_clickhouse_walkthrough.py \
  --setup tmp/clickhouse-repro/current.json \
  --output tmp/clickhouse-walkthrough/<run-id>
python3 scripts/run_walkthrough_replay.py \
  --setup tmp/clickhouse-repro/current.json \
  --preparation tmp/clickhouse-walkthrough/<run-id> prepare
node tools/walkthrough/warm-ui.cjs tmp/clickhouse-walkthrough/<run-id>
APP_URL=http://127.0.0.1:18584 \
DEMO_CONTEXT=tmp/clickhouse-walkthrough/<run-id>/context.json \
EVIDENCE_DIR=project-submit/demo-videos/clickhouse \
BRS=01,02,03,04,05,06,07 PACE=1 node tools/walkthrough/record.cjs
node tools/walkthrough/build-index.cjs project-submit/demo-videos/clickhouse
python3 scripts/verify_clickhouse_browser_startup.py \
  --setup tmp/clickhouse-repro/current.json \
  --preparation tmp/clickhouse-walkthrough/<run-id>
node tools/walkthrough/check-index.cjs project-submit/demo-videos/clickhouse \
  tmp/clickhouse-walkthrough/<run-id>/player-verification.json
python3 tools/walkthrough/finalize_clickhouse.py \
  --preparation tmp/clickhouse-walkthrough/<run-id> \
  --videos project-submit/demo-videos/clickhouse \
  --output docs/evaluation/assets/clickhouse-walkthrough
```

Chạy tuần tự. Cold proof có thể mất vài phút; bước warm-up được ghi riêng,
không tính vào benchmark warm. Chuẩn bị cần ít nhất 3 GiB trống; raw rebuild
trước đó cần nhiều hơn theo harness. Chuẩn bị replay phát 10 measurement AGC
thật cùng manifest khai báo 30. Video sensor phát tiếp 20 event qua CLI thật,
MQTT, collector và Spark trên cùng checkpoint, rồi kiểm publication complete,
checksum, sequence và fingerprint. CLI chạy ngoài dashboard, có chú thích rõ.
Preparation sao chép parity đã đạt cho đúng publication, kiểm hai module
client/threshold trong image khớp source và server XML khớp bản hiện hành.
Nếu dùng rehearsal/image tạo trước bản sửa T51, cần rebuild image, nạp XML mới,
bảo toàn metric log cũ theo runbook rồi chạy lại parity; không đổi nhãn image
cũ thành bản đã sửa. Cold probe sau ghi hình chỉ restart app của bản demo.
Không chạy lệnh `complete` trước khi quay sensor vì sẽ mất chuyển trạng thái.
Nếu phải quay lại sau khi replay đã complete, dùng `--run-name live-replay-v2`
ở lệnh `prepare`. Wrapper tạo scope/outbox/checkpoint mới và ghi
`context-live-replay-v2.json`; dùng file đó cho `DEMO_CONTEXT` và `--context`
của finalizer. Giữ context và source recorder theo từng clip; các video lịch
sử chỉ được ghép khi publication/config/snapshot/oracle vẫn khớp.

`07` là video sensor bổ sung BR-01/03. Trang index ClickHouse bắt buộc đủ sáu
BR và sensor, `PACE=1`, cùng historical publication/config; context từng clip
được giữ/hash riêng. Script kiểm SHA video/download và
ảnh từng bước. Trình duyệt chặn HTTP ra ngoài origin local. App chỉ nối mạng
backend internal; relay localhost có frontend bridge. Preparation kiểm engine,
không có credential PostgreSQL và publication native. Oracle nước/năng lượng/
thu hoạch/chi phí được tính độc lập từ CSV đã kiểm SHA.

Theo yêu cầu user, bộ MP4 đã nghiệm thu, player, metadata, ảnh từng bước và
downloads tại `project-submit/demo-videos/clickhouse/` được lưu trong Git.
WebM gốc, trace, recorder snapshots và các lần quay thử giữ local; evidence
nghiệm thu lịch sử trong `docs/evaluation/` được giữ nguyên. Các lần quay lỗi phải giữ log
và không được tính là pass. Video không âm thanh, không có người tham gia;
video thuyết trình tổng thể được user tự quay offline. Finalizer kiểm toàn bộ
frame MP4, publication/config/snapshot trong JSON và trích frame từ video;
`main_visual_review` vẫn pending cho tới khi main thực sự xem ảnh. Không ghi
đè bộ evidence đã có; khi quay lại, dùng output khác.

## PostgreSQL — baseline T26

Smoke điều hướng backend ClickHouse (chín màn hình, không quay video):

```bash
PUBLICATION_ID=PUBLICATION_DA_NGHIEM_THU APP_URL=http://127.0.0.1:18582 \
  node tools/walkthrough/check-backends.cjs
```

Lệnh kiểm đúng publication hiển thị, export availability, quality details hoặc
empty state và replay empty state trên candidate AGC. Populated replay dùng
AppTest với bytes simulator thật trong suite T48. Sáu walkthrough video chi
tiết ClickHouse theo quy trình riêng ở trên; xem [UI/CLI](../../docs/implementation/clickhouse-ui-cli.md).

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

## Diễn tập trước video, không ghi hình

`rehearse_submission.cjs CONTEXT_JSON OUTPUT_DIR` dùng Playwright thật, chỉ đọc
nghiệp vụ: sáu BR, filter/rule, exports, mở lại scenario frozen có sẵn, overview,
quality và replay complete. Không tạo measurement/scenario mới, không sửaDOM/KPI
và không xuất video thuyết trình. Chọn output mới để giữ mọi log lỗi trước đó.
Context gồm `url` localhost, `publication` hiện hành, `replay`30events đã complete;
`scenario` tùy chọn có `name`, `publication`, `version` từ scenario DTO đã nghiệm thu.

```bash
node tools/walkthrough/rehearse_submission.cjs tmp/submission-rehearsal/context.json \
  tmp/submission-rehearsal/new-run
```

Đây là kịch bản dữ liệu AGC đã khóa: Irr10,36 sau correction diễn tập, drain3,89;
IUACAAS episode1180phút; Digilog19/02 shareB66,67%; energy308,078/A0→NULL.
Fresh raw deployment chưa có correction không được dùng kỳ vọng10,36; chọn
recorder gốc/raw oracle hoặc chuẩn bị đúng correction/context rõ ràng. Nếu mở
scenario gốc4b08 trên catalog hiện hành5d45, UI phải cảnh báo và giữ **frozen context**,
không ngầm áp quantity/publication mới. Timing automation không phải24phút nói.
