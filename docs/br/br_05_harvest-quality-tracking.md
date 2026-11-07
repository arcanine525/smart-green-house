# BR-05 — Theo dõi sản lượng thu hoạch và cơ cấu loại A/B

| Thuộc tính | Giá trị |
|---|---|
| Project | Group 29 — Smart Greenhouse |
| Phiên bản | 1.0 — 2026-11-03 |
| Trạng thái | Đặc tả đề xuất; chưa phải kết quả nghiệm thu |
| Người dùng chính | Quản lý sản xuất/trang trại |
| Người dùng liên quan | Kỹ sư canh tác |
| Ưu tiên | Bắt buộc trong bộ sáu BR |
| Thay thế | BR-05 cũ về tương quan năng suất–stress; phần tương quan chỉ còn là mở rộng khám phá |

## 1. Mục tiêu và yêu cầu nghiệp vụ

> Quản lý cần theo dõi lượng cà chua thu hoạch, sản lượng loại A và tỷ lệ loại B theo khoang và kỳ, để xác định những giai đoạn suy giảm tỷ lệ thương phẩm cần điều tra.

Ứng dụng phải giúp trả lời: mỗi khoang thu được bao nhiêu A/B, tổng lũy kế ra sao, kỳ nào tỷ lệ B cao, và kết quả nào đang thiếu dữ liệu. Thông tin này hỗ trợ chọn giai đoạn cần kiểm tra; chưa xác định nguyên nhân hay dự báo thu hoạch tương lai.

**Trong phạm vi:** lượng thu hoạch theo record/ngày/tuần/vụ, sản lượng lũy kế, tỷ lệ A/B có trọng số khối lượng, drill-down và trạng thái dữ liệu.

**Ngoài phạm vi:** doanh thu/lợi nhuận, dự báo sản lượng, truy xuất lô giao hàng, kết luận stress gây giảm năng suất, hoặc suy ra Brix của mọi quả từ mẫu rời rạc.

