# Tài liệu nộp — bản dự thảo

- [Báo cáo PDF](report-draft.pdf) · [nguồn Markdown](report.md).
- [Slide PDF](slides-draft.pdf) · [nguồn Markdown](slides.md).
- [Kịch bản video 24 phút](video-script.md).
- Sơ đồ trong `figures/` có nguồn Mermaid `.mmd` và ảnh render `.png`.

Các file đang đánh dấu **dự thảo**: chưa xác nhận Group ID, tên thành viên,
phân công thực tế, phản hồi người dùng, toàn bộ diễn tập offline hoặc video. Không
đổi tên thành gói nộp cuối và không công bố các mục chưa có bằng chứng là đạt.
Video giữ ngoài Git; nhóm cần tự ghi, kiểm tra và lưu ở nơi nộp.

Để xuất lại PDF cần cài Mermaid CLI (`mmdc` và Chromium), Pandoc và LibreOffice
(`soffice`) trên máy soạn tài liệu, sau đó chạy `./scripts/build_submission.sh`.
Đây là công cụ tạo artifact, không phải dependency runtime của ứng dụng local.
Các số trong tài liệu phải đối chiếu lại [correctness](../evaluation/correctness.md),
[performance](../evaluation/performance.md), [usability](../evaluation/usability.md)
và [sensor](../evaluation/sensor-replay.md) sau mỗi lượt đánh giá mới.
