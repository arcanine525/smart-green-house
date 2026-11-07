# Ma trận truy vết yêu cầu Smart Greenhouse

Cập nhật: 2026-11-07. Sáu BR có baseline PostgreSQL và backend mặc định ClickHouse
được kiểm riêng theo engine. Các bảng ở mục 2–7 giữ kết quả PostgreSQL lịch sử;
không dùng số đo đó làm bằng chứng ClickHouse. Full parity, benchmark và
restore ClickHouse đã đạt trong phạm vi công bố. Walkthrough ClickHouse T51
đã đạt; cutover T39 đã kiểm và chuyển mặc định. Gói report/slides/app và
video thuyết trình có điều kiện riêng ở T29–T30/T53.

## 1. Nguồn và cách đọc

- Đặc tả hiện hành là [sáu BR](../br/); [đề môn](../../DE_Requirements.md#23-yêu-cầu-cụ-thể) yêu cầu correctness, performance và effectiveness.
- Triển khai local theo [ADR-003](../adr/003-clickhouse-warehouse.md): raw → Python batch → ClickHouse core/marts → Streamlit/Plotly; sensor replay qua MQTT và Spark Structured Streaming. ADR-001 và các bảng PostgreSQL giữ bối cảnh lịch sử. [Sơ đồ](../architecture/README.md) cần đối chiếu với trạng thái thực tế khi làm báo cáo.
- Nguồn lịch sử là AGC 2nd edition đã khóa checksum: [snapshot](dataset-snapshot.md), [inventory](data-inventory.md), [quyết định dữ liệu](data-decisions.md).
- Đọc từng artifact để biết snapshot, publication, version và điều kiện; các lần đo khác nhau không tự động cùng publication. Thời gian nguồn chưa xác minh timezone, NULL khác 0; basis `AGC_SHARED_BASIS_V1` là giả định chưa xác minh.
- **Đạt có phạm vi** nghĩa là kiểm tra được nêu trong artifact đã pass, không khẳng định đúng cho mọi dataset. **Một phần** chỉ rõ mục tiêu/bằng chứng chưa đạt hoặc chưa có.

## 2. Truy vết sáu BR

| BR hiện hành | Người dùng | Nguồn AGC / cột chính và grain | Biến đổi / dữ liệu phục vụ | Màn hình / thao tác cần nghiệm thu | AC trong đặc tả hiện hành | Bằng chứng theo phạm vi | Sensor extension |
|---|---|---|---|---|---|---|---|
| [BR-01 — Đợt lệch khí hậu](../br/br_01_climate-condition-monitoring.md) | Người vận hành; kỹ sư canh tác, quản lý | `GreenhouseClimate.csv`: `%time`, `Tair`, `CO2air`; requested `t_heat_sp`, `t_vent_sp`, `co2_sp`; realized `t_heat_vip`, `t_ventlee_vip`, `co2_vip`. Khoang/vụ × slot 5 phút. | Raw → `ClimateObservation` + quality/rule → `FactClimate`, `ClimateEpisode`, `ClimatePeriodSummary`. Tách requested/realized; tính duration, coverage, OR trên jointly valid; gap/unknown/đổi rule ngắt episode; giữ thời điểm gốc khi cắt cửa sổ. | Chọn vụ/khoang/ngày/basis; tìm đợt dài nhất; mở actual/cận/unknown và nguồn; CSV cùng bộ lọc/version. | [BR-01/AC-01…AC-10](../br/br_01_climate-condition-monitoring.md#7-tiêu-chí-nghiệm-thu-và-ví-dụ-chuẩn): fixture out 15 phút / valid 25, hai đợt 5/10 phút; không fallback SP/VIP, không cộng trùng OR, không nối qua gap. | **Correctness:** [BR-01](../evaluation/br_01.md), [tổng hợp](../evaluation/correctness.md); **performance:** [benchmark](../evaluation/performance.md); **effectiveness:** [video Playwright; 6/6 luồng đạt](../evaluation/usability.md). | Replay BR-01: [sensor acceptance](../evaluation/sensor-replay.md), scope riêng. |
| [BR-02 — Tưới và drain](../br/br_02_irrigation-drainage-monitoring.md) | Người vận hành tưới; kỹ sư canh tác, quản lý | `Resources.csv`: `%Time `, `Irr`, `Drain`, khoang/vụ × ngày. `GreenhouseClimate.Cum_irr`, `water_sup` và `Weather.Rain` chỉ là bối cảnh mở rộng có điều kiện. | Raw → `ResourceDay` + `AreaBasis`/quality → `FactIrrigationDay`, `IrrigationPeriodSummary`. Tổng observed riêng; paired sums và ratio of sums khi basis tương thích; coverage từng measure/paired. Không cộng counter vào tổng ngày. | Chọn khoang/kỳ; chart ngày và bảng tuần; mở tử/mẫu, ngày ngoài rule, dòng nguồn; xuất CSV. | [BR-02/AC-01…AC-10](../br/br_02_irrigation-drainage-monitoring.md#7-tiêu-chí-nghiệm-thu-và-ví-dụ-chuẩn): fixture 40/14 L/m² → 35%; partial, mẫu số 0, drain 120%, rule chưa cấu hình, rerun/conflict và basis chưa rõ. | **Correctness:** [BR-02](../evaluation/br_02.md), [tổng hợp](../evaluation/correctness.md); **performance:** [benchmark](../evaluation/performance.md); **effectiveness:** [video Playwright; 6/6 luồng đạt](../evaluation/usability.md). | Giữ grain ngày; không cộng lịch sử và replay. |
| [BR-03 — EC/pH drain](../br/br_03_drain-nutrition-monitoring.md) | Kỹ sư canh tác; người vận hành tưới/dinh dưỡng | `GreenhouseClimate.csv`: `%time`, `EC_drain_PC`, `pH_drain_PC`; khoang/vụ × slot 5 phút, context drain process computer. `Resources.Irr/Drain` làm bối cảnh ngày; Grodan/lab giữ context riêng nếu mở rộng. | Raw → `DrainObservation` + quality/rule → `FactDrainCondition`, `DrainEpisode`, `DrainPeriodSummary`. Quality/coverage riêng EC/pH; ngoài mục tiêu tách suspect; OR cùng tập hợp lệ; không nhân nước ngày theo số slot. | Chọn tuần/profile; tìm đợt EC dài nhất; xem pH suspect, unknown, rule demo và bối cảnh tưới; mở lineage, xuất CSV. | [BR-03/AC-01…AC-10](../br/br_03_drain-nutrition-monitoring.md#7-tiêu-chí-nghiệm-thu-và-ví-dụ-chuẩn): EC out 15 phút, pH out 5, OR out 15 trên 20 phút jointly valid; ngắt gap/rule; không nội suy lab thành duration. | **Correctness:** [BR-03](../evaluation/br_03.md), [tổng hợp](../evaluation/correctness.md); **performance:** [benchmark](../evaluation/performance.md); **effectiveness:** [video Playwright; 6/6 luồng đạt](../evaluation/usability.md). | Replay BR-03: [sensor acceptance](../evaluation/sensor-replay.md), scope riêng. |
| [BR-04 — kWh/kg loại A](../br/br_04_energy-intensity-analysis.md) | Quản lý; kỹ sư vận hành, nhóm phân tích | `Resources.csv`: `Heat_cons` (MJ/m²/ngày), `ElecHigh`, `ElecLow` (kWh/m²/ngày); `Production.csv`: `ProdA` (kg/m²/record thu hoạch). Metadata vụ, basis theo measure/thời gian, manifest/quarantine. | `ResourceDay` → `FactEnergyDay` → aggregate cycle; `Harvest` → `FactHarvest` → aggregate cycle; ghép hai aggregate một dòng/cycle. Đổi MJ/3.6, áp đúng diện tích; toàn bộ ngày năng lượng / kg A, giữ completeness như BR-05; gate basis/coverage trước công bố/xếp hạng. | Chọn vụ; xem nhiệt/điện/kg A và khả năng tính từng khoang; mở công thức, basis, ngày thiếu/quarantine; export giữ dòng NULL và reason. | [BR-04/AC-01…AC-10](../br/br_04_energy-intensity-analysis.md#7-tiêu-chí-nghiệm-thu-và-ví-dụ-chuẩn): fixture 500 kWh/100 kg A = 5; giữ năng lượng ngày không thu hoạch; khác basis, thiếu thành phần, record ngoài vụ và tỷ số gộp. | **Correctness:** [BR-04](../evaluation/br_04.md), [tổng hợp](../evaluation/correctness.md); **performance:** [benchmark](../evaluation/performance.md); **effectiveness:** [video Playwright; 6/6 luồng đạt](../evaluation/usability.md). | Không biến Resources/Production thành dữ liệu sensor. |
| [BR-05 — Thu hoạch A/B](../br/br_05_harvest-quality-tracking.md) | Quản lý sản xuất/trang trại; kỹ sư canh tác | `Production.csv`: `%time`, `ProdA`, `ProdB`; grain record thu hoạch thưa, giữ danh tính file/dòng. `Weight_fruits_ClassA/B` là mẫu, không thay ProdA/B. Manifest, correction/quarantine và production area basis. | Raw → `Harvest` + correction/quarantine → `FactHarvest`, `HarvestPeriodSummary`, `HarvestCycleSummary`. Tổng observed/paired; tỷ lệ có trọng số khối lượng; lũy kế từ đầu vụ; completeness theo snapshot. Record Reference ngoài vụ chưa rõ phải cách ly, không tự sửa năm. | Xem A/B theo lần thu/tuần, tỷ lệ B, lũy kế; tìm kỳ cần điều tra, mở record và trạng thái incomplete; CSV cùng version. | [BR-05/AC-01…AC-10](../br/br_05_harvest-quality-tracking.md#7-tiêu-chí-nghiệm-thu-và-ví-dụ-chuẩn): A=3, B=0.2 → A share 93.75%; quarantine/completeness, missing B, ngày không event, rule và lũy kế; A/completeness khớp BR-04. | **Correctness:** [BR-05](../evaluation/br_05.md), [tổng hợp](../evaluation/correctness.md); **performance:** [benchmark](../evaluation/performance.md); **effectiveness:** [video Playwright; 6/6 luồng đạt](../evaluation/usability.md). | Giữ grain record thu hoạch, không nội suy sensor. |
| [BR-06 — Chi phí và biểu giá](../br/br_06_energy-cost-scenarios.md) | Quản lý trang trại/ngân sách; người vận hành, nhóm phân tích | `Resources.csv`: `Heat_cons`, `ElecHigh`, `ElecLow`; khoang/vụ × ngày × component. `Economics.pdf` cung cấp giá lịch sử, `ReadMe.pdf` cung cấp đơn vị/khung giờ; `TariffRate`, `CostScenario` là cấu hình ứng dụng có version, không phải CSV có sẵn. | Accepted quantities dùng chung BR-04 + tariff/basis hiệu lực → `EnergyCostDay`, `EnergyCostPeriod`, `ScenarioComparison`. Decimal quantity × rate theo ngày/component, chặn overlap/gap; giữ lượng/snapshot khi đổi giá; chỉ cộng khi basis đủ điều kiện. | Xem baseline, đóng góp component và lượng × giá; tạo/lưu/mở scenario điện +20%, xem delta và xuất CSV; chặn đổi khung giờ nguồn. | [BR-06/AC-01…AC-11](../br/br_06_energy-cost-scenarios.md#7-tiêu-chí-nghiệm-thu-và-ví-dụ-chuẩn): fixture baseline 1.83 → scenario 2.03 EUR/m², delta 0.20; đơn vị giá nhiệt, temporal join, gap, basis, validation, version/rerun. | **Correctness:** [BR-06](../evaluation/br_06.md), [tổng hợp](../evaluation/correctness.md); **performance:** [benchmark](../evaluation/performance.md); **effectiveness:** [video Playwright; 6/6 luồng đạt](../evaluation/usability.md). | Scenario giá là phân tích, không phải hóa đơn hay telemetry. |


## 3. Bằng chứng PostgreSQL và giới hạn của baseline

| Hạng mục | Correctness | Performance | Effectiveness / hạn chế |
|---|---|---|---|
| BR-01 khí hậu | [AC và raw reconciliation](../evaluation/br_01.md), 12/12 phép đối soát KPI/basis | **Benchmark gốc chưa đạt:** Hybrid 4.060,81 ms, Kimball 3.028,13 ms p95. [Follow-up sau tối ưu](../evaluation/rule-engine-optimization.md): Hybrid 1.770,93 ms, dưới 2.000 ms trong lượt đo này; chưa đo lại Kimball. | Video Playwright đạt; chưa có người thật. |
| BR-02 nước | [AC và raw reconciliation](../evaluation/br_02.md) | p95 Hybrid 76,39 ms ở workload đo. | Video Playwright đạt; basis diện tích chưa xác minh. |
| BR-03 drain | [AC và raw reconciliation](../evaluation/br_03.md) | p95 Hybrid 1.959,90 ms ở workload đo, gần ngưỡng. | Video Playwright đạt; rule ngưỡng là demo/chưa xác minh. |
| BR-04 năng lượng | [AC và raw reconciliation](../evaluation/br_04.md) | p95 Hybrid 19,82 ms ở workload đo. | Video Playwright đạt; basis giả định, Reference incomplete. |
| BR-05 thu hoạch | [AC và raw reconciliation](../evaluation/br_05.md) | p95 Hybrid 3,33 ms ở workload đo. | Video Playwright đạt; Reference có quarantine ngoài vụ. |
| BR-06 chi phí | [AC và oracle](../evaluation/br_06.md) | p95 Hybrid 9,19 ms ở workload đo. | Video Playwright đạt; giá lịch sử/scenario và basis không phải dự báo/hóa đơn. |
| Sensor BR-01/03 | [19 kiểm tra sensor/fault/recovery](../evaluation/correctness.md), [parity và trace](../evaluation/sensor-replay.md). | p50 1.212,57 ms, p95 1.491,60 ms ở một lượt 30 event synthetic; không suy rộng thành SLA. | [UI replay](../evaluation/sensor-replay-ui.md); chưa có đánh giá người dùng. |

Chi tiết cấu hình, phân biệt cache, workload và kết quả Kimball ở
[performance](../evaluation/performance.md). [Correctness](../evaluation/correctness.md)
ghi 419 backend và 97 UI test của lượt đầu; lượt sau tối ưu có 420 backend và 97 UI test. [Usability](../evaluation/usability.md)
ghi sáu video Playwright (93 bước, 19 file xuất) cùng AppTest trước đó, protocol tùy chọn và giới hạn chưa có quan sát con người.

## 4. Lịch sử mã BR và thứ tự ưu tiên tài liệu

| Nguồn lịch sử | BR-05 / BR5 trong nguồn đó | BR-06 / BR6 trong nguồn đó | Khác với bộ cuối kỳ |
|---|---|---|---|
| [Presentation bản nộp](../../Submit/DATA_WAREHOUSE_DESIGN_APPROACHES_GROUP_29.pptx.pdf#page=16) và [bản ở root](../../DATA_WAREHOUSE_DESIGN_APPROACHES_GROUP_29.pptx.pdf#page=16), trang/slide 16 | Năng suất và khí hậu: khoang năng suất cao có ít giờ stress hơn không; `FactHarvest` đối chiếu `FactClimate`, không kết luận nhân quả. | Giờ rủi ro bệnh: proxy `RH ≥ 85%` và `15°C ≤ Tair ≤ 25°C`; không phải nhãn cây bị bệnh. | BR-05 hiện hành đo sản lượng/cơ cấu A/B và lũy kế. BR-06 hiện hành đo chi phí nhiệt/điện và độ nhạy biểu giá; không phải BR stress/rủi ro bệnh. |
| [Presentation tham khảo](../ref/GroupID_DE_Presentation.pdf#page=18), trang/slide 18; [case analysis dẫn bộ mã cũ ở bảng cột](../ref/SmartGreenhouse_Case_Analysis.md#24-các-cột-quan-trọng) | Năng suất vs khí hậu, kg/m² đối chiếu giờ stress; bảng cột gắn Tair/PAR/harvest với mã 05. | Giờ rủi ro bệnh, RH/nhiệt độ; bảng cột gắn Rhair/HumDef với mã 06. | Chỉ là lịch sử ý tưởng; bảng cột/tên mart/SQL ở tài liệu tham khảo không thay công thức, cột nguồn hoặc AC của sáu file BR hiện hành. |
| [Nghiên cứu SmartGreenhouse ban đầu, mục 7.3](../ref/DWH_Design_Approaches_SmartGreenhouse.md#73-phân-tích-6-business-requirements--yêu-cầu-kiến-trúc) | BR5: go-live trong 3–4 tuần, phát triển cuốn chiếu. | BR6: lưu nguyên tử bất biến 3–5 năm cho VietGAP/ML. | Đây còn là một bộ đánh số cũ khác, thiên về kiến trúc. Không đồng nhất BR5/BR6 ở đây với presentation hoặc bộ BR cuối kỳ. |

Nguồn triển khai hiện hành là [BR-05 thu hoạch](../br/br_05_harvest-quality-tracking.md) và [BR-06 chi phí](../br/br_06_energy-cost-scenarios.md), cùng bốn BR còn lại trong mục 2; ADR và thiết kế hiện tại điều chỉnh lựa chọn công nghệ. Không lấy đề xuất BigQuery, Open-Meteo, tự gắn UTC, ngưỡng nông học hoặc mã BR cũ trong `docs/ref/` ghi đè các hợp đồng này. Tương quan stress và proxy rủi ro có thể là ý tưởng mở rộng, không phải hai chức năng bắt buộc thay cho BR-05/06 hiện tại.

## 5. Điều kiện chốt

T26 đã hoàn thành: [sáu video và bằng chứng](../evaluation/assets/playwright-t26/README.md) theo phạm vi user chốt.
Bằng chứng correctness, benchmark và walkthrough đã được liên kết ở trên. Để
chốt toàn bộ project, cần ghi rõ
bất kỳ kết quả thiếu hoặc không đạt, hoàn thiện bộ chạy lại/offline và bốn sản
phẩm nộp. Kết quả BR-01 vượt mục tiêu ở benchmark gốc và kết quả follow-up sau tối ưu
được giữ riêng, không xóa số chưa đạt. Bằng chứng mới cần ghi nguồn, version, lệnh, kết quả và giới hạn thay vì
đổi trạng thái bằng suy đoán. Bản PostgreSQL là baseline đã nghiệm thu; theo quyết định chuyển backend,
T37–T52 có gate ClickHouse riêng trước cutover và tài liệu cuối.

Theo quyết định user ngày 2026-11-07, đánh giá người thật là mở rộng tùy chọn, không chặn T26. Walkthrough tự động chỉ chứng minh luồng và kết quả được kiểm tra, không chứng minh khả năng sử dụng của người thật.


## 6. Chốt nghiệm thu chức năng — T27, ngày 2026-11-07

**Đạt trong phạm vi bằng chứng đã công bố** cho sáu BR và sensor extension
BR-01/03. Main đã đối chiếu các ma trận AC trong báo cáo từng BR, oracle raw,
kết quả test backend/UI/recovery, benchmark và sáu video Playwright. Danh mục
artifact cùng SHA-256 và HEAD được review:
[biên bản máy đọc được](../evaluation/assets/acceptance/review-20261107.json).
Đây là lượt rà soát bằng chứng hiện có, không phải một lượt chạy mới toàn bộ test.

Không còn lỗi chức năng đã biết chưa xử lý trong các luồng nghiệm thu này.
BR-01 gốc không đạt p95 được giữ nguyên trong báo cáo; follow-up sau tối ưu đạt
ở workload đã đo, chưa là SLA. Basis diện tích/timezone chưa xác minh, ngưỡng
demo, Reference quarantine và giới hạn sensor 30 slot vẫn áp dụng. Walkthrough
tự động không đo hiểu biết hoặc phản hồi của người thật.

T27 được đóng theo tiêu chí sáu BR/sensor có bằng chứng và giới hạn được mô tả.
T28 (chạy lại/restore), T29 (tài liệu cuối) và T30 (gói nộp) vẫn có điều kiện
riêng; nghiệm thu chức năng không đồng nghĩa đã hoàn thành bàn giao toàn project.


## 7. Chốt bộ chạy lại — T28, ngày 2026-11-07

**Đạt:** [full AGC/offline và restore](../evaluation/assets/full-local-repro/README.md)
trên clone riêng, DB trống và image đã chuẩn bị; 420 backend/100 UI pass;
71 bảng cùng ZIP/raw/sáu volume khớp; MQTT/WAL/checkpoint phục hồi 30 event AGC,
UI lịch sử và replay kiểm thành công. Đây là phạm vi container offline và
sensor window đã nêu, không khẳng định host ngắt Internet hay replay toàn vụ.
T29–T30 tiếp tục là phần tài liệu và sản phẩm nộp cuối.

## 8. Gate ứng viên ClickHouse — T51

Gate này kiểm backend ClickHouse riêng với baseline PostgreSQL ở các mục
trước. Publication lịch sử `4b08bd51c092465783304f887d766d47` có cùng snapshot,
công thức và tolerance đã khóa; source/config versions và lineage được kiểm
qua native adapters, không truy vấn PostgreSQL từ app.

| BR | Dữ liệu phục vụ ClickHouse | Bằng chứng triển khai và AC |
|---|---|---|
| BR-01 | `threshold_measure`, bundle `climate`; rule/version và episode qua pure services | [Climate/drain](clickhouse-thresholds.md), [full parity](../evaluation/clickhouse-parity.md) |
| BR-02 | `water_measure` theo cycle/ngày/measure và basis/quality | [Water](clickhouse-water.md), paired denominator/coverage và raw lineage |
| BR-03 | `threshold_measure`, bundle `drain`; giữ context EC/pH process computer | [Climate/drain](clickhouse-thresholds.md), episode/rule/gap/unknown và nước ngày |
| BR-04 | `energy_measure` và `harvest_measure` được tổng hợp riêng rồi ghép | [Energy/cost](clickhouse-energy-cost.md), MJ/3,6, kg A và NULL/coverage/basis |
| BR-05 | `harvest_measure` và `harvest_state` giữ completeness/quarantine | [Harvest](clickhouse-harvest.md), share B theo record/ngày/tuần và lineage |
| BR-06 | Cùng quantities BR-04, tariff/scenario envelope bất biến trong `configuration_envelope` | [Energy/cost](clickhouse-energy-cost.md), lưu/mở lại scenario, Decimal/rates/request conflict |

Lượt kiểm sau sửa cold proof có **91 test, 82 chạy đạt/chín skip** cho harness
heavy riêng; full AGC đạt **6/6 BR, 96 CSV, 30 lineage, 18 nhóm threshold và
67.829 episode**. Các [fault/sensor](clickhouse-sensor.md),
[benchmark paired](../evaluation/clickhouse-performance.md) và
[offline/restore](../evaluation/clickhouse-local-repro.md) có phạm vi, phiên bản
và giới hạn riêng; không cộng số test qua nhiều suite thành số duy nhất.

Walkthrough ClickHouse đạt bảy video/109 bước/21 file xuất; đầy đủ decode, player
và main visual review. [Metadata](../evaluation/assets/clickhouse-walkthrough/README.md). [Biên bản ứng viên](../evaluation/clickhouse-acceptance.md)
ghi lỗi RAM/HTTP stream đã phát hiện, regression, cấu hình telemetry và giới
hạn vận hành. Chưa dùng gate này để tuyên bố đã cutover hoặc gói nộp cuối đã
đủ; T39, T52, T29–T30 còn điều kiện riêng. Video thuyết trình tổng thể T53 do
user tự thực hiện offline; sáu walkthrough BR không thay thế video đó.

## 9. Backend mặc định và rollback — T39/T52

[Cutover](../evaluation/clickhouse-cutover.md) đạt: journal62 intents được đối soát,
315 config envelopes,178 scenario versions, bảy replay pins gồm partial; toàn bộ
metadata clock/cursor/checksum khớp. Correction +1 Irr ngày02/03/2020 là diễn tập
có audit, không sửa raw. Native publication mới `5d455686a06649e789908b9708a9d436`;
PostgreSQL rollback `c51402d9d25f45c182140b072397de78`. Full6BR/96CSV/30lineage/
18threshold groups/67.829episodes đạt sau reverse import; old pin giữ nguyên.
Một scenario family trùng ID từ hai lineage được namespace/map bất biến, không
ghi đè scenario PG. Chỉ fixture agent được dựng lại, database/volumes gốc giữ nguyên.

100 native tests (91 chạy/chín heavy skips),24PG cost và8routing pass; các suite
có thể giao nhau, không cộng thành tổng. Native UI chín màn hình pass khi PG dừng.
Backup PG đọc toàn bộ archive; quay lại native và journal active đã kiểm.
HTTP switch9,250s và KPI cold198,954s là hai mốc riêng; không gọi9s là tổng downtime.

[Mười sơ đồ](../architecture/README.md), [README local](../../readme.md) và
[runbook](clickhouse-cutover-rollback.md) mô tả đường chạy thật. Video ClickHouse
đã quay ghim publication gốc `4b08bd51c092465783304f887d766d47`; cửa sổ Automatoes
02–03/03 có Irr9,36. Deployment sau correction có Irr10,36; không trộn số giữa pins.
Group29/tên-MSSV placeholder theo user; T53 là video20–30phút user quay offline.

## 10. Tài liệu cuối và gói trước video — T29/T30

[Report và slide hiện hành](../submission/README.md) ở `project-submit/`:
Group29, báo cáo8trangA4/slide20trang/DOCX/PPTX, script24phút; tên/MSSV là
placeholder theo user. Nguồn identity duy nhất và hướng dẫn rebuild đã có.
[Kiểm gói/diễn tập](../evaluation/assets/submission-package/README.md) ghi kết quả
sourceZIP, checksum bảy video/21exports, local Compose và thao tác sáuBR/sensor
trên publication hiện hành. BaseHEAD và per-file hashes phân biệt source ứng viên
đã review với commit gốc; không tuyên bố ZIP là nguyên HEAD sạch khi chưa đúng.

Trạng thái pre-video tách rõ khỏi đủ bốn sản phẩm: user còn điền danh tính,
xác nhận phần trình bày thực tế, quay video tổng thể20–30phút có audio, xem/nghe
và kiểm quyền truy cập nơi nộp. Checker mặc định từ chối danh tính placeholder/
thiếu video; `--pre-video` chỉ chấp nhận các phần đã có. Không tạo video giả hoặc
tự nộp bài. Timing automation không phải thời lượng thuyết trình của ba người.
