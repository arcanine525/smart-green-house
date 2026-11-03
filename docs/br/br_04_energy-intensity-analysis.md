# BR-04 — Phân tích cường độ năng lượng trên sản lượng loại A

| Thuộc tính | Giá trị |
|---|---|
| Project | Group 29 — Smart Greenhouse |
| Phiên bản | 1.0 — 2026-11-03 |
| Trạng thái | Đặc tả đề xuất; KPI kWh/kg còn điều kiện xác minh diện tích |
| Người dùng chính | Quản lý trang trại |
| Người dùng liên quan | Kỹ sư vận hành, nhóm phân tích dữ liệu |
| Ưu tiên | BR chính có điều kiện; phải xử lý điều kiện trước nghiệm thu đầy đủ |
| Kế thừa | Giữ BR-04; đặc tả đúng đơn vị, diện tích, thời gian và drill-across |

## 1. Mục tiêu và yêu cầu nghiệp vụ

> Quản lý cần so sánh năng lượng sưởi và điện chiếu sáng sử dụng cho mỗi kg quả loại A trong cùng vụ, để xác định khoang cần rà soát hiệu quả sử dụng tài nguyên.

Người dùng phải thấy tổng năng lượng, hai thành phần nhiệt/điện, sản lượng A dùng làm mẫu số và các điều kiện cho phép so sánh. BR đo cường độ tài nguyên vật lý; chi phí và biến động giá thuộc [BR-06](br_06_energy-cost-scenarios.md).

**Trong phạm vi:** toàn vụ, từng khoang; lượng năng lượng được ghi nhận và tỷ số trên A khi dữ liệu đủ điều kiện; truy nguồn và giải thích phép tính.

**Ngoài phạm vi:** tổng điện toàn trang trại, primary energy, carbon footprint, suy luận tiết kiệm do can thiệp, dự báo sản lượng hoặc mặc định tỷ số tuần là hiệu suất của cây trong tuần đó.

