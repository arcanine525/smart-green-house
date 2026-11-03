# BR-01 — Theo dõi các đợt lệch điều kiện khí hậu

| Thuộc tính | Giá trị |
|---|---|
| Project | Group 29 — Smart Greenhouse, Data Engineering CO5173 |
| Phiên bản | 1.0 — 2026-11-03 |
| Trạng thái | Đặc tả đề xuất cho project cuối kỳ; chưa phải kết quả đã triển khai/nghiệm thu |
| Người dùng chính | Người vận hành nhà kính |
| Người dùng liên quan | Kỹ sư canh tác, quản lý trang trại |
| Ưu tiên | Bắt buộc trong bộ sáu BR |
| Kế thừa | Chỉnh BR-01 của presentation: bổ sung sự kiện liên tiếp và độ phủ dữ liệu |

## 1. Mục tiêu và yêu cầu nghiệp vụ

> Người vận hành cần xác định khoang và khoảng thời gian nhiệt độ hoặc CO₂ lệch khỏi điều kiện vận hành đã chọn, biết độ lệch và thời lượng, để ưu tiên kiểm tra lại cấu hình điều khiển.

Hệ thống phải trả lời: khoang nào có đợt lệch dài nhất, đợt đó xảy ra khi nào, biến nào lệch, và dữ liệu có đủ để đưa ra nhận xét hay không. Giá trị nghiệp vụ là rút ngắn việc tìm giai đoạn cần kiểm tra; không tuyên bố điều kiện lệch đã gây thiệt hại cho cây.

**Trong phạm vi:** phân tích lịch sử sáu khoang AGC, bộ lọc vụ/khoang/thời gian, so sánh actual với setpoint, thống kê và truy vấn chi tiết các đợt lệch.

**Ngoài phạm vi:** điều khiển quạt/sưởi/CO₂, gửi cảnh báo thời gian thực, chẩn đoán stress sinh lý và tự tối ưu setpoint. Các sự kiện trong tài liệu là sự kiện phát hiện trên dữ liệu lịch sử.

## 2. Quy ước chung cho cả sáu BR

Các quy ước này cũng áp dụng cho [BR-02](br_02_irrigation-drainage-monitoring.md), [BR-03](br_03_drain-nutrition-monitoring.md), [BR-04](br_04_energy-intensity-analysis.md), [BR-05](br_05_harvest-quality-tracking.md) và [BR-06](br_06_energy-cost-scenarios.md).

- `cycle_id` là một vụ của một khoang. Không dùng chung một cycle cho cả sáu khoang. Tên thư mục nguồn ánh xạ sang khoang/đội qua metadata có nguồn chứng minh.
- Cửa sổ mặc định của dataset: `[2019-12-16 00:00, 2020-05-30 00:00)`. Ngày cuối của Resources/Production trong cửa sổ là 2020-05-29. Các phép đo ở đúng điểm biên kết thúc được lưu nhưng không mở thêm slot sau cuối vụ.
- Thời gian Excel dùng epoch `1899-12-30`; giữ serial gốc. Nhãn thời gian nguồn chưa có timezone được xác minh: không tự gắn UTC, dịch +7 giờ hay áp DST. Thời điểm chạy pipeline có timezone riêng, không thay thế thời gian quan sát.
- Với nguồn 5 phút, chuẩn hóa slot từ serial theo `round(serial × 288)` sau kiểm tra sai số. Mỗi mẫu đại diện `[t, t+5 phút)` theo quy ước phân tích. Thời lượng là ước lượng theo cadence, không phải đo liên tục đã xác thực.
- Cửa sổ ngày/tuần luôn nửa mở; tuần dùng ISO year + ISO week. Cắt slot ở biên truy vấn. Nguồn tổng ngày chỉ hỗ trợ chọn ngày trọn vẹn; không phân bổ giả xuống giờ.
- Trạng thái chất lượng theo từng measure: `accepted`, `missing`, `invalid`, `suspect`. Baseline chỉ tính `accepted`; thay đổi policy phải có version. Không loại cả dòng chỉ vì một measure lỗi.
- Phân biệt số 0 có quan sát, không có dữ liệu dùng được, kết quả một phần và chưa đủ điều kiện tính. API/export dùng NULL cho giá trị chưa xác định; giao diện hiển thị lý do. Tỷ lệ có mẫu số 0 là NULL.
- Mỗi kết quả có `dataset_snapshot_id`, `pipeline_run_id`, `metric_version`, `quality_policy_version`, cửa sổ tính và `reason_codes`. Các BR theo ngưỡng thêm `rule_version`; BR chi phí thêm `tariff_version`, `scenario_version`. Khi có correction hoặc quy đổi diện tích, giữ thêm `correction_version` và `basis_version`; các BR chia sẻ đầu vào phải dùng cùng bộ version khi đối soát.
- Lưu raw bất biến cùng checksum/file/dòng; mọi sửa lỗi là bản dẫn xuất có lineage. Nạp lại cùng snapshot không nhân số bản ghi. Trùng khóa cùng nội dung được khử trùng có log; trùng khóa khác nội dung phải xử lý conflict, không chọn tùy ý một bản.
- Không mặc định các cột cùng ghi `/m²` có cùng basis diện tích. Không cộng các tỷ số hoặc cộng trực tiếp số đo nhiệt độ, pH, CO₂.
- Tên bảng/mart trong sáu tài liệu là thiết kế logic đề xuất, không xác nhận bảng đã tồn tại và không khóa vào một engine.
- Các ngưỡng demo là cấu hình phân tích, không phải tiêu chuẩn nông học đã được phê duyệt. Giao diện và export phải giữ nhãn này.
- Mục tiêu hiệu năng đề xuất: p95 truy vấn tổng hợp không quá 2 giây trên snapshot AGC, đo ít nhất 30 lần sau 5 lần warm-up, công bố cấu hình máy/engine/cache. Đo cold run và thời gian pipeline riêng; không coi mục tiêu này là số đã đo. Truy vấn chi tiết phải phân trang.

