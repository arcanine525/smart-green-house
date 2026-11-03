# BR-02 — Theo dõi tưới, drain và giai đoạn cần rà soát

| Thuộc tính | Giá trị |
|---|---|
| Project | Group 29 — Smart Greenhouse |
| Phiên bản | 1.0 — 2026-11-03 |
| Trạng thái | Đặc tả đề xuất; chưa phải kết quả nghiệm thu |
| Người dùng chính | Người vận hành tưới |
| Người dùng liên quan | Kỹ sư canh tác, quản lý trang trại |
| Ưu tiên | Bắt buộc; phù hợp triển khai luồng đầu tiên |
| Kế thừa | Chỉnh BR-02: lượng nước/tỷ lệ drain là chính; mưa là bối cảnh tùy chọn |

## 1. Mục tiêu và yêu cầu nghiệp vụ

> Người vận hành cần theo dõi lượng nước tưới, lượng drain và tỷ lệ drain theo khoang/ngày/tuần, xác định ngày cần rà soát, để kiểm tra lại chế độ tưới dựa trên dữ liệu.

Kết quả hỗ trợ chọn thời gian và khoang cần điều tra. Drain cao chưa tự chứng minh lãng phí; dataset không đủ để xác nhận toàn bộ nước drain bị thải bỏ hay được tái sử dụng.

**Trong phạm vi:** tổng nước ngày/tuần/toàn vụ, tỷ lệ drain, ngày ngoài dải kiểm tra, truy nguồn và so sánh có điều kiện.

**Ngoài phạm vi:** tự động tưới, tối ưu lượng tưới, tính chính xác lượng nước cây hấp thu, kết luận mưa gây thay đổi tưới, chứng minh tiết kiệm nước thực tế.

