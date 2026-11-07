# T14 — Rule schedule bất biến và engine duration/episode chung BR-01/BR-03

Trạng thái trong task: engine + kho lưu trữ + kiểm thử. Nghiệm thu BR-01/BR-03 end-to-end (đọc từ
published marts, hiệu năng, UI) thuộc T15–T17 — mục này KHÔNG phải nghiệm thu AGC của hai BR.

## Phạm vi đã xây

- **Contracts** (`src/greenhouse/contracts/models.py`, export trong `contracts/__init__.py`):
  `RuleScheduleSegment`, `RuleScheduleRevision`, `RuleBasis`, `RuleDeviationDirection`,
  `IntradayContext` (thêm `measurement_context` + `basis` bắt buộc), `ThresholdSlot`,
  `UnknownReasonShare`, `RuleMetricDuration`, `RuleMetricApplied`, `RuleJointDuration`,
  `RuleSlotDeviation`, `RuleEpisode`, `RuleDurationResult`, hằng `RULE_SCHEDULE_METRICS`,
  `RULE_METRIC_UNITS`, `UNKNOWN_REASON_PRIORITY`, helper `rule_segment_sort_key`. Codec JSON v2
  hoạt động cho toàn bộ nhóm (roundtrip được kiểm thử).
- **Engine thuần** (`src/greenhouse/queries/rule_duration.py`): không SQL/DB/adapter; timestamp
  source-naive; Decimal 50 chữ số cho duration/ratio.
- **DB** (`db/migrations/008_rule_schedule.sql`, `db/rule_schedule.py` + 3 file SQL): bảng additive
  `config.rule_schedule_revision` + `config.rule_schedule_segment`, `EXCLUDE USING gist` (btree_gist)
  cấm chồng lấn cùng `(metric, context, basis)` bên trong một revision kể cả khi bypass repository,
  trigger `config.immutable_version()` trên cả hai bảng. 005/006/007 không đổi byte; 008 chỉ ADD.

## Quyết định biên (chốt của main, hiện thực hóa)

1. **Nhóm khai báo**: mọi lần đánh giá pin `measurement_context` + `RuleBasis` trong
   `IntradayContext`, chia sẻ bởi mọi metric được yêu cầu. `requested`/`realized` chỉ chấp nhận
   `temperature`/`co2`; `measured` chỉ chấp nhận `ec`/`ph`. Metric ngoài nhóm bị từ chối kể cả khi
   không có data/segment; không trộn nhóm (temperature+ec là fixture từ chối, không phải OR tổng hợp).
   Metric thiếu hoàn toàn kế thừa context/basis của REQUEST, không bịa nhóm thay thế.
2. **Segment mode nghiêm ngặt**: temperature/co2 chỉ mang `delta` (biên được tính theo setpoint của
   từng slot: nhiệt `lower=heating−delta, upper=ventilation+delta`; CO2 `lower=max(0,SP−delta),
   upper=SP+delta`, SP0 là unknown off-mode); ec/ph chỉ mang `lower/upper` cố định. Kiểm tra cùng
   lúc ở contract VÀ DB CHECK; `delta` không bao giờ nạp đè biên cố định.
3. **Revision bất biến như MỘT KHỐI + chọn tối đa một revision mỗi metric**: đăng ký đúng nội
   dung là idempotent, drift bị từ chối; đăng ký chạy trong MỘT transaction/savepoint thuộc
   repository (autocommit, connection thường, và cả bên trong transaction/savepoint của caller —
   append thất bại để lại không phần dư, retry thành công). Sau commit, revision được SEAL: header
   giữ `registration_xid` (trigger stamp `pg_current_xact_id()`, override giá trị caller), guard
   segment so sánh với xid hiện hành và từ chối mọi append muộn kể cả không chồng lấn (PG17:
   `pg_current_xact_id()` là TOP-LEVEL ID cả trong subtransaction, nên seal an toàn với nesting).
   EXCLUDE(gist) chặn chồng lấn cùng key kể cả khi bypass; contract kiểm per-key (chặn cả pattern
   trộn temperature/co2/temperature), adjacency chính xác hợp lệ. Hai revision khác nhau có thể
   phủ cùng khoảng lịch sử tại rest, nhưng một lần đánh giá chọn TỐI ĐA MỘT revision cho mỗi
   `(metric, context, basis)` — kể cả hai revision không chồng lấn cũng bị từ chối trước khi đánh
   giá (sự thay đổi theo thời gian thuộc segment bên trong một revision); metric khác nhau vẫn
   chọn được revision riêng; không tự chọn first/latest.
