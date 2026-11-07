# Nguồn tài liệu Project cuối kỳ

- [Báo cáo PDF Group 29](../../project-submit/29_technical_report.pdf) · [Markdown](report.md).
- [Slides PDF Group 29](../../project-submit/29_slides.pdf) · [Markdown](slides.md).
- [Kịch bản 24 phút](video-script.md) · [metadata nhóm](group.json).
- [Chỉ mục bàn giao](../../project-submit/README.md): file editable, video và checklist.

Group ID **29**; ba tên/MSSV dùng placeholder theo yêu cầu user. Điền một nguồn
`group.json`, xác nhận vai/đóng góp thực tế rồi build lại PDFs. Sáu phần báo cáo
bám đề Project: bối cảnh/BR, dữ liệu, kỹ thuật/ứng dụng, alternative/phương pháp,
kết quả, kết luận/bàn giao. Số liệu phân biệt PG/CH, warm/cold và publication gốc/
correction có audit. Các bản PDF draft cũ đã được thay bằng đường dẫn gói hiện hành.

```bash
./scripts/build_submission.sh
```

Cần Mermaid CLI/Chromium, Pandoc, LibreOffice, Python3 và Poppler trên máy soạn
tài liệu; đây không phải dependency runtime app. Script xuất report A4, slide
PDF và DOCX/PPTX tại `project-submit/`; sơ đồ có nguồn Mermaid trong `figures/`.
Có thể sửa Markdown hoặc identity và tái xuất, rồi chạy kiểm tra gói để cập nhật
checksum. Không chỉnh PDF binary rồi bỏ qua source/manifest.

Video thuyết trình **20–30 phút do user quay offline**. Bộ bảy walkthrough CH
đã có, không có giọng trình bày và không thay video tổng thể. Giới hạn usability:
chưa có người tham gia. Không tạo video đen/placeholder để vượt kiểm tra. Gói
trước video và gói cuối đủ bốn sản phẩm là hai trạng thái riêng; hướng dẫn kiểm
ở [chỉ mục](../../project-submit/README.md). Agent không tự upload/nộp bài.

Bằng chứng: [ma trận yêu cầu](../implementation/requirements-matrix.md),
[correctness/benchmark/recovery](../evaluation/README.md),
[walkthrough native](../evaluation/assets/clickhouse-walkthrough/README.md).
