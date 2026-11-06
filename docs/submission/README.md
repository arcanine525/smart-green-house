# Tài liệu nộp — bản dự thảo

- [Báo cáo PDF](report-draft.pdf) · [nguồn Markdown](report.md).
- [Slide PDF](slides-draft.pdf) · [nguồn Markdown](slides.md).
- [Kịch bản video 24 phút](video-script.md).
- Sơ đồ trong `figures/` có nguồn Mermaid `.mmd` và ảnh render `.png`.

Các file đang đánh dấu **dự thảo**: chưa xác nhận Group ID, tên thành viên,
phân công thực tế hoặc video thuyết trình 20–30 phút. Không
đổi tên thành gói nộp cuối và không công bố các mục chưa có bằng chứng là đạt.
Video giữ ngoài Git; nhóm cần hoàn thiện video thuyết trình, kiểm tra và lưu ở nơi nộp.
Bộ walkthrough T26 có phạm vi riêng: sáu luồng BR tự động, không giọng đọc; xem
[bằng chứng và cách mở video](../evaluation/usability.md). Đánh giá người thật
là mở rộng tùy chọn theo quyết định user ngày 2026-11-07.
[T28 full AGC/offline và restore](../evaluation/assets/full-local-repro/README.md) đã đạt trong phạm vi công bố.

Để xuất lại PDF cần cài Mermaid CLI (`mmdc` và Chromium), Pandoc và LibreOffice
(`soffice`) trên máy soạn tài liệu, sau đó chạy `./scripts/build_submission.sh`.
Đây là công cụ tạo artifact, không phải dependency runtime của ứng dụng local.
Các số trong tài liệu phải đối chiếu lại [correctness](../evaluation/correctness.md),
[performance](../evaluation/performance.md), [usability](../evaluation/usability.md)
và [sensor](../evaluation/sensor-replay.md) sau mỗi lượt đánh giá mới.

## Kiểm tra gói nộp

Sau khi xác nhận Group ID và tạo sản phẩm cuối, đặt vào một thư mục local:
`<GROUP_ID>_technical_report.pdf`, `<GROUP_ID>_slides.pdf` và
`<GROUP_ID>_video.mp4` hoặc `.webm`. Ứng dụng là repository/Compose này. Chạy:

```bash
./scripts/check_submission.sh GROUP_ID /duong/dan/den/goi-nop
```

Lệnh cần `pdfinfo`, `pdftotext`, `ffprobe` và Docker Compose, chỉ đọc file và
kiểm tra cấu hình. Nó kiểm PDF có text/không gắn nhãn draft, tên file, video có
cả hình/tiếng và dài 20–30 phút; cần xem thủ công nội dung, group ID, audio,
demo, link và quyền truy cập nơi nộp. Test ngày 2026-11-06 xác nhận gói draft
thiếu video bị từ chối; gói **giả lập** gồm PDF mẫu và video đen/im lặng 1.201
giây được chấp nhận về cấu trúc. Gói giả lập không phải sản phẩm của project.