4. **Đủ điều kiện và lý do**: `valid_minutes` là thời gian đủ dữ liệu để đánh giá (gồm slot out); out_low/high tách
   riêng; unknown phân bổ MỖI slot đúng MỘT lần theo ưu tiên
   `rule_not_configured > missing_input > quality_invalid > quality_suspect > sp0_off_mode >
   inverted_bounds`; suspect giữ bucket riêng và vẫn nằm trong unknown. `valid==0` → out-hours/
   percent NULL (không xanh 0); equality biên nằm trong dải; thiếu timestamp là unknown, không
   carry-forward; slot trùng/off-grid/metric không yêu cầu/mismatch context-basis bị từ chối; mẫu
   tại đúng biên cuối cycle zero duration.
5. **OR, metric không cấu hình và chọn rỗng**: metric thiếu segment vẫn có row (expected
   duration, `rule_not_configured`, KPI out NULL); request ≥2 metric hợp lệ tạo đúng MỘT OR trên
   giao slot joint-known (union violation trong giao), contributions ghi danh tính từng rule góp;
   request một metric không có OR; request ec+ph thiếu hoàn toàn ph vẫn là OR đầy đủ với jointly
   valid 0. Chọn `revisions=()` tường minh hợp lệ: mọi metric yêu cầu ở lại `rule_not_configured`
   (có slot lẫn không slot) — không profile dummy, không bịa revision. Provenance
   `PublicationRef`/`Versions` khi cung cấp phải trùng scope/time-basis với ngữ cảnh, `Versions`
   tường minh phải bằng publication.versions (suy ra từ publication nếu bỏ trống); không tra
   pointer, không tự bịa.
6. **Episode**: dựng trên TOÀN BỘ cycle trước cắt, break khi về dải/unknown/thiếu slot/đổi segment
   (exact adjacency khác content vẫn break); trả chỉ episode chồng cửa sổ dương, giữ facts gốc cộng
   clipped facts; low/high trong một out run là một episode; peak deviation/thời gian xác định; min
   filter chỉ lọc danh sách theo duration gốc, không đụng tổng kết.

## Fixture tái lập (unit tests trong project container)

`tests/unit/test_rule_schedules.py` (27 test):

- BR-01 nhiệt `[19,21,26,26,NULL,24]`, delta 0 trên setpoint 20/25 → valid25/out15/unknown5,
  coverage 83.3…, out 60% (out_hours 0.25), episodes 5+10, peak 1.
- BR-01 CO2: SP0 off-mode, thiếu setpoint, biên equality trong dải, out_high 650±50.
- OR nhiệt/co2 hai slot joint-known: nhiệt out cả hai, co2 out slot hai → OR 10, không cộng 15.
- BR-03 ec `[3,6,6,NULL,3,6]` biên 2..5; ph `[6,6,7,6,0(suspect),6]` biên 5.5..6.5 → ec
  valid25/out15/episodes10+5; ph valid25/out5/suspect5; joint valid 20/OR out 15 (75%),
  joint coverage 66.66….
- ec+ph chỉ có ec: OR đầy đủ với jointly valid 0, ph `rule_not_configured` 30 phút, KPI NULL.
- Episode 00:00..00:15 với query 00:02..00:12 → original 15, clipped 10, summary out 10; min filter
  20 phút chỉ rút danh sách; query ngoài khoảng trả (); `valid==0` → KPI NULL.
- Replay cùng revision xác định giống hệt; registration idempotent/drift/atomic (autocommit,
  connection thường, nested savepoint với injected failure); EXCLUDE chặn chồng lấn khi bypass trong
  cùng transaction đăng ký; revision đã commit bị chặn append (seal, kể cả segment không chồng lấn);
  adjacency hợp lệ cả ở DB lẫn engine (đổi content là break episode); chọn hai revision cùng metric
  (chồng lấn lẫn không) bị từ chối; provenance không tương thích bị từ chối; chọn rỗng là unknown.

## Kiểm chứng

- `core_database` pin số applied migration 7 → 8 (008 additive) tại `tests/unit/test_core.py`;
  full backend 240 pass, UI 52 pass trên image dựng lại; focused rule 41 pass (gồm
  `test_demo_rules.py` để chắn regression).
- Ngoài các thay đổi nêu trên không có gì khác bị sửa: 005/006/007 byte-identical; sealed
  AGC/raw/core và schema water/harvest rule profiles không đổi; không republish AGC. Chạy trên
  Docker; DB test `test_*` được teardown sau mỗi run.

## Hạn chế — vùng giao cho T15/T16

- Engine chưa được cấp nguồn (wire) từ published marts; caller cung cấp slots/revisions/publication/
  versions tường minh; không pin metadata tự động. Chưa có UI, mart, hiệu năng, xuất khẩu/export.
- `measurement_context` hiện tự do dạng string (identifier); việc gắn từ `drain_observation`
  (default `drain_process_computer`) và climate (`climate`) thuộc bước wire T15/T16.
- Engine hiện không cache, không STREAM; gọi lại từng cửa sổ, không phải query tổng hợp SQL.
