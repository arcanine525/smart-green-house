# BR-06 — Phân tích chi phí năng lượng và kịch bản biểu giá

| Thuộc tính | Giá trị |
|---|---|
| Project | Group 29 — Smart Greenhouse |
| Phiên bản | 1.0 — 2026-11-03 |
| Trạng thái | Đặc tả đề xuất; chưa phải kết quả nghiệm thu |
| Người dùng chính | Quản lý trang trại/ngân sách |
| Người dùng liên quan | Người vận hành, nhóm phân tích dữ liệu |
| Ưu tiên | Bắt buộc trong bộ sáu BR |
| Thay thế | BR-06 cũ về proxy rủi ro bệnh; phân tích nhiệt/ẩm có thể giữ như tính năng phụ của khí hậu |

## 1. Mục tiêu và yêu cầu nghiệp vụ

> Quản lý cần biết chi phí sưởi và chiếu sáng theo kỳ, thành phần đóng góp chính, và chi phí thay đổi ra sao khi biểu giá thay đổi trong khi mức tiêu thụ giữ nguyên, để lập ngân sách theo kịch bản.

Khác [BR-04](br_04_energy-intensity-analysis.md), BR này hỗ trợ quyết định ngân sách và phân tích độ nhạy theo giá, không đo hiệu suất vật lý trên kg quả. Giá trị riêng của chức năng gồm biểu giá có hiệu lực, kịch bản được lưu phiên bản và giải thích chênh lệch chi phí.

**Trong phạm vi:** nhiệt sưởi, điện đèn cao/thấp điểm, biểu giá lịch sử của thí nghiệm, giá giả định do người dùng nhập, chi phí theo ngày/tuần/vụ và so sánh kịch bản.

**Ngoài phạm vi:** tổng chi phí trang trại, lợi nhuận, tiền nước/phân bón/lao động/CO₂, biểu giá hiện hành, thuế/phí công suất, ngoại tệ, dự báo tiêu thụ hoặc đề xuất đổi lịch đèn dựa trên giả định năng lượng không đổi.

