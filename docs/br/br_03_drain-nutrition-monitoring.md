# BR-03 — Theo dõi EC/pH của dung dịch drain

| Thuộc tính | Giá trị |
|---|---|
| Project | Group 29 — Smart Greenhouse |
| Phiên bản | 1.0 — 2026-11-03 |
| Trạng thái | Đặc tả đề xuất; chưa phải kết quả nghiệm thu |
| Người dùng chính | Kỹ sư canh tác |
| Người dùng liên quan | Người vận hành tưới/dinh dưỡng |
| Ưu tiên | Bắt buộc trong bộ sáu BR |
| Kế thừa | Làm rõ BR-03: phạm vi quan sát là EC/pH dung dịch drain |

## 1. Mục tiêu và yêu cầu nghiệp vụ

> Kỹ sư canh tác cần xác định các khoảng EC hoặc pH của dung dịch drain nằm ngoài dải mục tiêu, biết mức độ và thời lượng, để ưu tiên kiểm tra chế độ dinh dưỡng và tưới.

Hệ thống trả lời khoang/tuần nào có nhiều thời gian ngoài mục tiêu, đợt dài nhất xảy ra khi nào và dữ liệu nào cần xác minh trước khi kết luận. EC/pH drain là hai chỉ báo quan sát; không đại diện đầy đủ cho tình trạng dinh dưỡng hay sức khỏe cây.

**Trong phạm vi:** theo dõi riêng EC/pH drain, thời lượng ngoài dải, sự kiện liên tiếp, cấu hình ngưỡng và đối chiếu dữ liệu tưới theo ngày.

**Ngoài phạm vi:** chẩn đoán thiếu/thừa từng nguyên tố, đề xuất công thức phân bón tự động, khẳng định thiệt hại cây, trộn phép đo drain với giá thể/lab thành một chuỗi duy nhất.