Áp dụng [quy ước chung](br_01_climate-condition-monitoring.md#2-quy-ước-chung-cho-cả-sáu-br). Giao diện mặc định toàn vụ. Khoảng con chỉ là phân tích mô tả các lượng ghi nhận trong kỳ và không tham gia xếp hạng hiệu suất toàn vụ.

## 2. Dữ liệu đầu vào và grain

| Nguồn/cột | Ý nghĩa/đơn vị | Grain |
|---|---|---|
| `Resources.Heat_cons` | Nhiệt sưởi, MJ/m²/ngày | Khoang/vụ × ngày |
| `Resources.ElecHigh`, `ElecLow` | Điện chiếu sáng cao/thấp điểm, kWh/m²/ngày | Khoang/vụ × ngày |
| `Production.ProdA` | Sản lượng loại A tại lần ghi nhận, kg/m² | Khoang/vụ × ngày thu hoạch có ghi nhận |
| Metadata vụ | Bắt đầu/kết thúc, khoang, đội | Một vụ của một khoang |
| `AreaBasis` đề xuất | Diện tích, loại mẫu số, phạm vi hiệu lực, nguồn và trạng thái xác minh | Measure/context × khoang × thời gian |
| Metadata ingestion/quarantine | Snapshot đủ file, lỗi nguồn, bản ghi chưa phân giải | File/dòng/lần chạy |

Grain core giữ riêng `ResourceDay` và `Harvest`. Grain KPI chính là `(cycle_id, metric_version)`, một dòng mỗi khoang/vụ. Không ghép thu hoạch vào từng slot climate hoặc chỉ giữ các ngày có thu hoạch.

Dictionary nêu basis chuyển đổi Production là 62,5 m². Các diện tích 96 m² và 76,8 m² xuất hiện ở các mô tả khác; chúng không tự chứng minh basis của năng lượng. Không dùng một trường `area_m2` chung cho mọi mục đích.

## 3. Điều kiện sẵn sàng công bố KPI

Hệ thống phải quản lý riêng trạng thái số liệu và trạng thái ngữ nghĩa:

| Điều kiện | Bằng chứng cần có | Nếu chưa đạt |
|---|---|---|
| Basis nhiệt/điện/sản lượng | Giá trị diện tích hoặc bằng chứng cùng basis, nguồn và phạm vi hiệu lực | Không công bố kWh/kg chính thức |
| Energy đủ kỳ | Tất cả ngày kỳ vọng có ba measure accepted | Hiển thị tổng quan sát riêng, KPI chính NULL |
| Harvest đủ snapshot | Đủ file, parse thành công, không còn record liên quan chưa phân giải theo BR-05 | Hiển thị A accepted và lỗi; KPI chính NULL |
| Mẫu số dương | Tổng khối lượng A > 0 | KPI NULL với `zero_class_a_production` |
| Cùng phạm vi so sánh | Cùng cửa sổ vụ, định nghĩa A, quality policy và basis hợp lệ | Không xếp hạng chung |

“Đủ snapshot” là đầy đủ đối với bộ dữ liệu đã công bố, không bảo đảm mọi hoạt động ngoài thực tế đều được ghi nhận.

Ngày thu hoạch ngoài vụ của Reference phải giữ nguyên nguồn và quarantine. Nếu chưa rõ record thuộc ngày/vụ nào, sự không chắc chắn này ảnh hưởng đến tính đầy đủ của vụ Reference; không bỏ record rồi coi vụ hoàn chỉnh. Việc sửa phải có bằng chứng, audit và version.

## 4. Công thức KPI

### 4.1. Chuẩn hóa đơn vị và tính lượng tuyệt đối

```text
heat_kwh_per_m2(d) = Heat_cons(d) / 3.6

E_heat_kwh = Σ [Heat_cons(d) / 3.6 × A_heat(d)]
E_high_kwh = Σ [ElecHigh(d) × A_electricity(d)]
E_low_kwh  = Σ [ElecLow(d)  × A_electricity(d)]
E_total_kwh = E_heat_kwh + E_high_kwh + E_low_kwh

Y_A_kg = Σ [ProdA(h) × A_production(h)]

energy_intensity_kwh_kg_a = E_total_kwh / Y_A_kg
heat_intensity_kwh_kg_a   = E_heat_kwh / Y_A_kg
light_intensity_kwh_kg_a  = (E_high_kwh + E_low_kwh) / Y_A_kg
```

`d` chạy qua toàn bộ ngày của vụ, kể cả trước lần thu hoạch đầu. `h` chạy qua các record thu hoạch accepted trong cùng vụ. Diện tích phải áp dụng đúng hiệu lực tại ngày tương ứng; không dùng diện tích mới nhất cho toàn lịch sử.

Chỉ rút gọn bằng tỷ số các tổng per-m² nếu có bằng chứng mọi measure dùng cùng basis và diện tích chung không đổi trong cửa sổ. Hai diện tích bằng số nhưng khác định nghĩa chưa đủ để khẳng định tương thích; nếu cùng basis thay đổi theo ngày, phải dùng công thức có trọng số diện tích.

Mẫu số chỉ A, không A+B. Đây là convention cường độ trên sản phẩm A: toàn bộ năng lượng trong phạm vi được chia cho sản lượng A, không phải bằng chứng B không tiêu thụ năng lượng.

### 4.2. Thiếu dữ liệu và cộng gộp

- Tính `energy_coverage_pct = 100 × số ngày đủ cả ba thành phần accepted / số ngày kỳ vọng`; thêm coverage từng thành phần và danh sách ngày thiếu.
- Không scale tổng năng lượng theo coverage để lấp ngày thiếu. Không dùng giao các ngày có cả Energy và Harvest làm phạm vi tính.
- Nếu điều kiện ở mục 3 chưa đạt, các tổng quan sát vẫn có thể xem nhưng KPI chính NULL và `comparison_eligible=false`; mọi lý do thiếu được trả cùng lúc.
- Tổng nhiều khoang phải là `Σ energy_kwh / Σ production_a_kg` trên các khoang đủ điều kiện; không lấy trung bình các tỷ số. UI phải nêu tập khoang được bao gồm và bị loại.
- Đối với khoảng con không có thu hoạch, không báo năng lượng/kg bằng 0. Không diễn giải một tuần thu hoạch nhiều là năng lượng tuần đó đã tạo ra toàn bộ số quả ấy.

## 5. Xử lý DE và dữ liệu phục vụ

```text
RawResources → ResourceDay → FactEnergyDay → EnergyCycleAggregate
RawProduction → Harvest → FactHarvest → HarvestCycleAggregate
AreaBasis + Cycle + Quality status → kiểm tra khả năng tính/so sánh
Hai aggregate theo cycle → EnergyIntensityCycle
```

Mỗi aggregate có tối đa một dòng/cycle trước khi ghép. Dùng outer/left join phù hợp để không làm biến mất khoang thiếu một nguồn; trả reason thay vì loại âm thầm.

Output tối thiểu: cycle/khoang/đội, cửa sổ, energy components, tổng A, các basis và bằng chứng, coverage, trạng thái harvest, KPI thành phần/tổng, `comparison_eligible`, danh sách lý do và metadata chung. Cho mở bảng đóng góp từng ngày/từng harvest để truy nguồn.

## 6. Luồng sử dụng và giao diện

1. Quản lý chọn vụ; xem danh sách sáu khoang và trạng thái khả năng tính.
2. Với khoang đủ điều kiện, xem nhiệt, điện, kg A và kWh/kg A cạnh nhau.
3. Mở một KPI để xem công thức, diện tích, ngày thiếu và record bị cách ly.
4. Chỉ bật sắp hạng chính thức cho tập khoang đủ điều kiện và cùng phạm vi; không công bố “hạng 1/6” khi có khoang bị loại chưa phân giải.
5. Export giữ cả dòng chưa tính được cùng lý do; không chỉ xuất các số thành công.

Nếu cung cấp phân tích độ nhạy theo diện tích giả định, phải là chế độ scenario riêng, ghi rõ giả định và không ghi đè metadata đã xác minh. Chế độ này không thay thế nghiệm thu KPI thực nghiệm.

## 7. Tiêu chí nghiệm thu và ví dụ chuẩn

Fixture tổng hợp hai ngày, không phải AGC. Basis đã xác minh trong fixture: nhiệt/điện 100 m², Production 50 m², giữ ổn định trong kỳ.

| Ngày | Heat_cons (MJ/m²) | ElecHigh (kWh/m²) | ElecLow (kWh/m²) | ProdA (kg/m²) |
|---|---:|---:|---:|---:|
| 1 | 3.6 | 1 | 0 | Không có lần thu hoạch, snapshot xác nhận đầy đủ |
| 2 | 7.2 | 0 | 1 | 2 |

| ID | Trường hợp | Kết quả bắt buộc |
|---|---|---|
| AC-01 | Toàn bộ fixture | Nhiệt=300 kWh, điện=200 kWh, tổng=500 kWh, A=100 kg, KPI=5 kWh/kg A |
| AC-02 | Cố chia trực tiếp tổng per-m² | Không chấp nhận 5/2=2.5; phải dùng basis khác nhau |
| AC-03 | Kiểm tra ngày không thu hoạch | Vẫn tính 200 kWh ngày 1; không inner join làm mất ngày này |
| AC-04 | Thêm ProdB=1 kg/m² ngày 2 | KPI trên A vẫn là 5; không đổi mẫu số thành A+B |
| AC-05 | Thiếu một thành phần năng lượng ngày 1 | KPI chính NULL, coverage ngày đủ thành phần=50%, có reason |
| AC-06 | Basis năng lượng chưa xác minh | KPI chính NULL; tổng trên đơn vị nguồn vẫn xem được; không xếp hạng |
| AC-07 | A=0 hoặc chưa có dữ liệu thu hoạch dùng được | NULL với lý do tương ứng, không chia cho 0 |
| AC-08 | Một record harvest ngoài vụ chưa phân giải | KPI chính NULL cho cycle bị ảnh hưởng; hiện record/quarantine |
| AC-09 | Hai khoang đủ điều kiện có (E,Y)=(500,100) và (100,50) | Tỷ số gộp=600/150=4; không lấy trung bình 5 và 2 thành 3.5 |
| AC-10 | Rerun cùng snapshot; join aggregate | Một dòng/cycle và kết quả không đổi |

Sai số số thực `1e-6` trước làm tròn. Cần thêm kiểm tra diện tích hiệu lực theo ngày nếu triển khai basis thay đổi theo thời gian.

## 8. Đánh giá, phụ thuộc và điều kiện hoàn thành

- **Độ đúng:** đối soát riêng tổng energy/harvest trước ghép, fixture khác diện tích và trường hợp không thu hoạch; kiểm tra cardinality.
- **Hiệu năng:** đo hai aggregation, bước ghép và truy vấn KPI; so sánh core query với mart trên cùng rule/snapshot, không mặc định khác biệt là ưu thế của toàn kiến trúc.
- **Khai thác:** quản lý giải thích được tử số/mẫu số và chỉ ra khoang nào chưa được so sánh vì lý do nào.
- **Phụ thuộc:** cùng dữ liệu thu hoạch accepted và trạng thái completeness với [BR-05](br_05_harvest-quality-tracking.md); parser Resources; AreaBasis có evidence.
- **Điều kiện chưa giải quyết:** basis năng lượng/Production và record Reference ngoài vụ. Tài liệu này không tự xác nhận chúng chỉ bằng việc đặt công thức.
- **Done when:** AC đạt; basis cho phạm vi công bố được xác minh; kết quả trên dữ liệu thật truy nguồn được; có UI/export và đánh giá. Hiển thị đúng trạng thái chưa sẵn sàng là một AC cần thiết nhưng chưa đủ để gọi BR kWh/kg trên dữ liệu thật đã hoàn tất.
- **Phương án phạm vi:** nếu không giải quyết được basis trước mốc triển khai do nhóm đặt, cân nhắc đổi BR bắt buộc sang DLI/ánh sáng bằng quyết định và đặc tả riêng. Không tự coi BR này đã đạt hoặc âm thầm thay tên bằng một báo cáo kWh/m².

## 9. Nguồn và quan hệ tài liệu

- [Yêu cầu môn học](../../DE_Requirements.pdf), trang 1–2 và 6.
- [Grain tài nguyên/thu hoạch trong phân tích tham khảo](../ref/SmartGreenhouse_Case_Analysis.md#23-grain-ba-mức-cùng-tồn-tại) và [phản biện phép ghép BR-04](../ref/SmartGreenhouse_Case_Analysis.md#44-vấn-đề-cần-sửa). Các mục này không xác minh diện tích hoặc record Reference ngoài vụ; [inventory T05](../implementation/data-inventory.md) đã đo file/ngày nguồn, basis và record bất thường được xử lý ở T03 từ ReadMe và dữ liệu gốc. Metric contract cũ không có trong repo; [công thức hiện hành](#4-công-thức-kpi) của BR này là cơ sở triển khai.
- `ReadMe.pdf`, mục Production và Resources, trong [dataset Kaggle](https://www.kaggle.com/datasets/piantic/autonomous-greenhouse-challengeagc-2nd-2019/).
- [Kimball: multipass SQL](https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/dimensional-modeling-techniques/multipass-sql/): tổng hợp từng fact trước khi drill-across để tránh nhân bản số liệu.
- [BR-06](br_06_energy-cost-scenarios.md) tái dùng năng lượng accepted nhưng có đơn vị giá và mục đích ngân sách riêng.