Áp dụng [quy ước chung](br_01_climate-condition-monitoring.md#2-quy-ước-chung-cho-cả-sáu-br). Chỉ nhận cửa sổ ngày trọn vẹn; tiền tệ của v1 là EUR.

## 2. Dữ liệu đầu vào và grain

| Nguồn/cột | Ý nghĩa | Vai trò |
|---|---|---|
| `Resources.Heat_cons` | MJ/m²/ngày | Lượng nhiệt để định giá |
| `Resources.ElecHigh` | kWh/m²/ngày, khung cao điểm nguồn 07:00–23:00 | Lượng điện cao điểm |
| `Resources.ElecLow` | kWh/m²/ngày, ngoài khung trên | Lượng điện thấp điểm |
| `Economics.pdf` trong archive | Giá tham chiếu của thí nghiệm | Nguồn biểu giá baseline |
| Metadata khoang/vụ/basis | Diện tích và ý nghĩa mẫu số | Kiểm tra khả năng cộng chi phí |
| `TariffRate`, `CostScenario` đề xuất | Giá, hiệu lực, phiên bản và cấu hình người dùng | Input ứng dụng có kiểm soát |

Grain lượng nguồn: `(cycle_id, resource_date, energy_component)`. Grain kết quả chi tiết: cùng khóa cộng `scenario_version`. Summary theo khoang/kỳ/scenario.

Tariff và scenario là bảng do ứng dụng quản lý, không phải CSV có sẵn. Có thể nhập profile baseline từ Economics và ghi nguồn; không giả vờ dataset đã chứa hóa đơn chi phí thực tế.

## 3. Biểu giá và quy tắc kịch bản

### 3.1. Profile lịch sử

| Thành phần | Giá gốc | Đơn vị |
|---|---:|---|
| `electricity_peak` | 0.08 | EUR/kWh |
| `electricity_offpeak` | 0.04 | EUR/kWh |
| `heating` | 0.0083 | EUR/MJ |

Nguồn: `Economics.pdf`, trang 1–2 trong archive. Profile ứng dụng mang tên `AGC_2020_REFERENCE_V1`, áp dụng cho cửa sổ thí nghiệm `[2019-12-16,2020-05-30)` như phạm vi tính được nhóm khai báo; không phải bằng chứng biểu giá thị trường ngoài thí nghiệm.

Giữ đúng giá nhiệt 0.0083 EUR/MJ của tài liệu. Nếu hiển thị theo kWh, quy đổi thành 0.02988 EUR/kWh; không thay âm thầm bằng giá làm tròn 0.03 rồi kỳ vọng khớp tuyệt đối.

### 3.2. Đặc tả tariff/scenario

Mỗi rate có `tariff_version`, `component`, `currency`, `price_unit`, `rate`, `valid_from`, `valid_to_exclusive`, `source_reference`. Mỗi scenario có `scenario_id/version`, tên, baseline tham chiếu, rates/effective intervals, người ghi nhận, thời điểm tạo và giả định.

- V1 cho nhập giá hữu hạn không âm. Currency phải là EUR; đơn vị phải khớp component hoặc được chuyển đổi tường minh trước tính.
- Tại một ngày/component phải có đúng một giá. Chồng hiệu lực bị từ chối; thiếu giá làm component chưa tính được và báo reason. Không dùng giá gần nhất/current date để lấp gap.
- Nếu dùng cùng profile cho nhiều khoang, scope áp dụng phải rõ. So sánh hai khoang cần cùng biểu giá hoặc phải thể hiện khác biệt đó.
- User có thể sao chép baseline, đổi giá trực tiếp hoặc theo phần trăm và lưu version mới. Baseline bất biến; sửa scenario tạo version mới, giữ lịch sử.
- Scenario v1 chỉ đổi giá, không đổi khối lượng, loại nguồn, basis, cửa sổ đo hoặc ranh giới giờ cao/thấp điểm. Với Resources đã tổng hợp theo khung giờ nguồn, đổi khung giờ cần dữ liệu điện chi tiết hơn; UI v1 phải chặn thao tác đó.
- So sánh baseline/scenario giữ cùng dataset snapshot, quality policy, diện tích và tập ngày. Nếu thiếu dữ liệu hoặc thiếu giá ở một phía, không so sánh hai tổng được tính từ hai tập ngày khác nhau.

## 4. Công thức và điều kiện diện tích

### 4.1. Chi phí thành phần theo đơn vị nguồn

```text
heat_cost_per_basis_m2(d,s) = Heat_cons(d) × heat_price_eur_mj(d,s)
peak_cost_per_basis_m2(d,s) = ElecHigh(d)  × peak_price_eur_kwh(d,s)
offpeak_cost_per_basis_m2(d,s) = ElecLow(d) × offpeak_price_eur_kwh(d,s)
```

Mỗi kết quả có đơn vị EUR/m² trên **basis của thành phần tương ứng**. Có thể hiển thị riêng từng thành phần khi chưa hòa giải được basis, nhưng phải ghi rõ trạng thái.

Chỉ cộng ba chi phí per-m² nếu đã xác nhận cùng basis và basis không đổi trong cửa sổ. Nếu basis khác nhau hoặc thay đổi, tính lượng tuyệt đối theo diện tích có hiệu lực:

```text
heat_cost_eur(d,s) = Heat_cons(d) × A_heat(d) × heat_price_eur_mj(d,s)
peak_cost_eur(d,s) = ElecHigh(d) × A_electricity(d) × peak_price_eur_kwh(d,s)
offpeak_cost_eur(d,s) = ElecLow(d) × A_electricity(d) × offpeak_price_eur_kwh(d,s)
total_cost_eur(s) = Σ_d [heat_cost_eur + peak_cost_eur + offpeak_cost_eur]
```

Không lấy tổng EUR/m² khác basis để gọi là tổng chi phí. Chưa có diện tích số nhưng đã có bằng chứng chung basis cố định thì được báo tổng EUR/m², chưa được báo tổng EUR. Chưa xác minh cả hai cách thì chỉ công bố chi phí thành phần, `total_cost=NULL` và reason.

BR này không cần basis của Production hoặc sản lượng A. Điều kiện basis tài nguyên vẫn phải được kiểm tra; việc BR-04 chưa tính được kWh/kg không tự làm chi phí thành phần ngừng hoạt động.

### 4.2. Tổng hợp và độ nhạy giá

```text
delta_cost = scenario_cost − baseline_cost
delta_pct  = 100 × delta_cost / baseline_cost
component_share_pct = 100 × component_cost / total_cost
```

- Tất cả toán hạng phải cùng đơn vị tiền, basis và phạm vi. Baseline hoặc total bằng 0 → tỷ lệ tương ứng NULL, nhưng chênh lệch tuyệt đối vẫn có thể tính khi đủ dữ liệu.
- Tính ở từng ngày/component theo rate hiệu lực rồi SUM. Không dùng một giá trung bình đơn giản nhân tổng lượng khi mức tiêu thụ khác nhau theo ngày.
- Dùng decimal cho giá/chi phí; không làm tròn tiền từng dòng trước cộng. UI hiển thị hai chữ số thập phân; export giữ độ chính xác nội bộ và nêu đơn vị.
- Tổng danh mục nhiều khoang cần EUR tuyệt đối từng khoang hoặc cách gộp diện tích có nghĩa; không SUM EUR/m² rồi gọi là ngân sách tổng.

### 4.3. Missing và completeness

Theo dõi coverage lượng và coverage định giá riêng cho từng component. Ngày được định giá đầy đủ cần lượng accepted và rate hợp lệ, kể cả khi lượng bằng 0 để giữ contract nhất quán.

Cho xem tổng quan sát/đã định giá trên tập con với nhãn partial và số ngày. Tổng toàn kỳ chính thức và chênh lệch scenario toàn kỳ chỉ công bố khi đủ ngày/component, basis tương thích và không còn rate gap. Các trường không đủ điều kiện là NULL; không ngoại suy hoặc mặc định giá thiếu bằng 0.

## 5. Xử lý DE và dữ liệu phục vụ

`RawResources → ResourceDay/FactEnergyDay + TariffRate + AreaBasis → EnergyCostDay → EnergyCostPeriod + ScenarioComparison`.

- Dùng cùng dữ liệu năng lượng accepted và version với BR-04; không tính lại các quantity theo một policy riêng cho dashboard chi phí.
- Temporal join ngày với tariff/basis phải một-một trong từng component; kiểm tra số dòng trước/sau join để tránh nhân chi phí do rate chồng hiệu lực.
- Cache/materialization nếu có phải chứa snapshot, quality policy, metric, tariff, scenario và basis version trong định danh; đổi scenario không được trả kết quả cũ.
- Raw, baseline và kết quả scenario tách rõ. Mọi kết quả mở được quantity, rate, phép nhân và nguồn giá.

Output gồm: cycle/khoang/window, component costs, tổng nếu đủ điều kiện, đơn vị/basis, quantity và pricing coverage, baseline/scenario versions, delta/delta_pct, reason codes và metadata chung.

## 6. Luồng sử dụng và giao diện

1. Quản lý chọn vụ/khoang và xem profile lịch sử, biểu đồ đóng góp nhiệt/cao điểm/thấp điểm.
2. Mở một ngày hoặc component để xem lượng × giá và nguồn biểu giá.
3. Tạo scenario, ví dụ tăng cả hai giá điện 20%, giữ nguyên giá nhiệt và tiêu thụ.
4. Xem baseline và scenario cạnh nhau; giải thích chênh lệch theo component. Nhãn luôn ghi “chi phí theo kịch bản, giữ nguyên mức tiêu thụ”.
5. Lưu version và export để tái lập; mở lại scenario phải giữ chính xác giá, phạm vi và snapshot.

Không hiển thị chi phí scenario như hóa đơn thật. Không suy ra rằng chuyển điện từ cao điểm sang thấp điểm chắc chắn giữ nguyên sản lượng hoặc lượng dùng; v1 không mô phỏng thay đổi lịch vận hành.

## 7. Tiêu chí nghiệm thu và ví dụ chuẩn

Fixture tổng hợp một ngày: `Heat_cons=100 MJ/m²`, `ElecHigh=10 kWh/m²`, `ElecLow=5 kWh/m²`. Giả định cả ba cùng basis cố định đã xác minh. Giá theo profile lịch sử ở mục 3.

| ID | Trường hợp | Kết quả bắt buộc |
|---|---|---|
| AC-01 | Baseline fixture | Nhiệt=0.83, cao điểm=0.80, thấp điểm=0.20; tổng=1.83 EUR/m² |
| AC-02 | Tăng hai giá điện 20%, giữ nhiệt | Giá điện=0.096/0.048; chi phí điện=0.96+0.24; tổng=2.03; delta=0.20 EUR/m², tăng khoảng 10.928962% tổng |
| AC-03 | Đổi giá nhiệt sang đơn vị EUR/kWh tương đương | Dùng 0.02988 EUR/kWh; cùng lượng nhiệt cho chi phí 0.83, không thay thành 0.03 |
| AC-04 | Hai ngày điện cao điểm [10,30] kWh/m², giá [0.08,0.10] | Chi phí=3.80 EUR/m²; không dùng giá bình quân 0.09 ×40=3.60 |
| AC-05 | Hai rate cùng component chồng hiệu lực | Từ chối lưu/tính, không nhân đôi chi phí |
| AC-06 | Thiếu rate hoặc quantity một ngày | Báo coverage và partial; tổng chính thức/delta toàn kỳ NULL |
| AC-07 | Lượng đầy đủ nhưng tất cả bằng 0, giá đủ | Chi phí=0; delta tuyệt đối=0 nếu hai scenario cùng lượng; delta_pct NULL |
| AC-08 | Basis nhiệt và điện chưa hòa giải | Chi phí thành phần vẫn có đơn vị riêng; tổng NULL và reason; không SUM trực tiếp |
| AC-09 | Nhập NaN/giá âm/sai đơn vị hoặc yêu cầu đổi khung giờ cao điểm | Validation từ chối với thông báo cụ thể |
| AC-10 | Lưu scenario mới, mở lại baseline và version cũ | Kết quả từng version không đổi; baseline không bị ghi đè |
| AC-11 | Nạp lại snapshot hoặc refresh cùng scenario | Không nhân chi phí; lineage và phiên bản khớp |

Fixture là đáp án tính toán, không phải số chi phí đã đo của một khoang AGC. Kiểm tra decimal chính xác cho các tích hữu hạn trên; tỷ lệ cho phép sai số `1e-6` trước làm tròn.

## 8. Đánh giá, phụ thuộc và điều kiện hoàn thành

- **Độ đúng:** oracle tính độc lập quantity × rate, tests đơn vị và temporal join; đối soát tổng component với tổng kỳ.
- **Hiệu năng:** summary và tính lại scenario đáp ứng mục tiêu chung; lưu thời gian cache hit/miss và rebuild riêng.
- **Khai thác:** quản lý tạo được scenario điện +20%, chỉ ra vì sao tổng không tăng đúng 20% và mở được component giải thích.
- **Phụ thuộc:** Resources accepted, kho tariff/scenario có version, basis tài nguyên. Không phụ thuộc Harvest, TomQuality hoặc giá thị trường hiện tại.
- **Cần xác minh:** basis để cộng chi phí, phạm vi áp dụng biểu giá và quy tắc quản lý version. Giá lịch sử lấy đúng Economics; không cần người dùng phê duyệt lại để nhập profile nguồn, nhưng phải công bố phạm vi thí nghiệm.
- **Done when:** AC-01…AC-11 đạt; quantity/rate/basis trace được; baseline và scenario chạy trên dữ liệu thật; có UI/export, benchmark và demo. Nếu chưa đủ basis để cộng, chức năng chi phí thành phần đạt nhưng mục tiêu tổng chi phí chưa hoàn tất và phải ghi rõ.

## 9. Nguồn và quan hệ tài liệu

- [Yêu cầu môn học](../../DE_Requirements.pdf), trang 1–2, 5–6.
- `Economics.pdf`, trang 1–2: phạm vi chi phí và giá nhiệt/điện; `ReadMe.pdf`, mục Resources: đơn vị và khung giờ ElecHigh/ElecLow. Cả hai nằm trong [archive Kaggle AGC](https://www.kaggle.com/datasets/piantic/autonomous-greenhouse-challengeagc-2nd-2019/).
- [Nguồn AGC trong phân tích tham khảo](../ref/SmartGreenhouse_Case_Analysis.md#21-nguồn-dữ-liệu) và [grain tài nguyên ngày](../ref/SmartGreenhouse_Case_Analysis.md#23-grain-ba-mức-cùng-tồn-tại). Tổng quan không phải dictionary đơn vị Resources hay báo cáo kiểm chứng chi tiết: dùng ReadMe/Economics trong archive Kaggle ở trên và mục 2–4 của BR này. [Inventory T05](../implementation/data-inventory.md) đã đo file/header/dòng/time; [quyết định ngữ nghĩa](../implementation/data-decisions.md#5-profile-demo-và-biểu-giá-lịch-sử) xác nhận đơn vị giá nhiệt/điện và scope profile, basis tài nguyên vẫn chưa xác minh từ nguồn nhưng [user cho phép tổng chi phí theo AGC_SHARED_BASIS_V1 có nhãn](../implementation/data-decisions.md#7-br-04--quyết-định-basis-của-user), giữ NULL path khi thiếu/tắt giả định.
- [BR-04](br_04_energy-intensity-analysis.md) chia sẻ lượng năng lượng và basis; [BR-05](br_05_harvest-quality-tracking.md) chỉ cần nếu mở rộng kinh tế sau này. V1 không lấy doanh thu trừ năng lượng để gọi là lợi nhuận.
- Tài liệu này thay BR-06 rủi ro bệnh trong presentation; không cần nhãn bệnh hoặc xây mô hình dự đoán để nghiệm thu BR chi phí.