Áp dụng [quy ước chung](br_01_climate-condition-monitoring.md#2-quy-ước-chung-cho-cả-sáu-br) về thời gian, quality policy, lineage, NULL, version và đo hiệu năng.

## 2. Dữ liệu đầu vào và grain

| Nguồn/cột | Ý nghĩa | Grain và vai trò |
|---|---|---|
| `GreenhouseClimate.csv`: `%time` | Excel serial | Khoang × mốc 5 phút |
| `EC_drain_PC` | EC dung dịch drain, dS/m | Measure EC chính |
| `pH_drain_PC` | pH dung dịch drain, không đơn vị | Measure pH chính |
| `Resources.csv`: `Irr`, `Drain` | L/m²/ngày | Bối cảnh ngày từ BR-02 |
| Metadata khoang/vụ | Danh tính và cửa sổ | Conformed dimensions |
| `GrodanSens`, `LabAnalysis` | Phép đo giá thể và mẫu lab | Mở rộng có điều kiện, giữ ngữ cảnh riêng |

Grain core: `(cycle_id, slot_start, measurement_context='drain_process_computer')`. Hai measure EC/pH có cờ chất lượng độc lập. Một dòng nguồn có thể dùng được cho EC nhưng không dùng được cho pH.

EC của Grodan cùng đơn vị dS/m vẫn là bối cảnh giá thể khác. Lab có khoảng lấy mẫu dài; không forward-fill thành dữ liệu quan sát 5 phút và không dùng để cộng số giờ ngoài dải.

## 3. Quy tắc chất lượng và ngưỡng

- Missing/NaN/infinite không tham gia phép tính. EC âm bị đánh dấu invalid theo hợp đồng measure này.
- pH=0 được gắn suspect trong bối cảnh dataset, không khẳng định 0 là giá trị pH bất khả thi trong mọi dung dịch. Baseline loại suspect khỏi mẫu số và báo riêng số phút suspect.
- Giá trị pH rất cao/thấp nhưng chưa có bằng chứng lỗi không được tự loại chỉ vì ngoài dải mục tiêu. **Ngoài mục tiêu là kết quả nghiệp vụ; invalid/suspect là quyết định chất lượng dữ liệu.** Hai loại rule phải tách riêng và có version.
- Profile demo kế thừa presentation: EC trong `[2,5] dS/m`; pH trong `[5.5,6.5]`. Đây là rule phân tích của đồ án, chưa được xác minh như tiêu chuẩn phổ quát cho giống cây/giai đoạn này.
- Rule có `metric`, `measurement_context`, cận dưới/trên, đơn vị, scope, effective interval, version, provenance và trạng thái `demo`/`validated_for_scope`. Không có hai rule chồng hiệu lực cho cùng scope/metric.
- Cho phép kỹ sư tạo version khác và so sánh kết quả; không sửa ngầm kết quả của version cũ. Cận phải hữu hạn và lower không lớn hơn upper; cận EC không âm.
- Nếu chưa có rule phù hợp, hiển thị phép đo và quality nhưng KPI ngoài dải là NULL với `rule_not_configured`.

## 4. Định nghĩa KPI và sự kiện

### 4.1. Phân loại slot

```text
ec_low  = EC_drain_PC < ec_lower
ec_high = EC_drain_PC > ec_upper
ph_low  = pH_drain_PC < ph_lower
ph_high = pH_drain_PC > ph_upper
```

Bằng cận được xem là trong dải. Chỉ đánh giá trên slot measure accepted và có rule hợp lệ. Tính phút EC và pH riêng; không lấy độ phủ EC thay cho pH.

```text
out_hours(metric)    = out_minutes(metric) / 60
out_percent(metric)  = 100 × out_minutes(metric) / valid_minutes(metric)
coverage_pct(metric) = 100 × valid_minutes(metric) / expected_minutes
unknown_minutes     = expected_minutes − valid_minutes
```

Không có valid_minutes → KPI ngoài dải công bố NULL, coverage 0%. Unknown được phân rã theo lý do thiếu, invalid, suspect hoặc thiếu rule, tránh cộng trùng khi nhiều lý do trên cùng slot.

Đối với KPI “EC hoặc pH ngoài dải”: chỉ xét tập slot EC và pH cùng hợp lệ; đánh giá OR từng slot rồi cộng duration. Mẫu số là thời gian jointly valid, coverage chung báo riêng. Không cộng hai giờ riêng hoặc lấy tỷ lệ trung bình.

### 4.2. Sự kiện liên tiếp

Một sự kiện của từng metric gồm các slot out liền kề, cùng khoang/vụ/context/rule. Slot trong dải, gap, unknown hoặc đổi version ngắt sự kiện. Lưu `[start,end)`, duration, hướng lệch tại từng slot, mức lệch cao nhất và lineage.

Phát hiện sự kiện trên vụ rồi lấy phần giao khi lọc kỳ; giữ thời gian gốc và thời lượng trong kỳ. Bộ lọc độ dài sự kiện chỉ thay danh sách hiển thị, không thay tổng phút ngoài dải.

Không cộng pH trực tiếp và không dùng trung bình pH của tuần để quyết định tuần có bao nhiêu giờ ngoài dải. Baseline báo phân loại từng slot, min/max và sự kiện; không cần một “pH trung bình toàn khoang” để nghiệm thu.

## 5. Xử lý DE và dữ liệu phục vụ

`RawClimate → StagingClimate → DrainObservation + MetricRule + DQIssue → FactDrainCondition / DrainEpisode / DrainPeriodSummary`.

- Core giữ nguyên bối cảnh và quality từng measure; rule áp dụng được truy về cấu hình.
- Mart tính phân loại, duration và episode. Dùng cùng hạ tầng sự kiện với BR-01 nhưng khác metric, rule và phạm vi nghiệp vụ.
- Dữ liệu tưới lấy từ mart ngày của BR-02 qua cycle/date. Không nhân lượng tưới ngày vào mỗi slot EC/pH rồi SUM.
- Nếu thêm lab hoặc giá thể, chart phải có series/đơn vị/bối cảnh riêng và hiển thị đúng thời điểm mẫu. Phần này không bắt buộc cho v1.

Output summary: cycle/khoang, window, EC/pH out hours, low/high minutes, valid/unknown/suspect minutes, coverage từng metric, joint coverage/OR hours, số sự kiện, đợt dài nhất, rule/context và metadata chung.

## 6. Luồng sử dụng và giao diện

1. Kỹ sư chọn khoang, tuần và profile mục tiêu.
2. Xem hai thẻ EC/pH với coverage riêng; biểu đồ có dải mục tiêu và các khoảng unknown.
3. Lọc đợt EC cao kéo dài hoặc pH thấp; chọn một đợt để xem slot và nguyên nhân phân loại.
4. Mở bối cảnh tưới/drain theo ngày trong cùng cửa sổ. Giao diện ghi rõ đây là đối chiếu để điều tra, chưa kết luận nguyên nhân.
5. Xem các điểm suspect và lineage; xuất kết quả có rule/quality version.

Nếu người dùng chọn profile demo, nhãn “ngưỡng minh họa của đồ án” phải xuất hiện trong màn hình và export. Không dùng ngôn ngữ “cây bị thiếu dinh dưỡng” chỉ từ một đợt EC/pH ngoài dải.

## 7. Tiêu chí nghiệm thu và ví dụ chuẩn

Fixture sáu slot liên tiếp, mỗi slot 5 phút; profile EC `[2,5]`, pH `[5.5,6.5]`; pH=0 là suspect và bị loại theo baseline:

```text
EC = [3, 6, 6, NULL, 3, 6]
pH = [6, 6, 7,    6, 0, 6]
```

| ID | Trường hợp | Kết quả bắt buộc |
|---|---|---|
| AC-01 | EC của fixture | Valid 25 phút, out 15 phút, out=60%, coverage=83,333333%; 2 sự kiện dài 10 và 5 phút |
| AC-02 | pH của fixture | Valid 25 phút, out 5 phút, suspect 5 phút; out=20%, coverage=83,333333% |
| AC-03 | KPI OR của fixture | Joint valid 20 phút; OR out 15 phút; out=75%, coverage=66,666667%; không lấy 15+5=20 phút |
| AC-04 | EC=2/5, pH=5.5/6.5 | Trong dải |
| AC-05 | pH suspect, EC accepted trên cùng dòng | Giữ EC; pH unknown, không xóa toàn dòng |
| AC-06 | Tất cả slot thiếu hoặc chưa có rule | KPI ngoài dải NULL và lý do; không báo “đạt 100%” |
| AC-07 | Hai đợt out ngăn bởi gap hoặc đổi rule | Không gộp thành một sự kiện |
| AC-08 | Một mẫu lab mỗi hai tuần | Chỉ hiện ở thời điểm mẫu, không thêm hàng nghìn slot vào duration |
| AC-09 | Mở bối cảnh tưới ngày từ sự kiện 5 phút | Tổng tưới khớp BR-02, không bị nhân theo số slot |
| AC-10 | Đổi profile rồi mở lại profile cũ | Mỗi version tái lập kết quả và export tương ứng |

Sai số số thực `1e-6` trước làm tròn; thời lượng và số sự kiện khớp chính xác. Các con số trên là đáp án kiểm thử, không phải số đo AGC.

## 8. Đánh giá, phụ thuộc và điều kiện hoàn thành

- **Độ đúng:** fixture tính độc lập, đối soát slot–episode–summary; kiểm tra phân biệt lỗi đo với ngoài mục tiêu.
- **Hiệu năng:** summary và danh sách sự kiện theo mục tiêu chung; đo riêng rebuild khi rule đổi.
- **Khai thác:** kỹ sư tìm được đợt EC dài nhất, giải thích vì sao điểm pH suspect không vào KPI và mở đúng bối cảnh tưới.
- **Phụ thuộc:** parser Climate, hạ tầng quality/rule/event, metadata vụ; BR-02 chỉ cần cho drill-down bối cảnh tưới, không chặn KPI EC/pH.
- **Cần xác minh:** dải mục tiêu theo giống cây/giai đoạn và policy điểm nghi lỗi. Có thể nghiệm thu phép tính với profile demo đã công bố; chưa được nghiệm thu độ chính xác chẩn đoán nông học.
- **Done when:** AC-01…AC-10 đạt; có UI/export/lineage, đối soát và kết quả đo; ngưỡng demo, context đo và unknown được thể hiện rõ.

## 9. Nguồn và quan hệ tài liệu

- [Yêu cầu môn học](../../DE_Requirements.pdf), trang 1–2, 5–6.
- [Grain trong phân tích tham khảo](../ref/SmartGreenhouse_Case_Analysis.md#23-grain-ba-mức-cùng-tồn-tại) và [cột EC/pH drain](../ref/SmartGreenhouse_Case_Analysis.md#24-các-cột-quan-trọng); [BR-03 trong presentation tham khảo](../ref/GroupID_DE_Presentation.pdf#page=18) nêu câu hỏi EC/pH và dải minh họa. Đây không phải bằng chứng xác thực ngưỡng nông học. Chưa có báo cáo profiling/phương pháp kiểm chứng chi tiết trong repo; chất lượng EC/pH sẽ được kiểm kê/đo ở T05, còn ngữ nghĩa và giới hạn profile demo được ghi ở T03.
- `ReadMe.pdf`, mục GreenhouseClimate, LabAnalysis và GrodanSens, trong [dataset Kaggle](https://www.kaggle.com/datasets/piantic/autonomous-greenhouse-challengeagc-2nd-2019/).
- [BR-01](br_01_climate-condition-monitoring.md) chia sẻ kỹ thuật duration/episode; [BR-02](br_02_irrigation-drainage-monitoring.md) cung cấp bối cảnh nước. Các BR phục vụ quyết định khác nhau và được nghiệm thu riêng.
