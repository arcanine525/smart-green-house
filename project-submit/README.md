# Group 29 — Gói Project Data Engineering

Backend chính **ClickHouse**, chạy local. Trạng thái: **đã kiểm gói trước video và diễn tập T30 đạt**. Video thuyết trình
tổng thể còn chờ user; gói chưa đủ bốn sản phẩm để nộp.
Tên và MSSV ba thành viên là placeholder theo yêu cầu; nhóm sẽ điền sau.

| Sản phẩm | File / cách mở | Trạng thái |
|---|---|---|
| Báo cáo kỹ thuật | [29_technical_report.pdf](29_technical_report.pdf) | Đã xuất, còn điền tên/MSSV |
| Slide Project | [29_slides.pdf](29_slides.pdf) | Đã xuất, còn điền tên/MSSV |
| Bản editable | [DOCX](29_technical_report.docx), [PPTX](29_slides.pptx) | Nguồn Markdown tại docs/submission |
| Ứng dụng | Repository và gói source `29_application.zip` được tạo ở bước kiểm gói | Chạy theo README trong source; ZIP/images/dataset không thay cho nhau |
| Walkthrough | [Bảy video ClickHouse](demo-videos/clickhouse/index.html) | Đã lưu trong Git, gồm sáu BR và sensor |
| Video tổng thể | `29_video.mp4` hoặc `.webm` | **User tự quay offline**,20–30phút có audio |

[Kịch bản 24 phút](29_video_script.md) có phân vai A/B/C và highlights; không
phát hết cả bảy walkthrough27phút22,56s bên trong bài24phút.
[group.json](group.json) là copy metadata; chỉnh nguồn
[docs/submission/group.json](../docs/submission/group.json) rồi chạy:

```bash
./scripts/build_submission.sh
```

Phần A/B/C là gợi ý cần nhóm xác nhận đóng góp và phần trình bày thực tế.
Không ghi nhận tên hoặc đóng góp giả. Build lại report/slide và kiểm gói sau
khi điền danh tính để checksum không còn trỏ bản trước.

## Bối cảnh demo phải giữ đúng

Video native ghim publication gốc `4b08bd51c092465783304f887d766d47`:
Automatoes02–03/03/2020 có tưới9,36/drain3,89L/m²→41,56%. App hiện tại ghim
`5d455686a06649e789908b9708a9d436` sau correction diễn tập có audit+1Irr ngày02/03:
tưới10,36/drain3,89→37,55%. Raw và pin gốc không đổi. Năng lượng308,078kWh,
A=0→intensityNULL; tariff/basis giữ nhãn; điện+20% làm tổngcost+18,41%.
Catalog UI chọn current complete pin, không có selector cho mọi pin cũ. Dùng
video gốc hoặc API pin khi trình bày số gốc, không trộn với correction live.

[Cutover và audit](../docs/evaluation/clickhouse-cutover.md),
[walkthrough checksums](../docs/evaluation/assets/clickhouse-walkthrough/README.md).
Bản PostgreSQL trước đây ở `demo-videos/` là bằng chứng baseline riêng; chọn bộ
ClickHouse cho phần triển khai hiện hành. Không đổi nhãn video cũ thành CH.

## Hoàn tất offline và nộp

1. Điền tên/MSSV, xác nhận vai; build lại PDF và kiểm metadata.
2. Chuẩn bị dependency/images/ZIP theo [README](../readme.md), warm app trước ghi.
3. Quay theo script24phút, xuất `29_video.mp4`/`.webm`; có phần của ba người.
4. Xem/nghe toàn bộ, kiểm20–30phút, hình/chữ, audio và đúng version/số liệu.
5. Chạy checker gói cuối, ghi checksum và kiểm quyền truy cập nơi nộp. Kênh nộp
   chưa được chỉ định; agent không tự gửi/upload hoặc công bố gói đã nộp.

Nguồn ứng dụng chứa code/config/fixtures/locks và hướng dẫn; không kèm personal
`.env`, raw dataset, Docker images hoặc DB dumps. Chuẩn bị riêng ZIP AGC đúnghash
và images để runtime offline; report giải thích basis/timezone/rules chưa xác minh,
cold proof nhiều phút và chưa có user study. Bộ bảy video ClickHouse đã nghiệm thu và player/assets đi cùng được clone từ
GitHub; raw WebM/trace và các lần quay thử vẫn giữ local.

## Tạo và kiểm gói

Tại repository có đầy đủ bảy video và exports local:

```bash
python3 scripts/submission_package.py build 29 project-submit
./scripts/check_submission.sh 29 project-submit --pre-video
```

`29_application.zip` giữ local, không commit bản sao toàn source. File này có
manifest SHA-256 cho từng file và không chứa `.env` cá nhân, Git, dataset, dumps
hoặc video. [Delivery manifest](29_delivery_manifest.json) ghi checksum PDFs,
editable sources, sourceZIP và bảy video; checksum video phải khớp bộ đã nghiệm thu.
Base HEAD trong manifest là điểm xuất phát; **per-file hashes** xác định chính xác
source snapshot, bao gồm file ứng viên T30 được review trước commit. ZIP có thể
giải nén chạy local theo README; không cần lịch sử Git cho runtime ứng dụng.

Checker `--pre-video` xác nhận các phần đã có và ghi rõ video tổng thể/danh tính
còn chờ. Chế độ mặc định **phải từ chối** khi tên/MSSV còn placeholder hoặc thiếu
video tổng thể. Sau khi điền danh tính và quay video, build PDFs rồi tạo lại
manifest và chạy:

```bash
./scripts/build_submission.sh
python3 scripts/submission_package.py build 29 project-submit
./scripts/check_submission.sh 29 project-submit
```

Kiểm cấu trúc/checksum không thay thế xem/nghe đầy đủ, xác nhận đóng góp và quyền
truy cập nơi nộp. Khi clone GitHub, bộ `demo-videos/clickhouse/` đã nghiệm thu gồm MP4, index,
ảnh từng bước và downloads được tải theo Git.

[Bằng chứng diễn tập/kiểm gói](../docs/evaluation/assets/submission-package/README.md):
chín màn hình, sáu BR, bảy exports và sensor30events; scenario cũ giữ frozen pin.

Source ZIP loại riêng evidence kiểm gói và delivery manifest sinh ra để tránh
checksum tự tham chiếu. Bằng chứng bàn giao đầy đủ nằm trong repository và thư
mục `project-submit/` bên ngoài ZIP; các phần này không cần cho runtime ứng dụng.