Áp dụng [quy ước chung](br_01_climate-condition-monitoring.md#2-quy-ước-chung-cho-cả-sáu-br). BR này có grain sự kiện thu hoạch thưa; không sử dụng coverage theo số ngày có dòng như một measure độ đầy đủ.

## 2. Dữ liệu đầu vào và grain

| Nguồn/cột | Ý nghĩa | Sử dụng |
|---|---|---|
| `Production.csv`: `%time` | Ngày ghi nhận thu hoạch, Excel serial | Chọn kỳ và sắp xếp |
| `ProdA` | Khối lượng loại A, kg/m² tại ngày thu hoạch | Yield A |
| `ProdB` | Khối lượng loại B, kg/m² tại ngày thu hoạch | Yield B |
| `Weight_fruits_ClassA/B` | Khối lượng của mẫu 10 thân, gram | Không dùng thay ProdA/ProdB |
| Metadata khoang/vụ/đội | Nguồn thư mục và mapping được xác minh | Ngữ cảnh |
| Production area basis | Dictionary nêu 62,5 m² cho quy đổi Production | Chỉ đổi sang kg tuyệt đối khi basis được gắn và xác minh đúng scope |
| Manifest và quarantine | File kỳ vọng, parse, record chưa phân giải | Độ đầy đủ đối với snapshot |

Mỗi file có 23–24 record; không phải một dòng mỗi ngày. Grain nguồn/core là một record thu hoạch. Khóa `(cycle_id, harvest_date)` chỉ áp dụng sau khi kiểm tra uniqueness của snapshot; giữ `source_row_id` để không mất danh tính gốc. Nếu sau này có nhiều lần thu một ngày, phải mở rộng khóa và tổng hợp có chủ đích, không coi mọi record cùng ngày là duplicate.

Grain mart: `FactHarvest` giữ record accepted; summary theo `(cycle_id, window)`. A/B là hạng chất lượng, không phải hai khu trồng. `A+B` là tổng sản lượng hai hạng ghi nhận, không tự gọi tất cả là sản lượng thương phẩm loại A.

## 3. Chất lượng và tính đầy đủ của dữ liệu thưa

1. Kiểm tra file theo manifest; parse tất cả dòng có log. Missing/NaN/infinite hoặc số âm không được chuyển thành 0. Giữ cờ riêng cho A/B.
2. Record đúng ngày nhưng thiếu B vẫn có thể đóng góp vào tổng A quan sát; không đủ để tính tỷ lệ A/B của record đó.
3. `Reference/Production.csv` có record serial 43510 (2019-02-14), A=0,32 kg/m², nằm ngoài vụ. Quarantine nguyên record; không tự sửa năm. Giao diện phải cho thấy giá trị bị loại và lý do.
4. Record ngoài vụ chưa xác định được thuộc vụ nào khiến completeness của cycle nguồn chưa được xác nhận; không xếp hạng như cycle đầy đủ chỉ vì đã lọc record đó.
5. Việc sửa record cần bảng correction có old/new value, nguồn bằng chứng, người ghi nhận, thời điểm và version; raw vẫn nguyên vẹn. Tái tính các BR phụ thuộc cùng snapshot/correction version.
6. Không có record trong ngày có thể là không thu hoạch, không phải thiếu sensor. Với snapshot hoàn chỉnh theo contract, calendar view được điền 0 ở ngày không có sự kiện, kèm `record_present=false`. Nếu snapshot chưa hoàn chỉnh, ô đó là “không có bản ghi; chưa xác minh”, không suy ra thu hoạch 0.
7. `harvest_snapshot_complete` chỉ true khi đủ file của cycle, parse thành công, không còn conflict/record liên quan chưa phân giải, các measure cần cho KPI được chấp nhận. Đây là completeness đối với snapshot, không chứng minh mọi thu hoạch ngoài thực tế đều đã được ghi.
8. Báo số record đầu vào/accepted/quarantine/missing theo measure. `accepted_records / ingested_records` nếu hiển thị phải gọi là tỷ lệ record được chấp nhận, không phải tỷ lệ thời gian thu hoạch có dữ liệu.

## 4. Công thức KPI

### 4.1. Tổng quan sát và cơ cấu A/B

```text
yield_a_observed_kg_m2 = Σ ProdA trên record A accepted
yield_b_observed_kg_m2 = Σ ProdB trên record B accepted
```

Để tính cơ cấu, lấy cùng tập record `H` có cả A và B accepted:

```text
paired_yield_a_kg_m2 = Σ ProdA trên H
paired_yield_b_kg_m2 = Σ ProdB trên H
paired_total_kg_m2   = paired_yield_a_kg_m2 + paired_yield_b_kg_m2
class_a_share_pct    = 100 × paired_yield_a_kg_m2 / paired_total_kg_m2
class_b_share_pct    = 100 × paired_yield_b_kg_m2 / paired_total_kg_m2
```

- Không có record dùng được → các tổng quan sát NULL, trừ trường hợp snapshot được xác nhận đầy đủ và thật sự không có sự kiện trong kỳ thì tổng kỳ bằng 0. Mẫu số bằng 0 → tỷ lệ NULL.
- Nếu chỉ một phần record đủ A/B, tỷ lệ chỉ mô tả tập H và phải kèm số record bị loại; không gọi là tỷ lệ toàn kỳ đầy đủ. UI phân biệt tổng observed theo từng measure với tổng paired.
- Không lấy trung bình tỷ lệ của từng lần thu để ra tỷ lệ kỳ. Khi mẫu số dương, hai tỷ lệ paired phải cộng thành 100% trước sai số làm tròn.
- Quy đổi A/B sang kg tuyệt đối theo basis Production có hiệu lực tại ngày thu. Nếu gộp khoang, dùng tổng kg, không cộng hoặc lấy trung bình các tỷ lệ/per-m² trực tiếp. Chưa xác minh diện tích thì giữ báo cáo per-m² riêng từng khoang.

### 4.2. Lũy kế và ngày cần xem xét

Lũy kế mặc định tính từ đầu vụ đến ngày đang xem, kể cả khi chart đang zoom vào một khoảng con; nhãn phải ghi “lũy kế từ đầu vụ”. Nếu hỗ trợ lũy kế từ đầu bộ lọc thì dùng một lựa chọn và nhãn riêng.

Rule cần xem xét: `class_b_share_pct > max_class_b_share_pct` ở grain người dùng chọn (lần thu/ngày/tuần). Ngưỡng cấu hình thuộc [0,100], có version; bằng ngưỡng không bị đánh dấu. Không có rule → vẫn có KPI nhưng không kết luận ngoài mục tiêu. Fixture demo dùng 10%; không coi đây là ngưỡng chất lượng chuẩn của AGC.

Không gọi chênh lệch giữa hai tuần là giảm năng suất do môi trường nếu lịch thu hoạch hoặc coverage khác nhau. Phần mở biểu đồ khí hậu trước kỳ thu chỉ hỗ trợ khám phá; không tính một hệ số tương quan rồi coi là kết luận nhân quả.

## 5. Xử lý DE và dữ liệu phục vụ

`RawProduction → StagingProduction → Harvest + Correction + Quarantine → FactHarvest → HarvestPeriodSummary / HarvestCycleSummary`.

- Core kiểm tra ngày/vụ, đơn vị, measure quality và completeness.
- Mart tạo tổng riêng từng measure, tổng paired, tỷ lệ và lũy kế đúng grain.
- [BR-04](br_04_energy-intensity-analysis.md) dùng cùng accepted A, correction version và completeness; không viết một parser thu hoạch khác cho năng lượng.
- Không join trực tiếp mỗi harvest với hàng nghìn climate rows để tính yield. Bối cảnh khí hậu được truy vấn/tổng hợp riêng theo cửa sổ trước khi trình bày.
- `TomQuality` là nguồn mở rộng theo kỳ mẫu. Nếu thêm, giữ sample date/context; không suy ra mọi record thu hoạch cùng kỳ có cùng Brix và không gán batch/lot ID khi nguồn không có.

Output tối thiểu: khoang/cycle/window, A/B observed và paired, tổng paired, share A/B, lũy kế, số record các trạng thái, `harvest_snapshot_complete`, basis, rule, reason và metadata chung. Có đường dẫn tới danh sách record đã tính và chưa tính.

## 6. Luồng sử dụng và giao diện

1. Quản lý chọn vụ/khoang/kỳ; xem tổng A/B và nhãn đầy đủ/một phần.
2. Xem stacked bars theo lần thu hoặc tuần, đường lũy kế và tỷ lệ B. Tooltip phải ghi grain, đơn vị và số record.
3. Chọn kỳ có tỷ lệ B cao để mở từng record; xem rule và điểm chưa phân giải.
4. Nếu cần, mở bối cảnh khí hậu/EC/pH cùng khoảng thời gian do người dùng chọn; không tự suy ra nguyên nhân.
5. Export cùng dữ liệu, trạng thái và version. Khoang incomplete vẫn hiện trong bảng nhưng không tham gia xếp hạng chính thức.

## 7. Tiêu chí nghiệm thu và ví dụ chuẩn

Fixture tổng hợp gồm hai record cùng cycle: `(ProdA,ProdB)=(1,0.2)` và `(2,0)` kg/m²; snapshot được giả định đầy đủ trừ khi test nêu khác.

| ID | Trường hợp | Kết quả bắt buộc |
|---|---|---|
| AC-01 | Tổng hợp fixture | A=3, B=0.2, tổng=3.2 kg/m²; A share=93.75%, B share=6.25% |
| AC-02 | So với trung bình tỷ lệ từng lần thu | Không trả A share=91.666667%; phải có trọng số khối lượng |
| AC-03 | Thêm một record A=0.32 ngoài vụ, chưa phân giải | Tổng accepted không đổi; quarantine hiện 0.32; completeness=false, không xếp hạng |
| AC-04 | Record thứ hai B=NULL | A observed=3; B observed=0.2; paired A=1, B=0.2; A share=83.333333%, có cảnh báo tỷ lệ trên tập con |
| AC-05 | Có record A=B=0 | Tổng=0; tỷ lệ NULL; phân biệt với không có dữ liệu |
| AC-06 | Ngày không có event giữa hai lần thu, snapshot đầy đủ | Calendar ngày đó 0 với record_present=false; không tăng số lần thu hoạch |
| AC-07 | Ngày không có event nhưng snapshot lỗi parse | Hiển thị unknown/chưa xác minh, không điền 0 như đã biết không thu hoạch |
| AC-08 | Rule demo B share >10%, áp dụng từng lần thu | Record đầu 16.666667% bị đánh dấu, record sau 0% không bị đánh dấu |
| AC-09 | Lũy kế sau hai record; zoom chỉ record sau | Lũy kế từ đầu vụ A=3, tổng=3.2; không reset âm thầm thành 2 |
| AC-10 | Rerun snapshot và dùng A cho BR-04 | Không nhân record; accepted A và completeness giữa hai BR khớp |

Sai số số thực `1e-6` trước làm tròn. Khi biểu diễn tổng khối lượng nhiều khoang, cần thêm fixture khác diện tích để kiểm tra trọng số.

## 8. Đánh giá, phụ thuộc và điều kiện hoàn thành

- **Độ đúng:** đối soát từng record tới tổng và lũy kế, fixture missing/quarantine/ratio of sums; không dùng lịch ngày giả làm coverage harvest.
- **Hiệu năng:** summary theo vụ và chart tuần đáp ứng mục tiêu chung; đo pipeline và refresh correction riêng.
- **Khai thác:** quản lý tìm được record có tỷ lệ B cao trong fixture và giải thích vì sao một khoang chưa được xếp hạng.
- **Phụ thuộc:** parser Production, manifest, metadata cycle và correction/quarantine. Không cần dữ liệu lab hoặc dự báo để hoàn thành v1.
- **Cần xác minh:** ngày Reference và tính đầy đủ của snapshot. Có thể nghiệm thu chức năng trên phần dữ liệu đủ điều kiện đồng thời hiển thị đúng phần chưa phân giải; không được công bố tổng toàn vụ Reference đã hoàn chỉnh khi chưa xác minh.
- **Done when:** AC đạt; tổng và cơ cấu trên dữ liệu thật truy nguồn được; UI/export/lineage hoạt động; có đánh giá và demo; BR-04 dùng cùng định nghĩa A.

## 9. Nguồn và quan hệ tài liệu

- [Yêu cầu môn học](../../DE_Requirements.pdf), trang 1–2, 5–6.
- [Nguồn AGC trong phân tích tham khảo](../ref/SmartGreenhouse_Case_Analysis.md#21-nguồn-dữ-liệu) và [grain thu hoạch thưa](../ref/SmartGreenhouse_Case_Analysis.md#23-grain-ba-mức-cùng-tồn-tại). [Inventory T05](../implementation/data-inventory.md) đã đo số record và time range thô; [khảo sát Reference](../implementation/data-decisions.md#2-reference-ngoài-vụ-và-completeness) xác nhận một record ngoài vụ tại dòng 2, chưa có căn cứ correction nên quarantine và giữ completeness false.
- `ReadMe.pdf`, mục Production; `Economics.pdf`, phần phân loại A/B, trong [dataset Kaggle](https://www.kaggle.com/datasets/piantic/autonomous-greenhouse-challengeagc-2nd-2019/).
- BR-05 trong tài liệu presentation cũ là câu hỏi năng suất–stress. File này chủ động thay bằng yêu cầu sản lượng/cơ cấu A/B cho project cuối kỳ; không diễn giải nội dung cũ thành yêu cầu triển khai bắt buộc.