## 3. Dữ liệu đầu vào và grain

| Nguồn/cột | Ý nghĩa | Cách dùng |
|---|---|---|
| `GreenhouseClimate.csv`: `%time` | Excel serial, 5 phút | Thời gian quan sát |
| `Tair`, `CO2air` | Nhiệt độ °C, nồng độ CO₂ ppm | Actual; quality độc lập |
| `t_heat_sp`, `t_vent_sp`, `co2_sp` | Requested setpoints | Basis `requested` |
| `t_heat_vip`, `t_ventlee_vip`, `co2_vip` | Realized setpoints của process computer | Basis `realized`, báo cáo riêng |
| Metadata nguồn | Khoang, đội, vụ, site | Gắn ngữ cảnh và cửa sổ vụ |

Grain core: `(cycle_id, slot_start)`. Grain kết quả tổng hợp: `(cycle_id, window, basis, metric, rule_version)`. Grain sự kiện: một chuỗi slot lệch liên tiếp cho một metric/basis/rule.

Sáu file climate có 47.809 dòng/file, gồm điểm biên cuối. Đủ timestamp không đồng nghĩa đủ giá trị: profiling trước đó ghi nhận AICU thiếu khoảng 69,7% `t_heat_sp` và 86,0% `co2_sp`. Phải tái lập coverage trên đúng snapshot và cửa sổ đang báo cáo.

## 4. Quy tắc tính và cấu hình

### 4.1. Chọn điều kiện so sánh

- Người dùng chọn `requested` hoặc `realized`; không `COALESCE` hai basis. Mặc định mở `requested` để bám ý nghĩa presentation, đồng thời cho xem `realized` bên cạnh.
- Với nhiệt độ: `lower = heating_setpoint − delta_temp`; `upper = ventilation_setpoint + delta_temp`. Setpoint thông gió dùng phía leeward như dictionary; đây là một vùng điều khiển tham chiếu đã nêu rõ, không mô hình hóa toàn bộ controller.
- `temp_out = Tair < lower OR Tair > upper`. Bằng biên được tính trong dải. `delta_temp` hữu hạn và không âm; thiếu cận hoặc `lower > upper` tạo trạng thái không xác định.
- Với CO₂: `co2_lower = max(0, selected_co2_setpoint − delta_co2)`; `co2_upper = selected_co2_setpoint + delta_co2`; so sánh ngoài dải đóng. Tolerance là ppm, không phải phần trăm.
- Hồ sơ demo đề xuất: `delta_temp = 0.5°C`, `delta_co2 = 50 ppm`, áp dụng toàn bộ slot trong cửa sổ, nhãn `demo_rule`. Đây là lựa chọn thiết kế để chạy demo, không suy ra từ dictionary hay chứng minh điều kiện tối ưu.
- Setpoint CO₂ bằng 0: baseline coi là chưa rõ ngữ nghĩa/off-mode và đưa vào unknown. Chỉ loại khỏi phạm vi áp dụng khi có policy giải thích chế độ tắt. Không tự coi mọi slot ban đêm là không áp dụng.
- Setpoint hữu hạn bất thường cần đánh giá ngữ nghĩa điều khiển; không dùng dải nhiệt độ cảm biến để tự động kết luận setpoint hỏng. Giữ cờ suspect theo policy có nguồn.
- Rule lưu `rule_id/version`, basis, tolerance, khoảng hiệu lực, phạm vi áp dụng, người ghi nhận và lý do. Một phạm vi không được có hai version chồng hiệu lực. Thay ngưỡng tạo version mới.

