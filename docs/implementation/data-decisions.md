# Quyết định ngữ nghĩa dữ liệu AGC

Ngày khảo sát: 2026-11-04. Phạm vi: snapshot `agc-2nd-2019-c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68`. Đây là kết luận từ nguồn và quy ước triển khai, chưa phải accepted data, KPI hoặc nghiệm thu BR. **Basis tài nguyên vẫn chưa xác minh từ nguồn; user đã chốt ngày 2026-11-04 dùng giả định có nhãn `AGC_SHARED_BASIS_V1` cho BR-02/04/06. Giữ BR-04 kWh/kg, không thay bằng DLI hoặc thu hẹp phạm vi.**

## Nguồn và cách tái tạo

Nguồn gốc được giữ trong [snapshot và manifest](dataset-snapshot.md), archive SHA-256 `c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68`, 12,793,984 byte; [inventory](data-inventory.md) ghi đủ 45 file. Mọi đường dẫn nguồn dạng `Reference/Production.csv:2` dưới đây tương đối với thư mục snapshot trong container `/data/raw/agc-2nd-2019/<sha256>/`; số dòng vật lý bắt đầu từ 1, header ở dòng 1. Không liên kết tới bản giải nén thủ công hoặc đường dẫn máy cá nhân.

- `ReadMe.pdf`: trang 1 mô tả khoang/diện tích; trang 2–8 climate, thời gian và setpoint; trang 9 Production; trang 12–13 Resources; trang 14 TomQuality. Số trang tính từ đầu PDF.
- `Economics.pdf`: trang 1–2, mục Variable costs và Income. Đây là giá tham chiếu của challenge 2020, không phải hóa đơn hoặc giá hiện hành.
- Chỉ ghi count, min/max, ngày và vị trí dòng; không nhúng dòng CSV thật. Không suy ra basis vật lý, timezone hoặc trạng thái thiết bị từ tương quan số liệu.

Sau khi cấu hình import/raw theo hướng dẫn snapshot, chạy trong container:

```bash
docker compose -p smart-greenhouse --profile tools build snapshot
docker compose -p smart-greenhouse --profile tools run --rm snapshot
docker compose -p smart-greenhouse --profile tools run --rm snapshot python -m greenhouse.diagnostics.agc_evidence
```

Lệnh thứ hai xác minh ZIP và snapshot/manifest; kết quả khảo sát là `already_exists_verified`. Lệnh cuối kiểm lại manifest/hash/counters rồi in JSON tổng hợp ổn định; không ghi nguồn, accepted records hoặc DB. [Diagnostic](../../src/greenhouse/diagnostics/agc_evidence.py) chỉ đo lưới thử và phân bố lexical, không phải parser T06. Mặc định output ra stdout; nếu lưu để đối soát, dùng thư mục scratch bên ngoài repo. PDF được đọc trực tiếp theo trang từ bản trong snapshot; chẩn đoán không diễn giải PDF tự động.

## 1. Basis diện tích và đơn vị

