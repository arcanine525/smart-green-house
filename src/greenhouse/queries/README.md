# Query service và oracle BR-02

Energy quantities for later BR-04/BR-06 share [EnergyService](energy.py) and the
[effective basis/energy contract](../../../docs/implementation/energy-mart-contract.md).
They read sealed component/day rows and separate observed, complete-day and
official quantities; there is no intensity, tariff or scenario computation here.

[IrrigationService](irrigation.py) chỉ phụ thuộc [contracts](../contracts/README.md) và `QueryAdapter` Protocol. Công thức là hàm thuần `summarize_irrigation(context, publication, cycle, days)`, không cần DB/Spark/CSV. PostgreSQL implementation ở [db/query_adapter.py](../db/query_adapter.py), SQL ở package DB; adapter mặc định đọc sealed water mart, core adapter chỉ dùng đối soát; không có adapter giả hoặc ClickHouse.

## Một tác vụ pin một publication

```python
from datetime import date
import psycopg
from greenhouse.contracts import QueryContext
from greenhouse.db.query_adapter import PostgresQueryAdapter
from greenhouse.queries import IrrigationService, result_cache_key
from greenhouse.settings import Settings

with psycopg.connect(**Settings.from_env().connection_kwargs(), autocommit=True) as conn:
    service = IrrigationService(PostgresQueryAdapter(conn))
    publication = service.pin_publication("<scope_id>")
    context = QueryContext(
        publication.scope_id, publication.publication_id, publication.snapshot_id,
        "<cycle_id>", ("<compartment_id>",), date(2020, 1, 1), date(2020, 1, 8),
    )
    days = service.get_irrigation_days(context)
    summary = service.get_irrigation_summary(context)
    cache_key = result_cache_key(context, publication, scenario_version=None)
```

Đây là composition ở caller; service không tự mở connection/import adapter. Chạy caller qua service job Compose như [môi trường local](../../../docs/implementation/local-environment.md), không cần venv host. Không có CLI hoặc endpoint mới. Connection do caller sở hữu; read methods chỉ SELECT.

`pin_publication(scope_id)` đọc current pointer đúng một lần, trả `PublicationRef`. Mọi query sau dùng ID trong context/ref, kể cả khi pointer đã đổi. Không dùng view `core.published_measure` cho tác vụ nhiều query vì view đó đọc pointer hiện hành mỗi lần. Mart đã materialize chính membership/quality/basis pins của publication được yêu cầu; adapter không chọn latest revision/basis hoặc đọc mutable basis toggles. Thiếu receipt/bundle mart → lỗi, không fallback core. `PostgresCoreQueryAdapter` giữ đường published core riêng cho oracle/audit. Context/pub/snapshot sai, cycle không thuộc scope hoặc compartment sai → fail.

**Main chốt cho contracts:** cycle trong schema thuộc đúng một khoang. Một context/summary chỉ chứa một cycle và một compartment; multi-compartment comparison dùng nhiều contexts với cycle tương ứng, cùng publication đã pin. Không cộng L/m² của các khoang thành tổng trang trại. Service chưa xếp hạng nhiều khoang; `comparison_eligible` chỉ là gate nội bộ cho một kết quả, so sánh còn phải cùng window/policy/basis.

## Ngữ nghĩa oracle

