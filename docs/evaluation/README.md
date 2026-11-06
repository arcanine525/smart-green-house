# Bằng chứng đánh giá

Mỗi báo cáo ghi phạm vi, dữ liệu, điều kiện chạy và giới hạn của kết luận. Scaffold hoặc synthetic stack tests chỉ chứng minh môi trường hoạt động; không thay thế nghiệm thu nghiệp vụ. Xem thêm [ma trận yêu cầu](../implementation/requirements-matrix.md).

| BR | Báo cáo | Phạm vi |
|---|---|---|
| BR-01 | [Khí hậu](br_01.md) | Nhiệt độ/CO₂, coverage, episode và rule basis |
| BR-02 | [Nước](br_02.md) | Irrigation/drain, cân bằng và đối soát nguồn |
| BR-03 | [Drain](br_03.md) | EC/pH, episode và chất lượng dữ liệu |
| BR-04 | [Năng lượng](br_04.md) | Quy đổi năng lượng và kWh/kg |
| BR-05 | [Thu hoạch](br_05.md) | Sản lượng, tỷ lệ loại B và completeness |
| BR-06 | [Chi phí](br_06.md) | Baseline tariff và scenario |

## Kiến trúc và hiệu năng

- [Kịch bản khai thác](usability.md): sáu walkthrough tự động và protocol đánh giá người dùng còn cần thực hiện.
- [Correctness và phục hồi](correctness.md): lệnh test local, oracle từng BR, publication pin và fault/recovery.

- [Nghiệm thu sensor replay](sensor-replay.md): parity, fault matrix, recovery và độ trễ.
- [UI sensor replay](sensor-replay-ui.md): scope, quality, trạng thái và CSV.
- [Structured Streaming replay](sensor-streaming.md): checkpoint, publication, quality và kiểm tra cục bộ.
- [Kiến trúc Kimball thay thế](kimball.md): schema, ETL, adapter và parity với Hybrid.
- [Benchmark Hybrid/Kimball](performance.md): pipeline mới/rerun/rebuild, storage, workload sáu BR và COPY so với INSERT; kèm số đo thô và lệnh tái lập.

Benchmark trên snapshot lịch sử không thay thế đánh giá usability hoặc nghiệm thu đường sensor replay. Basis diện tích và nước vẫn phải được đọc theo nhãn `assumed_unverified` trong từng báo cáo. Bằng chứng dựng môi trường nằm trong [hướng dẫn local](../implementation/local-environment.md).