| Câu hỏi | Bằng chứng và vị trí | Kết luận hoặc giới hạn | Hành vi theo quyết định user / khi chưa rõ | Owner / BR |
|---|---|---|---|---|
| Diện tích sàn và trồng có phải basis chung? | ReadMe trang 1 nêu total area 96 m², growing area 62.5 m² trong phần nói về 5 khoang; Reference được giới thiệu riêng. | Đây là hai khái niệm khác nhau. Câu mô tả không gán mẫu số cho từng resource measure, không chứng minh phạm vi diện tích sàn của Reference. Không tìm thấy 76.8 m² trong ReadMe/Economics của snapshot; con số ở tài liệu tham khảo không đủ làm metadata đã xác minh. | Lưu nguồn, loại diện tích, scope khoang/measure và hiệu lực riêng. Áp dụng giả định chung có nhãn ở dòng dưới cho scope đã chọn; không biến 62.5 thành metadata resource `verified`. Tắt giả định mà chưa có basis tương thích khác → NULL path. | T07 metadata; T18 / BR-02,04,05,06 |
| `ProdA`, `ProdB` chuẩn hóa theo gì? | ReadMe trang 9, hai mục ProdA/ProdB: kg/m² tại ngày thu hoạch; chuyển từ gram với production area 62.5 m². `*/Production.csv:1` có cả hai cột. | **Đã xác minh basis chuyển đổi Production = 62.5 m²** theo dictionary áp dụng cho Production. Không dùng khối lượng mẫu 10 thân `Weight_fruits_ClassA/B` thay tổng này. | T07 gắn basis cho measure A/B của sáu file trong phạm vi vụ đã chọn, nguồn trang 9; cửa sổ hiệu lực đó là phạm vi phân tích của ứng dụng, không chứng minh diện tích vật lý ngoài vụ. Không sửa record ngoài vụ để áp basis của vụ này. | T07, T12–13 / BR-05,04 |
| `Irr`, `Drain` dùng cùng mẫu số diện tích? | ReadMe trang 13: cả hai L/m²/ngày, cumulative trong ngày/reset midnight; `*/Resources.csv:1`. Không nêu diện tích số hay định nghĩa mẫu số tương ứng. | Đơn vị và reset **đã xác minh**; cùng basis Irr/Drain **chưa xác minh**. Cùng file và cùng chữ m² không chứng minh tương thích. | Áp dụng `AGC_SHARED_BASIS_V1` có nhãn cho Drain/Irr và tổng L = lượng per-m² × 62.5; tắt/thiếu giả định và chưa có basis tương thích khác → ratio/L tuyệt đối NULL với `incompatible_or_unknown_basis`, tổng observed riêng vẫn xem được. Không SUM L/m² giữa khoang thành L; không cộng `Cum_irr`/`water_sup` vào Resources. | T07, T09–11 / BR-02 |
| `Heat_cons` chuẩn hóa theo gì? | ReadMe trang 12: MJ/m²/ngày, tính từ chênh nhiệt rail/crop pipe so với không khí; có hệ số W/m² nhưng không gán diện tích sàn hay trồng. | Đơn vị nhiệt **đã xác minh**; diện tích chuẩn hóa, phạm vi và hiệu lực **chưa xác minh**. Công thức hệ số không đủ để chọn 96 hay 62.5. | Đổi MJ/3.6 thành kWh/m²; áp dụng `AGC_SHARED_BASIS_V1` có nhãn để cộng năng lượng, chia kg A và tính kWh tuyệt đối × 62.5 khi đủ quality/completeness. Tắt/thiếu giả định và chưa có basis tương thích khác → kWh/kg và lượng tuyệt đối NULL; giữ lượng trên đơn vị nguồn. | T07, T18 / BR-04,06 |
| `ElecHigh`, `ElecLow` chuẩn hóa theo gì? | ReadMe trang 12–13: kWh/m²/ngày, điện HPS/LED và khung giờ nguồn; công suất đèn W/m² không nói mẫu số diện tích. | Đơn vị và khung giờ **đã xác minh**; basis số/định nghĩa/hiệu lực của từng cột và tính tương thích với nhiệt/Production **chưa xác minh**. | Giữ lượng high/low riêng và áp dụng `AGC_SHARED_BASIS_V1` có nhãn để cộng nhiệt/điện per-m² hoặc tính lượng tuyệt đối × 62.5. Tắt/thiếu giả định và chưa có basis tương thích khác → các tổng phụ thuộc basis NULL; không coi chung m²/hệ thống đèn là bằng chứng đã xác minh. | T07, T18, T20 / BR-04,06 |
| `AGC_SHARED_BASIS_V1` — cùng basis per-m²? | User quyết định ngày 2026-11-04, do main ghi nhận. ReadMe trang 1 hỗ trợ growing area 62.5 m²; trang 9 xác nhận basis Production. **Mở rộng sang Resources là giả định, không phải bằng chứng nguồn.** | Status `assumed_unverified`, version `1`: Irr, Drain, Heat_cons, ElecHigh, ElecLow, ProdA và ProdB cùng basis growing area 62.5 m²; cả 6 khoang, `[2019-12-16,2020-05-30)` trên nhãn nguồn. | Áp dụng giả định có nhãn để tính ratio/tổng và so sánh khi qua các gate khác; mọi output/UI/export phụ thuộc mang status, assumption ID/version và nhãn “giả định chưa xác minh”. Tắt/thiếu giả định và chưa có basis tương thích khác → NULL path. Xem công thức/độ nhạy ở mục 7. | T07 metadata; T10–11 BR-02; T18–19 BR-04; T20–21 BR-06 |

Gate basis chấp nhận **basis verified hoặc giả định user đã cho phép còn hiệu lực**; hai trạng thái phải tách riêng. Bằng chứng verified phải chỉ rõ measure, loại mẫu số, khoang, diện tích số **hoặc** chứng cứ cùng basis cố định, khoảng hiệu lực và tài liệu nguồn. Metadata fixture giữ basis verified riêng, không được dùng làm bằng chứng AGC. Trạng thái basis/assumption và quantity quality được version riêng; giả định không mở các gate thiếu lượng, giá hoặc harvest completeness.

## 2. Reference ngoài vụ và completeness