- Grain duy nhất `(cycle_id, resource_date)` và đúng publication/compartment; duplicate hoặc mixed grain bị từ chối, không chọn first/last. Request chỉ ngày trọn, half-open; cắt theo cycle. Kết quả giữ cả requested context và effective window, reason `window_clipped_to_cycle` khi có cắt. Không giao cycle → fail.
- `D` gồm **mọi ngày** trong effective window, gồm ngày không có resource record. Adapter trả observed days; service tạo missing day DTO có NULL/reason `missing_resource_day`, không source/revision giả. Null/missing/invalid/suspect/applicability unknown không thành 0 hoặc accepted.
- Observed Irr/Drain cộng **riêng** trên accepted + applicable, không phụ thuộc basis. Không có ngày accepted cho measure → tổng NULL. Một measure lỗi không xóa measure còn lại.
- `P` là các ngày cả hai measure accepted/applicable và basis tương thích. Paired sums cùng P; ratio `100 × SUM paired Drain / SUM paired Irr`, không AVG daily ratios. P rỗng → NULL/reason `no_paired_days`; paired Irr=0 → NULL/reason `no_irrigation_quantity`. Observed zero vẫn có coverage.
- Compatible basis: cùng physical basis kind và positive area; cả hai verified, hoặc cả hai assumed với cùng assumption ID/version. Khác/thiếu basis, hoặc mixed verified/assumed chưa có quy ước tương thích → không paired, reason `incompatible_or_unknown_basis`. Không ghép theo numeric area đơn thuần, không auto-convert sang absolute L. Nếu physical basis thay trong window, canonical ratio cũng NULL thay vì cộng các denominator khác nhau.
- Coverage Irr/Drain/paired lần lượt là accepted day count hoặc |P| chia |D|. `unknown_days` là số ngày thiếu ít nhất một measure usable; `suspect_days` là số ngày có ít nhất một suspect measure. Basis failure riêng không bị gọi là mất quan sát. Partial paired coverage → `partial_coverage`; không ngoại suy toàn kỳ.
- Drain>Irr trên compatible accepted pair giữ tỷ lệ thật (có thể >100%) và cờ; không clamp. Số ngày này tách khỏi rule outcome. Không có rule profile → `rule_status=not_configured`, `rule_version=NULL`, `outside_rule_days=NULL`, không có kết luận đạt/không đạt hoặc ngưỡng nông học mặc định.
- Summary giữ publication/snapshot/scope, pipeline/quality/parser/code/config/metric versions, unit/time basis, basis IDs/assumption label/versions và correction IDs/versions. JSON/export dùng [codec](../contracts/README.md#json-version-2); UI không tính lại công thức hoặc biến NULL thành 0. Cache key chứa ref (cùng versions/config), context/window và scenario version khi liên quan.

Core catalog ghi daily water `L/m²/day`; adapter chỉ chấp nhận chính xác catalog unit này và ánh xạ **nhãn lượng ngày** sang BR `L/m²`, giữ `source_unit`. Không đổi số, không chia/nhân duration. Unit lạ như m³ bị từ chối. Paired totals/observed totals vẫn là L/m², ratio/coverage là phần trăm. Không cộng counters Climate vào Resources.

## Lineage

Tạo adapter với mapping mount `raw_snapshots={snapshot_id: raw_path}` rồi gọi `service.get_lineage(publication, revision_id=...)` hoặc `record_key=...`. Kiểm tra revision thuộc **membership đã pin** trước khi bọc T08 lineage và đọc lại raw hash. Không truy được candidate/correction mới chưa xuất hiện trong publication đó. Logical key có thể chung climate/drain; nếu ambiguous phải dùng revision ID, không chọn ngầm. Metadata có source serial chính xác, naive source timestamp, physical line range, hashes, units, correction version; không ghi raw payload.

## Catalog bộ lọc cho ứng dụng

[CatalogService](catalog.py) (Protocol + service thuần) và [PostgresCatalogAdapter](../db/catalog.py)
trả các option bộ lọc từ published data: publication hiện hành mỗi scope kèm receipt water mart
(`usable = bundle irrigation-mart-v1`, thiếu mart vẫn hiện kèm lý do), cycle/khoang của
publication đã pin, và rule profiles hợp lệ theo scope/khoang. UI không dán ID thủ công;
catalog không đọc lại pointer cho publication đã chọn — app pin theo publication ID một lần
mỗi tác vụ. Xem [ứng dụng nước](../app/README.md).

## Harvest service (BR-05, typed slice)

[HarvestService](harvest.py) + [PostgresHarvestQueryAdapter](../db/harvest_adapter.py) giữ record
grain thật: một record thu hoạch là một sự kiện (nhiều record cùng ngày không bị collapse), tổng
observed từng measure tính riêng, share = 100×ΣA paired/(ΣA+ΣB) trên CÙNG tập paired (không trung
bình share từng record), zero-denominator → NULL, không usable → NULL trừ cửa sổ trống hoàn toàn
trong snapshot hoàn chỉnh (known 0 với reason `known_zero_event_window`). Calendar day không có
event: complete → known 0 (`record_present=false`); incomplete → unknown/NULL, không bao giờ tự đặt
source/revision giả; không có metric "coverage theo ngày có harvest". Navigation cumulative chạy từ
`cumulative_origin` (đầu vụ, luôn đúng):
`HarvestSummary.cumulative_a` (accepted A, canonical cho BR-04) và `cumulative_total`
(A+B trên CHỈ records PAIRED) là HAI tập measure ĐỘC LẬP — record chỉ góp khi
`cycle.start <= harvest_date < effective_end`, record SAU cửa sổ không góp amount lẫn basis;
tập không có usable → NULL, snapshot complete không góp → known 0; `cumulative_basis_label` chỉ
tính từ records góp. Zoom trong vụ không mang reason clipping: biểu thị bằng `cumulative_origin`/
`cumulative_from_cycle_start`; cắt biên thật (cả MỘT biên) → `window_clipped_to_cycle`;
toàn-vụ (whole-cycle accessor) cố ý thay request bằng context toàn vụ và không coi là clipping.
Publication thiếu harvest receipt → adapter fail actionably
(không fallback core). Xem [harvest mart](../db/README.md#mart-thu-hoạch-và-completeness-br-05-slice).

## Rule durations và episodes (BR-01/BR-03, engine pure T14)

[rule_duration.py](rule_duration.py) là oracle thuần (không SQL, không adapter): đánh giá `ThresholdSlot`
đã typed chống các `RuleScheduleRevision` được **chọn tường minh**, trong một `IntradayContext`
source-naive. Nhóm khai báo `(measurement_context, basis)` là đầu vào bắt buộc, được chia sẻ bởi mọi
metric của một lần đánh giá: climate phát biểu `climate/requested` hoặc `climate/realized` (dùng
setpoint của chính slot — không backstop); drain phát biểu `drain/measured` (giá trị đo). Metric
nằm ngoài nhóm khai báo bị từ chối kể cả khi không có data hay segment cho nó; các ngữ cảnh/basis
khác là các lần đánh giá riêng — không tạo OR nhiệt+ec tổng hợp.

- Durations gắn trên slot grid cadence (mặc định 5 phút, neo đầu cycle); slot là
  `[start, start+cadence)`, cắt theo cycle và cửa sổ yêu cầu; mẫu tại đúng biên cuối cycle ở lại
  upstream với zero duration. Slot trùng lặp/off-grid/metric không được yêu cầu/mismatch context
  hoặc basis bị từ chối kèm lỗi validation có tài liệu; timestamp thiếu là unknown, không
  carry-forward giá trị cũ.
- `valid_minutes` là thời gian đủ dữ liệu để đánh giá (gồm slot out); out low/high tách riêng; unknown phân bổ
  mỗi slot đúng một lần theo thứ tự ưu tiên `UNKNOWN_REASON_PRIORITY` (suspect giữ bucket riêng và
  vẫn nằm unknown); `valid==0` → out-hours/percent NULL, không xanh 0. Coverage tính trên cửa sổ
  effective.
- Metric được yêu cầu nhưng không có segment nào áp dụng vẫn có row kết quả: expected duration giữ
  nguyên, `rule_not_configured`, KPI out NULL. Request hai metric hợp lệ tạo đúng MỘT OR trên giao
  slot jointly known (union các violation trong giao đó); request một metric không có OR; khi một
  metric được yêu cầu thiếu hoàn toàn vẫn giữ OR với jointly valid 0. Chọn `revisions=()` tường
  minh cũng hợp lệ: mọi metric yêu cầu ở lại `rule_not_configured` với expected duration — không
  cần profile dummy, không bịa revision mặc định/demo. Replay cùng revision tái lập kết quả giống
  hệt; caller chọn TỐI ĐA MỘT revision cho mỗi `(metric, context, basis)` — nạp hai revision cùng
  metric (kể cả khi segment không chồng lấn) bị từ chối TRƯỚC khi đánh giá, vì sự thay đổi theo
  thời gian thuộc về segment BÊN TRONG một revision bất biến. Metric khác nhau có thể chọn revision
  riêng; không tự chọn first/latest.
- Episodes dựng trên toàn bộ cycle trước khi cắt (break khi về dải/unknown/thiếu slot/đổi segment),
  trả chỉ những episode có chồng cửa sổ dương — giữ nguyên start/end/duration gốc cộng clipped
  facts; low/high liên tục trong một out run vẫn một episode; peak deviation/thời gian xác định;
  min duration filter chỉ lọc danh sách (theo duration gốc), không đụng tổng kết summary.
- Provenance tùy chọn `PublicationRef`/`Versions` phải tương thích với ngữ cảnh đã pin: scope và
  time-basis trùng nhau, `Versions` tường minh phải bằng publication.versions; thiếu versions tường
  minh thì suy ra từ publication. Không tra pointer hiện hành và không tự bịa metadata; pure fixture
  có thể bỏ trống provenance.

DB ở [db/rule_schedule.py](../db/rule_schedule.py): một `rule_version` bất biến, idempotent khi nội
dung giống hệt, từ chối drift bằng lỗi thay vì ghi đè. Đăng ký chạy trong MỘT
transaction/savepoint thuộc repository (autocommit hoặc bên trong giao dịch của caller): append
thất bại để lại không phần dư và retry thành công. Sau khi commit, revision được SEAL luôn như một
khối ở schema: header giữ `registration_xid` (trigger đóng `pg_current_xact_id()`), guard segment
so sánh với xid hiện hành và từ chối mọi append muộn — kể cả segment không chồng lấn. EXCLUDE(gist)
cấm chồng lấn cùng key trong một revision kể cả khi bypass repository (exact adjacency hợp lệ);
hai revision khác nhau có thể phủ cùng khoảng lịch sử, nhưng mỗi lựa chọn đánh giá chỉ chứa MỘT
revision cho mỗi metric. Demo profile là opt-in tường minh với provenance tổng hợp, không khoán
kết quả nông học.

## Phần tiếp nhận

- Water mart day, default transaction hook và day/week/cycle queries khớp cùng DTO/oracle; không materialize mọi period/filter.
- Ứng dụng nước dùng service/DTO cho chart/summary/export; không nhúng SQL trong UI, không repin giữa một tác vụ. Nghiệm thu BR-02 end-to-end và hiệu năng thuộc task sau.
- T12–T13 bổ sung harvest/completeness contracts; không dùng coverage ngày của nước cho harvest events.
- T21 tiếp nhận `ScenarioCommand` validation và triển khai persistence/idempotency/baseline versioning. T33 tiếp nhận `SinkBatch` và xử lý ghi/dedup/conflict; source identity/clock không đổi theo database engine.

Tests synthetic gồm pure oracle/serialization/architecture guards và PostgreSQL contract tests từ Resources fixture qua published core. DB `test_contracts_<hex>` tự drop cả khi test lỗi; chỉ giữ khi truyền `-e GREENHOUSE_KEEP_TEST_DB=1` tường minh vào test-runner. Đây là bằng chứng contract, chưa phải nghiệm thu mart/UI/BR-02.

## Day, ISO week, cycle và review rule

`get_irrigation_days(context)` đọc facts và calendar gaps; `get_irrigation_summary(context)` tổng hợp window; `get_irrigation_weekly(context)` đọc một tập day rows rồi trả tuple `IrrigationSummary`, mỗi tuần có `iso_year`/`iso_week`, window và expected days đã cắt theo query/cycle. `get_irrigation_cycle(context)` dùng toàn bộ cycle window tường minh. Mỗi summary là ratio of paired sums; không cộng hoặc AVG ratio của tuần/ngày. Với period tổng, `iso_year`/`iso_week` là NULL. Source quantities và 50-digit oracle T31 giữ nguyên.

Chọn profile bằng `dataclasses.replace(context, rule_version="<registered_profile_version>")`. Adapter đọc đúng immutable version đó và kiểm scope/compartment; ID lạ hoặc ngoài scope fail. Không chọn profile → not_configured, rule_version/outside_rule_days/rule_unknown_days/profile đều NULL. Có profile → day status within_range/outside_range/unknown, summary status configured với hai counts outside/unknown và đầy đủ `IrrigationRuleProfile` (version, four bounds, evidence/demo label). Bằng biên là trong dải. Thiếu pair usable/basis hoặc ratio không xác định do Irr=0 → unknown; không tự gọi là đạt rule. Summary giữ quantities/coverage giống oracle bất kể profile, chỉ thêm review metadata/reasons.

Rule cache identity nằm trong QueryContext, do đó chart/summary/weekly dùng cùng explicit version. Profile v2 không sửa kết quả request v1; source publication reference giữ source config versions, rule query-time nằm ở context/result/profile. Profile được khai báo riêng cho một scope/compartment; multi-compartment comparison dùng contexts/profiles riêng theo quyết định grain của main. AGC không có profile mặc định; rule status vẫn not_configured cho đến khi caller chọn một profile đã đăng ký.

JSON envelope hiện version **2**, thêm context rule version, result rule/profile metadata và ISO week fields; Decimal/NULL/clock/enum conventions giữ nguyên. Client cần decode đúng v2; v1 không được lặng lẽ nhận như shape mới. SQL day fact flags/ratio/reasons được tests đối chiếu trực tiếp với core-oracle, không chỉ kiểm output service. UI/export thuộc T10; nghiệm thu BR-02 thuộc T11.