### 4.2. Thời lượng và coverage

Với từng metric, `expected_minutes` là thời lượng của cửa sổ đã cắt theo vụ; bản v1 áp dụng toàn cửa sổ. `valid_minutes` chỉ gồm slot đủ actual và các setpoint/tolerance hợp lệ.

```text
out_hours      = out_minutes / 60
out_percent    = 100 × out_minutes / valid_minutes
coverage_pct   = 100 × valid_minutes / expected_minutes
unknown_minutes = expected_minutes − valid_minutes
```

Nếu không có slot hợp lệ, `out_hours` và `out_percent` công bố là NULL; số đếm nội bộ bằng 0 không được hiển thị như đã chứng minh không lệch. Khi có dữ liệu một phần, ghi rõ số giờ là số giờ quan sát được.

Tổng “nhiệt độ hoặc CO₂ lệch” là hợp của các slot vi phạm trên tập slot cả hai metric đều hợp lệ; báo coverage chung riêng. Không cộng hai tổng giờ riêng lẻ. Không ngoại suy giờ lệch cho phần thiếu.

### 4.3. Định nghĩa sự kiện

- Một sự kiện là chuỗi slot liền kề có cùng `cycle/metric/basis/rule_version` và trạng thái `out`.
- Slot trong dải, missing/invalid/suspect, đổi rule hoặc ngắt cadence đều ngắt sự kiện. `end_time` là biên phải loại trừ của slot cuối.
- Lưu loại lệch dưới/trên biên tại từng slot, độ lệch lớn nhất và thời điểm đạt đỉnh; một đợt `out` có thể chứa cả hai hướng.
- Tạo sự kiện trên toàn vụ trước khi lọc khoảng báo cáo. Khi sự kiện giao với cửa sổ, trả cả thời gian gốc và thời lượng phần giao; không làm sự kiện trông như bắt đầu đúng biên lọc.
- Bộ lọc sự kiện tối thiểu 15 phút là cấu hình demo để ưu tiên xem; không được loại các đợt ngắn khỏi tổng giờ lệch.

## 5. Xử lý DE và dữ liệu phục vụ

`RawClimate → StagingClimate → ClimateObservation + MetricRule + DQIssue → FactClimate / ClimateEpisode / ClimatePeriodSummary`.

Core chịu trách nhiệm thời gian, danh tính, đơn vị, chất lượng và giữ riêng SP/VIP. Mart đánh giá rule, cộng duration và tạo sự kiện. Không nhúng cách đọc CSV hoặc fallback setpoint khác nhau vào từng biểu đồ.

Output tối thiểu: khoang/vụ, metric/basis, window, out/valid/unknown minutes, out percent, coverage, episode count, longest episode, rule version, trạng thái chất lượng và đường dẫn truy nguồn. Truy vấn chi tiết trả actual, setpoint, cận áp dụng và lý do loại của từng slot.

## 6. Luồng sử dụng và giao diện

1. Chọn vụ, khoang, khoảng ngày và basis; màn hình luôn thể hiện đây là dữ liệu lịch sử.
2. Xem bảng tổng hợp, coverage và số đợt. Khi coverage khác nhau, không trình bày bảng xếp hạng tuân thủ không điều kiện.
3. Lọc/sắp xếp danh sách sự kiện theo thời lượng hoặc độ lệch để ưu tiên điều tra.
4. Chọn sự kiện để xem biểu đồ actual/cận và các khoảng unknown; xem phiên bản rule và bản ghi nguồn.
5. Xuất CSV đúng bộ lọc, kèm đơn vị, coverage, basis và metadata phiên bản.

Không dùng màu xanh cho khoảng thiếu. Thay rule phải cập nhật đồng bộ biểu đồ, KPI và export; dữ liệu cũ vẫn truy được theo version.

## 7. Tiêu chí nghiệm thu và ví dụ chuẩn

Tất cả dữ liệu dưới đây là fixture tổng hợp để kiểm thử, không phải kết quả AGC. Sai số KPI cho phép `1e-6` trước làm tròn hiển thị; khóa, slot và số đếm phải khớp chính xác.