Cửa sổ phân tích đã chốt ở [quy ước chung](../br/br_01_climate-condition-monitoring.md#2-quy-ước-chung-cho-cả-sáu-br) là `[2019-12-16 00:00,2020-05-30 00:00)` theo nhãn thời gian nguồn, không timezone.

| File / vị trí | Count và khoảng ngày theo epoch đã chọn | Kết luận / hành vi | Owner / BR |
|---|---|---|---|
| `Reference/Production.csv:2–25` | 24 record; min 2019-02-14, max 2020-05-29. **1 record ngoài vụ, dòng 2**, min=max ngày ngoài vụ 2019-02-14 (serial 43510). | Giữ raw serial và dòng; quarantine với `out_of_cycle_unresolved`. Chưa có correction trong ReadMe/Economics/CSV để đổi năm. Không dùng ngày thu đầu của AICU làm bằng chứng sửa Reference. | T06 lineage, T07 quarantine, T12 completeness / BR-05,04 |
| `AICU/Production.csv:2–25` | 24 record; 2020-02-14–2020-05-29; 0 ngoài vụ. | Đây là quan sát của AICU, không phải ngày sửa được xác minh cho Reference. | T12–13 / BR-05 |
| `Automatoes`, `Digilog`, `IUACAAS`, `TheAutomators` / `Production.csv:2–24` | Mỗi file 23 record; 2020-02-19–2020-05-29; 0 ngoài vụ. | Không dùng 23–24 record làm coverage ngày hoặc bảo đảm không có thu hoạch bị bỏ sót. | T12–13 / BR-05 |

T12 được cộng phần A/B accepted trong vụ với nhãn observed/partial, nhưng Reference vẫn `snapshot_complete=false`, `comparison_eligible=false` khi dòng 2 chưa phân giải. Không coi việc cách ly thành công là snapshot vụ đã hoàn chỉnh. T13 công bố cùng contract accepted A/completeness cho T18–T19; BR-04 của Reference vẫn NULL khi áp dụng giả định basis chung hoặc khi basis năng lượng được xác minh sau này. Correction chỉ được thêm khi có nguồn chứng minh ngày/vụ thực, lưu old/new value, lý do, người quyết định và revision; không sửa raw hoặc backfill năm theo heuristic. Không tự chuyển dòng sang vụ mới khi chưa rõ vụ đích.

## 3. Thời gian, biên vụ và drift serial

| Câu hỏi | Bằng chứng | Quyết định và hành vi | Owner / BR |
|---|---|---|---|
| Timezone / DST của nguồn? | ReadMe trang 2,6,8,9,13 chỉ gọi thời gian Excel; header CSV không có offset. Không có timezone/DST contract trong ReadMe/Economics. | **Chưa xác minh.** Lưu serial và timestamp nguồn không timezone, `source_timezone=unknown`. Không gắn UTC, Europe/Amsterdam, +7 giờ hoặc tự thêm/bỏ giờ DST. Clock ingestion/emission có timezone là trường riêng. 288 slot ngày 2020-03-29 chỉ là bằng chứng lưới nhãn đủ, không chứng minh nguồn có/không DST. | T06–07; BR-01–06, replay sau này |
| Epoch Excel? | ReadMe chỉ ghi Excel format, không ghi epoch; BR-01 mục 2 đã chọn `1899-12-30`. Serial 43815/43981 khớp cửa sổ phân tích theo lựa chọn này. | **Quy ước ứng dụng đã chốt**, chưa phải epoch được publisher xác nhận độc lập. Dùng epoch này có version, giữ số gốc; không đổi epoch/yêu cầu dịch ngày để hợp KPI. Giá trị không parse được đưa DQ/quarantine theo T06, không lấp bằng ngày hiện tại. | T06–07 / tất cả BR |
| Timestamp là đầu hay cuối slot? | ReadMe trang 2–8 ghi interval 5 min nhưng không định nghĩa start/end; Resources trang 13 nói reset midnight, không định nghĩa timestamp tổng ngày là ngày vừa kết thúc hay ngày gắn nhãn. | **Chưa xác minh ngữ nghĩa vật lý.** Theo BR-01, quy ước phân tích sample đại diện `[t,t+5 phút)`. Resources gắn ngày theo nhãn serial, không tự lùi một ngày. Công bố convention/version và giới hạn thời lượng; không gọi là thời lượng thiết bị thật đã đo. | T07, T14–17; T18/T20 dùng resource_date / tất cả BR |
| Sai số serial và grid? | Cả 13 file dày (6 Climate, 6 GrodanSens, Weather) đều median delta 4.9968 phút. Lưới thử `round(serial×288)` có max residual **0.384 giây**, 0 slot trùng, 0 slot thiếu trong span, 0 bước lùi. Vị trí mỗi file `:2–47810`. | T06–07 giữ raw và canonical slot riêng; nearest grid theo Decimal với ngưỡng **0.5 giây** là policy v1 của ứng dụng cho snapshot 5 chữ số thập phân, không suy ra từ nhà cung cấp. Ngưỡng bao sai số đã đo và nhỏ hơn nhiều nửa slot. Ngoài ngưỡng, tie, collision hoặc gap phải báo DQ/quarantine tương ứng, không snap tùy ý hoặc nội suy. Không cộng delta raw liên tiếp thành duration. Policy mới phải đổi version và kiểm cardinality lại. | T06–07, T14–17 / BR-01,03 |

Bảng biên sau đo độc lập cho từng khoang (không lấy min harvest làm đầu vụ):

| Khoang | Climate và GrodanSens (mỗi file) | Resources | Production |
|---|---|---|---|
| AICU | 2019-12-16 00:00–2020-05-30 00:00; 47,809 điểm | 2019-12-16–2020-05-29; 166 ngày | 2020-02-14–2020-05-29; 24 record |
| Automatoes | 2019-12-16 00:00–2020-05-30 00:00; 47,809 điểm | 2019-12-16–2020-05-29; 166 ngày | 2020-02-19–2020-05-29; 23 record |
| Digilog | 2019-12-16 00:00–2020-05-30 00:00; 47,809 điểm | 2019-12-16–2020-05-29; 166 ngày | 2020-02-19–2020-05-29; 23 record |
| IUACAAS | 2019-12-16 00:00–2020-05-30 00:00; 47,809 điểm | 2019-12-16–2020-05-29; 166 ngày | 2020-02-19–2020-05-29; 23 record |
| Reference | 2019-12-16 00:00–2020-05-30 00:00; 47,809 điểm | 2019-12-16–2020-05-29; 166 ngày | 2019-02-14–2020-05-29; 24 record; 1 quarantine dự kiến |
| TheAutomators | 2019-12-16 00:00–2020-05-30 00:00; 47,809 điểm | 2019-12-16–2020-05-29; 166 ngày | 2020-02-19–2020-05-29; 23 record |

Tham chiếu: mỗi `*/GreenhouseClimate.csv:2,47810`, `*/GrodanSens.csv:2,47810`; `*/Resources.csv:2,167`; Production ở mục 2. Weather có cùng span/dòng với chuỗi dày. Mỗi chuỗi dày có **47,808 nhãn trong vụ + 1 điểm đúng biên đóng**, và 288 nhãn ngày 2020-03-29. 166 ngày × 288 = 47,808 slot kỳ vọng; điểm đúng 2020-05-30 được lưu như boundary, không tạo thêm 5 phút. Đây là counts thời gian lexical, **không phải accepted coverage** của actual/setpoint. T06 phải phân biệt boundary hợp lệ với record Production ngoài vụ chưa phân giải.

## 4. Setpoint và off-mode

ReadMe trang 5–8 phân biệt requested setpoint và VIP realized; trang 3 mô tả HPS status 0/100, không định nghĩa CO₂ hoặc heating off sentinel. Các dấu gạch trong cột comment PDF không phải token off CSV. CSV có tên thực `assim_sp` và `t_ventlee_vip`; không dùng tên viết hoa/biến thể dictionary như header chính xác.

Khảo sát sáu cột phục vụ BR-01 (`co2_sp`, `co2_vip`, `t_heat_sp`, `t_heat_vip`, `t_vent_sp`, `t_ventlee_vip`) trên toàn bộ mỗi file `*/GreenhouseClimate.csv:2–47810`: sau khi tách missing lexical (`blank`, NaN, NA, N/A), **0 ký hiệu phi số khác** trong sáu cột ở cả sáu khoang. Điều này không chứng minh số 0 hay âm là off.

| Khoang | CO₂ requested: zero / missing | CO₂ realized: zero / missing | Dấu hiệu khác cần giữ nguyên |
|---|---:|---:|---|
| AICU | 0 / 41,125 | 0 / 71 | Heating requested thiếu 33,319; không được dùng realized để lấp requested. |
| Automatoes | 13,633 / 754 | 13,658 / 71 | Heating requested max 88°C, realized max 28.5°C; không chứng minh 88 là off hoặc sửa được thành 28.5. |
| Digilog | 17,306 / 516 | 18,542 / 71 | Heating requested min −10°C: 3 âm, 93 zero; realized 2 âm, 77 zero. Ventilation requested 94 zero, realized 79 zero. |
| IUACAAS | 17,201 / 2,441 | 17,309 / 71 | Heating requested max 50°C, realized max 26°C; không coalesce hay tự sửa. |
| Reference | 0 / 1,522 | 0 / 71 | Không có CO₂ zero không chứng minh mọi điểm đều hợp lệ. |
| TheAutomators | 0 / 644 | 0 / 71 | Không có CO₂ zero không xác minh được mode. |

**Kết luận:** mapping số/ký hiệu sang off-mode của CO₂/nhiệt **chưa xác minh**. T07 lưu raw value và observation cần rà soát, tách quality khỏi business out-of-range; không tự tuyên bố zero/âm/giá trị lớn là tắt hoặc lỗi. Baseline BR-01 giữ CO₂ setpoint 0 là unknown (`off_mode_unverified`), missing là unknown; không coi mọi đêm là inactive, không fallback giữa requested/realized. Với giá trị nhiệt khác thường, cần policy có version để quyết định eligibility; nếu còn phụ thuộc diễn giải off chưa rõ thì giữ unknown, không tự sửa thành giá trị hợp lý hay kết luận actual vi phạm một lệnh tắt. Không thêm cận loại số tùy ý từ min/max quan sát.

Chỉ khi có policy giải thích mode và scope được phê duyệt thì slot inactive mới `not_applicable` theo scope của policy; không ghi `in`, `out=0` hoặc forward-fill setpoint. KPI không có slot hợp lệ phải NULL kèm lý do; scope, expected/valid/unknown/not-applicable và version đi cùng export. Owner T07/T14–15; BR-01, không thay AC hiện hành.

## 5. Profile demo và biểu giá lịch sử

| Profile / câu hỏi | Nguồn và trạng thái | Hành vi / owner |
|---|---|---|
| Climate tolerance ±0.5°C, ±50 ppm; lọc episode tối thiểu 15 phút | BR-01 mục 4; **demo của đồ án**, không có validation nông học trong ReadMe. | T14–15 lưu rule version/scope, gắn nhãn `demo_rule` trên UI/export. Bộ lọc episode không xóa thời lượng đợt ngắn. Không gọi là điều kiện tối ưu đã xác thực. |
| Drain EC [2,5] dS/m; pH [5.5,6.5]; baseline pH=0 suspect | BR-03 mục 3,7; ReadMe trang 4 xác nhận đơn vị/context drain nhưng không xác nhận dải tối ưu hoặc policy lỗi đo. | T07/T16–17 version quality và rule riêng; gắn nhãn demo, không chẩn đoán thiếu dinh dưỡng/bệnh. Không dùng lab thưa hoặc Grodan/rootzone thay drain 5 phút. |
| Peak electricity | Economics trang 1, Variable costs: 0.08 EUR/kWh; ReadMe trang 12 `ElecHigh`: 07:00–23:00. | T20 nhập `AGC_2020_REFERENCE_V1`; dùng lượng `ElecHigh` có sẵn, không tái chia theo timezone suy đoán. |
| Off-peak electricity | Economics trang 2: 0.04 EUR/kWh; ReadMe trang 12–13 `ElecLow`: ngoài giờ peak. | Quy ước biên của profile ứng dụng: peak `[07:00,23:00)`, off-peak `[00:00,07:00)` và `[23:00,24:00)` trên nhãn giờ nguồn. PDF không nói chi tiết sample tại biên; không tái phân loại lượng ngày để kiểm biên. |
| Heat | Economics trang 2: **0.0083 EUR/MJ**. | T20 dùng giá gốc; nếu đổi sang kWh thì **0.02988 EUR/kWh**, không làm tròn thành 0.03. |
| Hiệu lực và phạm vi giá | Economics trang 1 nói challenge 2020; không cung cấp rate intervals theo từng ngày/offset. | Cửa sổ `[2019-12-16,2020-05-30)` của profile là **khai báo phạm vi phân tích** theo BR-06 mục 3, không xác nhận giá thị trường trong mọi ngày hoặc hóa đơn thật. Ngoài scope: không dùng profile mặc định. Không tính profit/farm cost đầy đủ từ ba component. |

### Gate cộng chi phí BR-06

1. T20 tái dùng accepted quantity/snapshot/quality/basis version của T18; mỗi ngày/component cần đúng một rate cùng currency/unit và hiệu lực. Rate gap hoặc overlap không được lấp bằng 0/latest. Tính Decimal từng quantity × rate, không làm tròn trước SUM.
2. Khi chưa có basis verified chung và cũng chưa áp dụng giả định user, chỉ hiển thị chi phí **từng component** trên m² basis nguồn riêng, kèm trạng thái chưa xác minh; không gọi tổng các phần này là EUR/m² chung. Partial theo ngày phải ghi rõ quantity/pricing coverage, kể cả khi giả định đang áp dụng.
3. Được cộng ba component thành tổng EUR/m² khi có basis verified chung cố định **hoặc** `AGC_SHARED_BASIS_V1` được áp dụng đúng scope/hiệu lực. Với giả định user đã chọn, tổng EUR/m² = heat cost + peak cost + off-peak cost; tổng EUR tuyệt đối = tổng EUR/m² × **62.5 m²** cho mỗi khoang. Mọi tổng, share và delta phụ thuộc giả định mang nhãn/status/ID/version ở mục 7. Nếu basis thực khác nhau hoặc thay đổi, dùng diện tích measure-specific có hiệu lực trước khi cộng EUR tuyệt đối; không áp diện tích mới nhất cho toàn lịch sử.
4. Giả định mở **gate basis**, còn quantity/pricing coverage và các gate khác vẫn phải đạt. Khi thiếu/tắt giả định và chưa có basis tương thích khác: `total_cost=NULL`, `component_share_pct=NULL`, tổng/delta toàn kỳ NULL với `incompatible_or_unknown_basis` cùng các lý do quantity/rate khác. T20–T21 và đánh giá BR-06 phải báo độ nhạy: nếu energy dùng 96 thay vì 62.5 m², EUR tuyệt đối tăng × **1.536**; tổng EUR/m² không đổi khi ba component vẫn chung basis. Không cần Production để tính chi phí; Reference harvest quarantine không tự chặn BR-06.
5. Scenario chỉ đổi rate, giữ nguyên quantity, snapshot, quality, basis, currency, ngày và cách phân high/low. Baseline bất biến, scenario có version; không đổi giờ cao điểm từ dữ liệu tổng ngày. Delta component có thể báo khi hai phía cùng scope/unit đủ dữ liệu; tổng/delta chính thức cần cùng tập ngày đầy đủ và gate basis qua. T21 không được coi chỉ component costs là hoàn tất mục tiêu tổng chi phí.

## 6. Handoff định dạng cho T06

| Nguồn / vị trí | Quan sát aggregate | Khuyến nghị adapter có version / hành vi khi khác |
|---|---|---|
| `IUACAAS/Resources.csv:1–167` | Header 12 trường, 166 data rows đều 12; **5 cột cuối index 7–11 (0-based) có header và toàn bộ giá trị rỗng**. | Adapter riêng theo signature/snapshot chỉ cho phép bỏ 5 cột này khỏi typed projection sau khi xác minh chúng rỗng; raw giữ nguyên, ghi adapter version/audit. Nếu có header/value mới hoặc width khác thì fail/quarantine có lý do, không truncate mọi cột sau thứ 7. |
| `Reference/TomQuality.csv:1–9` | Header 7 trường, cả 8 data rows có 8; trường cuối header gộp Weight và DMC_fruit bằng tab. ReadMe trang 14 tách Weight (g) và DMC_fruit (%). Năm file TomQuality khác có header/data 8 trường. | Đề xuất adapter header repair riêng tách hai tên, đối chiếu vị trí với dictionary/các header đồng dạng, giữ bản gốc và version. Chưa dùng TomQuality cho BR chính thì bảo toàn raw và ghi deferred/quarantine thay vì tuyên bố đã nạp đủ. Không zip 7 headers với 8 values làm mất cột cuối. |
| Cả 43 CSV | CRLF, **0 bare LF/CR**; chỉ `TheAutomators/LabAnalysis.csv` có UTF-8 BOM (39 header fields, 10 rows). | Đọc `utf-8-sig`, `newline=""`; không rewrite raw line endings/BOM. Kiểm width trước mapping, header normalization tường minh và phát hiện collision; giữ file/physical line/checksum/source token cho lineage. |

T06 chỉ triển khai ingestion/adapter theo policy, không suy đoán area/off-mode hoặc tự chữa năm; T07 giữ typed time, bằng chứng basis và giả định user tách trạng thái, quality/quarantine và policy version. Count grid/row có đủ không thay thế accepted counts theo measure.

## 7. BR-04 — quyết định basis của user

**Trạng thái: user đã quyết định ngày 2026-11-04, do main ghi nhận.** Giữ BR-04 kWh/kg với giả định có nhãn dưới đây, áp dụng đồng thời cho BR-02/06. Không chọn DLI hoặc thu hẹp mục tiêu. ReadMe trang 9 xác minh Production 62.5 m²; trang 12–13 vẫn chưa xác minh basis Resources. Quyết định cho phép tính theo giả định, không biến phần thiếu bằng chứng thành `verified`, cũng chưa phải nghiệm thu các BR.

| Thuộc tính | Giá trị / contract triển khai |
|---|---|
| Assumption ID / version | `AGC_SHARED_BASIS_V1` / `1` |
| Status | `assumed_unverified`, tách biệt `verified` |
| Statement | Irr, Drain, Heat_cons, ElecHigh, ElecLow (Resources) và ProdA, ProdB (Production) dùng cùng basis per-m², lấy growing area **62.5 m²**. |
| Scope / hiệu lực | AICU, Automatoes, Digilog, IUACAAS, Reference, TheAutomators; `[2019-12-16,2020-05-30)` theo nhãn thời gian nguồn. Không tự mở rộng ra vụ/snapshot khác. |
| Source / provenance | ReadMe trang 1 (growing area) và trang 9 (Production); user quyết định ngày 2026-11-04. Phần gán basis đó cho Resources là **giả định của user**, không phải kết luận PDF/CSV. |
| Metadata / lineage bắt buộc | T07 lưu `AreaBasis` theo measure/scope/hiệu lực, trạng thái, nguồn, assumption ID/version và trạng thái áp dụng. Mọi output, UI view, export phụ thuộc ghi `basis_status=assumed_unverified`, `basis_assumption_id=AGC_SHARED_BASIS_V1`, `basis_assumption_version=1`, nhãn nhìn thấy **“giả định chưa xác minh”**. Không gắn verified cho tổng/ratio chỉ vì Production có chứng cứ riêng. |
| So sánh / xếp hạng | Cho phép giữa các khoang dùng cùng giả định/version, cùng cửa sổ/quality và đủ các gate còn lại; luôn giữ nhãn. Reference vẫn không đủ điều kiện BR-04 vì harvest completeness false. |
| Khi thiếu / tắt giả định | Nếu chưa có basis tương thích khác, giữ nguyên NULL path và tests cho unknown/different basis; không fallback ngầm về 62.5. Giả định không áp lên fixture synthetic có basis verified riêng hoặc ghi đè basis khác đã biết. |
| Khi có bằng chứng mới | Tạo basis `verified` bằng version mới, giữ nguồn và lịch sử giả định/kết quả cũ; không overwrite. Mọi lần tính giữ basis/assumption version để tái lập. |

### Công thức trong phạm vi giả định

```text
BR-02: drain_ratio = Σ paired Drain / Σ paired Irr
       drain_ratio_pct = 100 × drain_ratio
BR-04: energy_kwh_per_m2 = Σ (Heat_cons / 3.6 + ElecHigh + ElecLow)
       kwh_per_kg_a = energy_kwh_per_m2 / Σ accepted ProdA
BR-06: total_cost_eur_per_m2 = Σ (Heat_cons × heat_rate_eur_mj
                                + ElecHigh × peak_rate_eur_kwh
                                + ElecLow × offpeak_rate_eur_kwh)
absolute quantity or cost = per_m2 total × 62.5
```

Các tổng vẫn theo grain/window/quality trong BR hiện hành: BR-04 aggregate energy toàn vụ và harvest riêng rồi ghép, BR-02 dùng paired sums, BR-06 áp rate theo ngày/component. Không bỏ ngày chưa thu hoạch khỏi energy. Basis chung và cố định làm **diện tích số triệt tiêu** trong ratio per-m²; 62.5 chỉ cần khi tính L, kWh, kg hoặc EUR tuyệt đối. Điều kiện mẫu số dương, quantity/rate coverage và completeness vẫn giữ nguyên; Reference dòng 2 quarantine nên BR-04/05 completeness false và BR-04 NULL.

### Độ nhạy bắt buộc đi cùng kết quả

Phương án đối chiếu: energy/water dùng floor area **96 m²**, Production dùng **62.5 m²**. Đây là phương án giả định thay thế để đo độ nhạy, không phải basis đã xác minh.

| Kết quả | Ảnh hưởng so với giả định chính 62.5 m² chung |
|---|---|
| BR-04 kWh/kg A | Nhân `96 / 62.5 = 1.536` (**+53.6%**), vì diện tích energy và Production không còn triệt tiêu. |
| Tổng L/kWh/EUR tuyệt đối của Resources | Tuyến tính theo diện tích; chọn 96 thay 62.5 → × **1.536**. Production tuyệt đối giữ 62.5 trong phương án này. |
| BR-02 Drain/Irr | Không đổi nếu cả Irr và Drain cùng dùng 96 m²; diện tích water vẫn triệt tiêu. Tổng nước tuyệt đối vẫn đổi. |
| BR-06 tổng EUR/m² | Không đổi số trên đơn vị nguồn khi ba energy component vẫn chung basis; tổng EUR tuyệt đối đổi × **1.536**. |

T18/T20 và đánh giá BR-04/06 phải báo phương án 96/62.5 cùng kết quả chính và nhãn giả định, không chỉ giấu trong metadata. T10–T11/đánh giá BR-02 nêu giả định ratio và độ nhạy tổng nước nếu công bố L. Không tạo kết quả số cho record/khoang bị chặn bởi gate khác; Reference không được mở KPI bằng sensitivity.

### Owner và quan hệ với AC

- **T07:** `AreaBasis`, assumption metadata, scope/hiệu lực, status/version và provenance; giữ riêng chứng cứ Production và metadata fixture.
- **T10–T11 (BR-02):** ratio dưới giả định, nhãn/lineage trên UI/export, NULL khi thiếu/tắt và độ nhạy tổng L.
- **T18–T19 (BR-04):** conversion, aggregate riêng, kWh/kg dưới giả định, sensitivity 1.536, nhãn so sánh/xếp hạng; giữ Reference NULL.
- **T20–T21 (BR-06):** tổng EUR/m² và EUR dưới giả định, sensitivity, temporal joins và scenario cùng basis/version; không thay biểu giá hoặc khung giờ nguồn.

**AC và sections 1–8 của sáu BR giữ nguyên.** Các AC unknown/different basis (BR-04 AC-06, BR-02 AC-09, BR-06 AC-08) tiếp tục kiểm NULL khi không có basis/assumption record phù hợp hoặc khi giả định bị tắt. Fixture synthetic giữ basis verified riêng, gồm trường hợp cố ý khác diện tích. Việc cho phép giả định là quyết định áp dụng có version được ghi tại đây; không xóa tests/gate cũ. Timezone, off-mode, tariff và Reference quarantine không đổi. Task triển khai và đánh giá sau này phải chứng minh nhãn, lineage, sensitivity và cả NULL path; tài liệu này chưa chứng minh chúng đã chạy.

## 8. Các điểm chưa xác minh và owner tiếp nhận

| Còn chưa rõ | Hành vi hiện hành / nguồn cần bổ sung | Owner và mốc |
|---|---|---|
| Resource area/basis, tương thích Irr/Drain, nhiệt/điện/Production | Bằng chứng nguồn vẫn chưa xác minh; user đã cho phép `AGC_SHARED_BASIS_V1` có nhãn cho ratio/kWh/kg/tổng cost, theo scope/version ở mục 7. Thiếu/tắt giả định mà chưa có basis khác → NULL; sensitivity 96/62.5 bắt buộc. | T07 metadata/assumption; T10–11 BR-02; T18–19 BR-04; T20–21 BR-06. Main tiếp nhận bằng chứng mới để tạo verified version, không overwrite. |
| Reference record thuộc ngày/vụ nào | Quarantine dòng 2; completeness false, cần correction có provenance, không sửa năm theo lịch thu hoạch. | Main tìm nguồn; T06/T07 lineage/audit; T12/T13 giữ accepted A/completeness dùng chung T18. |
| Source timezone/DST, epoch publisher, timestamp start/end, nhãn ngày tổng | Giữ source-naive/serial; dùng convention có version ở mục 3, không tuyên bố đã xác minh vật lý. | T06 parse, T07 metadata/time policy; T14–17 và replay giữ giới hạn. |
| Setpoint off semantics, ngưỡng nông học và validity điểm bất thường | CO₂ zero unknown; không coalesce/sửa số; demo rules có nhãn, cần tài liệu mode/validation trước diễn giải nông học. | T07 quality; T14–17 rules/UI; main quyết định nếu cần scope mới. |
| Giá ngoài cửa sổ, tính bao biên sample chính xác | Chỉ profile lịch sử trong phạm vi khai báo, giữ các daily buckets nguồn; không đổi giờ peak. | T20 version/rate join, T21 scenario validation. |

Handoff trọng tâm: **T06** adapter + raw/line/time; **T07** measure-specific basis và assumption status/version, naive time, quality/correction policies; **T12/T13** Reference completeness và accepted A; **T18/T19** lượng riêng, conversion, kWh/kg có nhãn và sensitivity; **T20/T21** đúng đơn vị giá, temporal join, tổng chi phí có nhãn/sensitivity và NULL path. Quyết định assumption không sửa AC hiện hành và không chứng minh các task triển khai đã chạy.

## 9. Revision trong snapshot khóa và ranh giới snapshot

**User quyết định ngày 2026-11-04, main chuyển tiếp khi triển khai recovery.** “Changed content → new revision” là correction tường minh trong `meta.correction`: source_record_id, full old/new values, actor (`recorded_by`), evidence và version. Correction append revision n+1, giữ raw/source identity bất biến. Rerun cùng input reuse unchanged revisions; publication chọn đúng latest revision và khớp membership của clean run. Duplicate cùng content và conflict khác content trong file vẫn xử lý theo T06; không chọn first winner cho conflict.

Snapshot raw khác là data_scope riêng; không merge hoặc matching identity xuyên snapshot trong batch recovery. Basis/assumption không tự mở rộng theo snapshot mới, phải có quyết định có nguồn như BR-04 ở mục 7. Correction hiện chỉ thay value map của một subtype; không tự sửa time, cycle, basis hoặc Reference out-of-cycle. [Coordinator và correction contract](../../src/greenhouse/db/README.md#workflow-chạy-lại-và-publication) triển khai và kiểm tra các ranh giới này; marts/nghiệm thu BR thuộc các task sau.
