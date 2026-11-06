---
title: "Smart Greenhouse"
subtitle: "Data Engineering · Project cuối kỳ · bản dự thảo"
date: "06/11/2026"
lang: vi-VN
---

## 1. Bối cảnh và người dùng

- Nhà kính có nhiều nguồn, grain và độ tin cậy khác nhau.
- Người vận hành: khí hậu, tưới, drain; kỹ sư: EC/pH và episode.
- Quản lý: thu hoạch, năng lượng, chi phí và kịch bản.
- Mục tiêu: phân tích có nguồn, version, quality và giới hạn rõ ràng.

## 2. Sáu yêu cầu nghiệp vụ

- BR-01–03: đợt lệch khí hậu, tỷ lệ drain, đợt EC/pH.
- BR-04–06: kWh/kg loại A, cơ cấu thu hoạch, kịch bản chi phí.
- Mỗi BR có công thức, grain, điều kiện công bố và AC.
- Không biến telemetry replay thành dữ liệu lịch sử mới.

## 3. Dataset AGC

- Autonomous Greenhouse Challenge 2nd edition 2019–2020.
- 45 file, 622.899 dòng CSV; manifest và checksum bất biến.
- Climate: slot 5 phút; Resources: ngày; Production: record thu hoạch.
- Nguồn thời gian chưa xác minh timezone; NULL ≠ 0.

## 4. Thiết kế local

![](figures/architecture-slide.png)

## 5. Công nghệ sử dụng

- PostgreSQL: transactions, quality, revision, metadata và marts.
- PySpark batch / Structured Streaming; MQTT Mosquitto và collector lưu bền.
- Streamlit + Plotly: sáu BR, tổng quan, quality, replay và CSV/JSON.
- Docker Compose local; ClickHouse là phương án tương lai.

## 6. Từ dữ liệu đến KPI

- Water: tổng paired drain / tổng paired irrigation; mẫu 0 → NULL.
- Harvest: B / (A+B) trên cùng record; quarantine ảnh hưởng completeness.
- Energy: tổng kWh / tổng kg A, tổng hợp mỗi nguồn trước khi ghép.
- Cost: lượng × giá theo ngày/component; khóa overlap/gap.

## 7. Chất lượng và lineage

- Quality theo measure: accepted / missing / invalid / suspect.
- Phiên bản publication, policy, basis, rule và scenario được giữ rõ.
- Quarantine Reference ngoài vụ: không tự sửa năm.
- Diện tích 62,5 m² `assumed_unverified`; sensitivity 96/62,5 = 1,536.

## 8. Phương án so sánh

- Hybrid: raw → staging → core revision → marts; giữ lineage và checkpoint.
- Kimball gọn: raw → staging → dimensional facts; ít lớp vật lý hơn.
- Cùng snapshot, engine, cleaning, rules và sáu workload; đã kiểm parity.
- Kimball chưa chứng minh recovery tương đương.

## 9. Demo: khí hậu và nước

- Chọn publication, khoang, khoảng ngày, rule và basis.
- Tìm đợt nhiệt độ ngoài điều kiện; mở nguồn và quality.
- AGC Automatoes, 02–03/03/2020: tưới 9,36 / drain 3,89 L/m² → 41,56%.
- Publication df3e4bcb…; coverage paired 100%; ngày thiếu vẫn là NULL.
- Xuất CSV cùng publication và bộ lọc.

## 10. Demo: EC/pH và thu hoạch

- Lọc đợt EC/pH, xem suspect và bối cảnh tưới theo ngày.
- Xem sản lượng A/B, record/tuần và lineage; phân biệt incomplete.
- Ngưỡng hiển thị là demo khi chưa có evidence vận hành.

## 11. Demo: năng lượng và scenario

- Cùng Automatoes 02–03/03: 308,078 kWh; kg A = 0 → kWh/kg là NULL.
- Baseline 17,60 EUR theo biểu giá lịch sử và diện tích giả định 62,5 m².
- Kịch bản điện +20% giữ nguyên lượng; không trình bày là hóa đơn thật.

## 12. Demo: sensor replay

- Replay Climate qua simulator → MQTT → collector → Spark → PostgreSQL.
- UI chọn scope replay riêng, publication pin, freshness và quality.
- Fault/late, checkpoint và resume: giải thích bằng trace, không cộng lịch sử.

## 13. Correctness và recovery

- Một lượt local: **419 backend + 97 UI + 19 sensor/recovery** pass.
- Sáu BR có raw/fixture oracle, lineage và version trong báo cáo correctness.
- Kiểm duplicate/conflict, correction, publication pin và restart.
- Phạm vi test: phần mềm và snapshot/fixture được nêu, không khẳng định nông học.

## 14. Performance: cùng snapshot, cùng PostgreSQL

- Fresh build (một lượt): Hybrid 880,33 s; Kimball 40,35 s.
- Database: Hybrid 3,319 GiB; Kimball 0,623 GiB (không gồm WAL/raw).
- BR-01 p95 gốc: 4.060,81 / 3.028,13 ms; cả hai vượt 2.000 ms.
- Follow-up Hybrid sau tối ưu: 1.770,93 ms; một lượt riêng, không phải SLA.
- BR-03 p95: 1.959,90 / 1.816,71 ms.
- Query Automatoes: 30 mẫu + 5 warm-up.

## 15. Effectiveness và giới hạn

- Sáu walkthrough tự động pass trên fixture; thời gian test không phải thời gian người dùng.
- **Chưa có người tham gia**, chưa có phản hồi hay điểm giải thích coverage.
- Basis chưa xác minh; Reference incomplete; rule/scenario có nhãn.
- Restore WAL, checkpoint và một retained message broker synthetic; chưa diễn tập full AGC, broker và outputs.

## 16. Kết luận và hướng tiếp theo

- Sáu BR và replay local có bằng chứng correctness, performance và UI tự động.
- Hoàn thành đánh giá người dùng, diễn tập offline và video 20–30 phút.
- BR-01 gốc vượt p95; follow-up sau tối ưu đạt trong một lượt, chưa phải SLA.
- ClickHouse chỉ xét sau khi có backfill/parity và bằng chứng phù hợp.
