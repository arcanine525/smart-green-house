# Group 29 — Gói Project Data Engineering

Backend chính **ClickHouse**, chạy local. Trạng thái: chuẩn bị **trước video
thuyết trình tổng thể**, không phải gói đã đủ bốn sản phẩm để nộp.
Tên và MSSV ba thành viên là placeholder theo yêu cầu; nhóm sẽ điền sau.

| Sản phẩm | File / cách mở | Trạng thái |
|---|---|---|
| Báo cáo kỹ thuật | [29_technical_report.pdf](29_technical_report.pdf) | Đã xuất, còn điền tên/MSSV |
| Slide Project | [29_slides.pdf](29_slides.pdf) | Đã xuất, còn điền tên/MSSV |
| Bản editable | [DOCX](29_technical_report.docx), [PPTX](29_slides.pptx) | Nguồn Markdown tại docs/submission |
| Ứng dụng | Repository và gói source `29_application.zip` được tạo ở bước kiểm gói | Chạy theo README trong source; ZIP/images/dataset không thay cho nhau |
| Walkthrough | [Bảy video ClickHouse](demo-videos/clickhouse/index.html) | Local, gồm sáu BR và sensor; không commit video |
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
cold proof nhiều phút và chưa có user study. Video local sẽ không có trong clone
GitHub; chép thư mục video hoặc chạy lại recorder theo hướng dẫn khi bàn giao máy khác.