Áp dụng [quy ước chung](br_01_climate-condition-monitoring.md#2-quy-ước-chung-cho-cả-sáu-br): thời gian nguồn, cửa sổ nửa mở, chất lượng theo measure, lineage, version và mục tiêu hiệu năng. BR này chỉ nhận cửa sổ ngày trọn vẹn.

## 2. Dữ liệu đầu vào và grain

| Nguồn/cột | Ý nghĩa | Vai trò |
|---|---|---|
| `Resources.csv`: `%Time ` | Ngày dạng Excel serial | Ngày tài nguyên |
| `Irr` | Lượng tưới ngày, L/m² | Nguồn chính của KPI nước |
| `Drain` | Lượng drain ngày, L/m² | Nguồn chính của KPI drain |
| `GreenhouseClimate.csv`: `Cum_irr` | L/m² cộng dồn trong ngày, reset nửa đêm | Drill-down nhịp tưới, phần mở rộng |
| `water_sup` | Phút tưới cộng dồn trong ngày | Thời gian tưới; không phải thể tích |
| `Weather.csv`: `Rain` | Trạng thái mưa/khô | Bối cảnh site, phần mở rộng |
| Metadata | Khoang/vụ/site và basis diện tích nước | Ngữ cảnh và tính tương thích |

Grain core/mart ngày: `(cycle_id, resource_date)`. Summary: `(cycle_id, window)`. Nguồn Weather có grain `site × slot`, không phải sáu trạm độc lập.

Mỗi Resources có 166 ngày. Profiling gần nhất có số hữu hạn ở Irr/Drain đủ các ngày; điều này không thay thế kiểm tra giá trị và ngày của snapshot triển khai. Các cột rỗng dư của IUACAAS chỉ được bỏ khi đã xác nhận toàn rỗng.

## 3. Quy tắc chất lượng và phạm vi tính

1. Parse ngày, đơn vị và số ở chế độ phát hiện lỗi. Thiếu/NaN/infinite tạo trạng thái không dùng được. Giá trị âm bị đánh dấu invalid theo hợp đồng lượng ngày; không tự lấy trị tuyệt đối.
2. Giá trị hữu hạn lớn bất thường được gắn suspect nếu vi phạm rule đã cấu hình; không xóa chỉ vì khác các khoang khác.
3. Duplicate ngày giống nhau được khử trùng có lineage; khác nhau phải xử lý conflict trước khi công bố ngày đó.
4. Kiểm tra `Irr` và `Drain` dùng cùng basis diện tích trước khi tính tỷ lệ. Chưa xác minh thì hiển thị hai tổng riêng và `ratio = NULL`, reason `incompatible_or_unknown_basis`.
5. Không cộng L/m² của sáu khoang để gọi là tổng lượng nước trang trại. Tổng L cần diện tích đúng của từng measure/khoang; chưa có thì chỉ so sánh riêng từng khoang.
6. `Irr − Drain` có thể hiển thị như chênh lệch ghi nhận nếu có cùng basis, nhưng không đặt tên “lượng cây hấp thu”.
7. `Drain > Irr` là một trường hợp cần xem xét, không tự clamp về 100% hoặc loại bỏ. Nếu cả hai số được chấp nhận, giữ tỷ lệ thật cùng cờ kiểm tra.

## 4. Định nghĩa KPI

Gọi `D` là tập ngày kỳ vọng trong cửa sổ vụ. Với từng measure, tính tổng trên ngày accepted và coverage riêng. Không có ngày accepted thì tổng công bố là NULL.

```text
irrigation_observed_l_m2 = Σ Irr trên ngày Irr accepted
drain_observed_l_m2      = Σ Drain trên ngày Drain accepted
irrigation_coverage_pct  = 100 × số ngày Irr accepted / |D|
drain_coverage_pct       = 100 × số ngày Drain accepted / |D|
```

Đối với tỷ lệ, dùng **cùng tập ngày** `P` mà cả Irr/Drain đều accepted và basis tương thích:

```text
paired_irrigation_l_m2 = Σ Irr trên P
paired_drain_l_m2      = Σ Drain trên P
drain_fraction_pct    = 100 × paired_drain_l_m2 / paired_irrigation_l_m2
paired_coverage_pct   = 100 × |P| / |D|
```

- `P` rỗng hoặc tổng Irr trên P bằng 0 → tỷ lệ NULL. Trường hợp Irr=Drain=0 là quan sát không tưới, không chứng minh tỷ lệ drain bằng 0%.
- Không lấy trung bình tỷ lệ ngày để tính tỷ lệ tuần. Summary phải trả cả hai tổng paired để giải thích mẫu số.
- Nếu coverage chưa đủ, gắn nhãn “kết quả trên các ngày có dữ liệu”; không ngoại suy tổng toàn kỳ.
- Cho so sánh chính thức chỉ khi cùng cửa sổ, cùng policy, basis tương thích và paired coverage 100%. Vẫn cho xem khoang thiếu dữ liệu bên cạnh với trạng thái rõ ràng; không tự chuyển sang giao các ngày để làm coverage trông đầy đủ.

### Ngày cần rà soát

Hồ sơ kiểm tra có `min_drain_pct`, `max_drain_pct`, `min_irrigation_l_m2`, `max_irrigation_l_m2`, phạm vi khoang và version. Các ngưỡng phải hữu hạn, đúng đơn vị và có thứ tự hợp lệ.

Không đặt một dải nông học mặc định cho mọi khoang. Nếu chưa cấu hình, vẫn có KPI và biểu đồ nhưng trạng thái kiểm tra là `not_configured`. Demo dùng ngưỡng khai báo trong fixture. Bằng biên được xem là trong dải; ngày thiếu dữ liệu là unknown.

## 5. Xử lý DE và các mở rộng có điều kiện

`RawResources → StagingResources → ResourceDay + DQIssue + AreaBasis → FactIrrigationDay → IrrigationPeriodSummary`.

Nguồn tổng ngày Resources là nguồn chuẩn cho BR. Counter không được cộng thêm vào Resources như một nguồn nước thứ hai.

Nếu triển khai drill-down counter: chỉ tính delta giữa hai mẫu liên tiếp hợp lệ trong cùng ngày; xử lý reset theo ngày; giảm trong ngày là cờ kiểm tra. Không suy ra delta qua gap, không tự suy ra thể tích trước mẫu đầu ngày, và không giả định mẫu 00:00 là lượng của ngày trước. Cần version quy tắc và bằng chứng về timestamp để đối soát counter với tổng ngày.

Nếu thêm Weather: chuẩn hóa Rain gần 0/1 với tolerance công bố, aggregate site/ngày trước khi ghép khoang/ngày. Rain là flag nên có thể tính phút có mưa, không tính mm. Không nhân tổng mưa lên sáu lần khi nhìn cấp site.

Hai mở rộng trên không phải điều kiện nghiệm thu chức năng nước ngày/tuần của v1.

## 6. Output và luồng sử dụng

Output summary gồm: khoang/vụ, cửa sổ, tổng Irr/Drain observed, tổng paired, tỷ lệ drain, coverage từng measure/paired, số ngày unknown/suspect/ngoài rule, basis status, rule version và metadata chung.

1. Chọn vụ, một hoặc nhiều khoang và khoảng ngày.
2. Xem đường lượng tưới/drain và tỷ lệ theo ngày; NULL phải tạo khoảng trống, không vẽ xuống 0.
3. Xem bảng tổng hợp tuần; mở tử số/mẫu số của một tỷ lệ.
4. Chọn một ngày cần rà soát, xem giá trị gốc, rule và lỗi chất lượng liên quan.
5. Xuất dữ liệu theo bộ lọc; export giữ đơn vị, coverage và lý do chưa tính được.

## 7. Tiêu chí nghiệm thu và ví dụ chuẩn

Fixture dưới đây là dữ liệu tổng hợp; giả định basis Irr/Drain đã xác nhận tương thích. Sai số số thực `1e-6` trước làm tròn.

| ID | Đầu vào/thao tác | Kết quả bắt buộc |
|---|---|---|
| AC-01 | Hai ngày: Irr [10,30], Drain [2,12] | Tổng Irr=40, Drain=14 L/m²; tỷ lệ kỳ=35%, không phải trung bình ngày 30% |
| AC-02 | Irr=0, Drain=0 trong cả kỳ | Tổng đều 0; coverage 100%; tỷ lệ NULL với lý do không có lượng tưới |
| AC-03 | Ngày 1: Irr=10, Drain=2; ngày 2: Irr=30, Drain=NULL | Irr observed=40; Drain observed=2; paired Irr=10, Drain=2; tỷ lệ=20%, paired coverage=50%, trạng thái partial |
| AC-04 | Irr=5, Drain=6 đều accepted | Tỷ lệ 120%; có cờ cần rà soát, không clamp |
| AC-05 | Không có số dùng được trong cửa sổ | Tổng và tỷ lệ NULL; coverage 0%, không hiển thị lượng nước 0 |
| AC-06 | Rule demo: tỷ lệ trong [20,40]%; hai ngày có tỷ lệ 20% và 50% | Chỉ ngày 50% nằm ngoài rule; ghi rule version |
| AC-07 | Chưa cấu hình rule | KPI vẫn có; không có kết luận đạt/không đạt theo ngưỡng |
| AC-08 | Nạp lại file hoặc có hai dòng cùng ngày khác nội dung | Không nhân tổng; conflict được báo, không chọn ngầm một dòng |
| AC-09 | Cùng ghi L/m² nhưng basis chưa xác minh | Giữ tổng riêng; tỷ lệ NULL và không xếp hạng |
| AC-10 | Xuất CSV từ một khoang/tuần | Giá trị, phạm vi và coverage khớp UI; không lẫn ngày ngoài bộ lọc |

## 8. Đánh giá, phụ thuộc và điều kiện hoàn thành

- **Độ đúng:** tính fixture độc lập; đối soát tổng ngày/tuần và chứng minh ratio of sums; kiểm tra số dòng trước/sau join.
- **Hiệu năng:** summary, dữ liệu chart và danh sách ngày theo mục tiêu chung; ghi thời gian ingestion và rerun.
- **Khai thác:** người vận hành tìm được ngày vượt rule đã cấu hình và giải thích được tử số/mẫu số cùng độ tin cậy của tuần đó.
- **Phụ thuộc:** parser Resources, metadata vụ, quality policy và xác nhận basis Irr/Drain. Không cần Kafka, nguồn thời tiết ngoài hoặc dữ liệu thu hoạch để hoàn thành v1.
- **Cần xác minh:** basis diện tích, ý nghĩa các giá trị ngoại lệ lớn và ngưỡng canh tác nếu muốn diễn giải chuyên môn.
- **Done when:** AC-01…AC-10 đạt; pipeline–mart–UI–export chạy xuyên suốt; có số đối soát, kết quả đo và demo; không tuyên bố tiết kiệm nước hoặc cây hấp thu từ phép trừ đơn giản.

## 9. Nguồn và quan hệ tài liệu

- [Yêu cầu môn học](../../DE_Requirements.pdf), trang 1–2 và 6.
- [Nguồn AGC trong phân tích tham khảo](../ref/SmartGreenhouse_Case_Analysis.md#21-nguồn-dữ-liệu), [grain](../ref/SmartGreenhouse_Case_Analysis.md#23-grain-ba-mức-cùng-tồn-tại) và [vấn đề dữ liệu](../ref/SmartGreenhouse_Case_Analysis.md#25-vấn-đề-dữ-liệu-chính), gồm counter và missing. Tổng quan này không thay thế dictionary gốc; đơn vị và ý nghĩa cột dùng theo ReadMe cùng mục 2 của BR này. [Inventory T05](../implementation/data-inventory.md) đã kiểm kê file/header/dòng/time và cột rỗng dư; [quyết định basis nước](../implementation/data-decisions.md#1-basis-diện-tích-và-đơn-vị) xác nhận đơn vị nhưng chưa xác minh mẫu số tương thích Irr/Drain; [user cho phép AGC_SHARED_BASIS_V1 có nhãn](../implementation/data-decisions.md#7-br-04--quyết-định-basis-của-user), giữ NULL path khi thiếu/tắt giả định.
- `ReadMe.pdf`, mục Resources, GreenhouseClimate và Weather, trong [dataset Kaggle](https://www.kaggle.com/datasets/piantic/autonomous-greenhouse-challengeagc-2nd-2019/).
- [BR-03](br_03_drain-nutrition-monitoring.md) có thể mở cùng cửa sổ để hỗ trợ điều tra; liên hệ trên biểu đồ không tự chứng minh quan hệ nhân quả.