| ID | Đầu vào/thao tác | Kết quả bắt buộc |
|---|---|---|
| AC-01 | Sáu slot 5 phút; cận [20,25], tolerance 0; Tair = [19,21,26,26,NULL,24] | Out 15 phút; valid 25; unknown 5; out 60%; coverage 83,333333%; 2 sự kiện dài 5 và 10 phút |
| AC-02 | Actual bằng đúng cận dưới/trên | Trong dải |
| AC-03 | SP thiếu, VIP có giá trị | Requested unknown; realized tính độc lập; không fallback |
| AC-04 | Hai slot chung hợp lệ: nhiệt độ lệch cả hai, CO₂ lệch slot thứ hai | Tổng OR = 10 phút, không phải 15 |
| AC-05 | Hai slot out bị ngăn bởi một slot missing | Hai sự kiện; không nối qua gap |
| AC-06 | Không có actual/setpoint dùng được | Coverage 0%, KPI lệch NULL và lý do; không hiển thị “0 giờ, đạt” |
| AC-07 | Điểm tại 2020-05-30 00:00 | Lưu nguồn nhưng không cộng thêm 5 phút vào vụ |
| AC-08 | Nạp lại cùng snapshot; thay rule version rồi quay về version cũ | Không nhân bản; tái lập đúng kết quả của mỗi version |
| AC-09 | Sự kiện bắt đầu trước cửa sổ lọc | Giữ thời điểm gốc; số phút trong báo cáo bằng phần giao |
| AC-10 | CO₂ SP = 0 hoặc cận nhiệt độ đảo | Unknown kèm reason; không tính là đạt/vi phạm sinh học |

## 8. Đánh giá, phụ thuộc và điều kiện hoàn thành

- **Độ đúng:** tính fixture độc lập; đối soát từng slot và tổng core–mart; xác minh không double-count thời lượng.
- **Hiệu năng:** đo summary và trang đầu danh sách sự kiện theo quy ước chung; ghi cả thời gian tái tính khi đổi rule.
- **Khai thác:** người dùng phải tìm được đợt dài nhất của metric được chọn, mở đúng diễn biến và giải thích được phần unknown bằng kịch bản có đáp án trước.
- **Phụ thuộc:** metadata khoang/vụ, parser thời gian, quality policy và kho rule có version. Có thể triển khai dù timezone chưa xác minh bằng cách giữ time basis nguồn và công bố giới hạn.
- **Cần xác minh:** ý nghĩa setpoint bất thường/off-mode; ngưỡng và phạm vi áp dụng nông học. Demo dùng profile gắn nhãn; chưa được gọi là kết luận nông học đã thẩm định.
- **Done when:** AC-01…AC-10 đạt; màn hình/export chạy trên AGC; lineage mở được; có kết quả đo và video demo; không còn lỗi dữ liệu bị che bằng 0 hoặc fallback ngầm.

## 9. Nguồn và quan hệ tài liệu

- [Yêu cầu môn học](../../DE_Requirements.pdf), trang 1–2, 5–6: business requirements, correctness, performance và khai thác ứng dụng.
- [Nguồn AGC trong phân tích tham khảo](../ref/SmartGreenhouse_Case_Analysis.md#21-nguồn-dữ-liệu), [quy mô](../ref/SmartGreenhouse_Case_Analysis.md#22-quy-mô) và [grain](../ref/SmartGreenhouse_Case_Analysis.md#23-grain-ba-mức-cùng-tồn-tại). Đây là tổng quan lịch sử, không phải báo cáo profiling snapshot; các tỷ lệ thiếu nêu ở mục 3 sẽ được đo lại ở T05. Chưa có tài liệu phương pháp kiểm chứng chi tiết trong repo; timezone và ngữ nghĩa setpoint/off-mode sẽ được xác minh ở T03 từ tài liệu nguồn.
- `ReadMe.pdf`, mục GreenhouseClimate và Climate and irrigation setpoints, trong [archive Kaggle AGC](https://www.kaggle.com/datasets/piantic/autonomous-greenhouse-challengeagc-2nd-2019/).
- Schema gợi ý trong phân tích tham khảo: [Inmon](../ref/SmartGreenhouse_Case_Analysis.md#3-schema-đại-diện-inmon-lõi-3nf), [Kimball](../ref/SmartGreenhouse_Case_Analysis.md#4-schema-đại-diện-kimball-star-factclimate) và [Hybrid](../ref/SmartGreenhouse_Case_Analysis.md#5-schema-đại-diện-hybrid). Tài liệu cũ là tham chiếu; công thức/rule của BR-01 cuối kỳ được định nghĩa tại file này.
