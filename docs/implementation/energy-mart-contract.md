# Energy mart và shared quantity contract

Phạm vi release này: lượng năng lượng và basis hiệu lực dùng chung BR-04/BR-06.
Không triển khai intensity join/UI, actual AGC energy publication, tariff hoặc
scenario ở đây. Các phần đó lần lượt thuộc bước BR-04 và BR-06 tiếp theo.
[Quyết định user](data-decisions.md#7-br-04--quyết-định-basis-của-user) giữ
`AGC_SHARED_BASIS_V1` v1, **assumed_unverified62.5m²**; không đổi sang DLI,
không coi giả định Resources là chứng cứ nguồn đã xác minh.

## Publication và grain

Migration additive [011_energy_mart.sql](../../src/greenhouse/db/migrations/011_energy_mart.sql)
thêm `mart.dim_energy_cycle`, `mart.fact_energy_day`, `mart.energy_publication`.
001–010 giữ nguyên bytes/checksums. Pipeline mới `pipeline-v6-energy` materialize
cả năm bundle water/harvest/climate/drain/energy trong cùng transaction/connection
với membership, pins, seal và current pointer. Seal v6 đòi đủ năm receipt, energy
đúng `energy-mart-v1`/`energy-quantity-v1`; seal cũ giữ hành vi version cũ.
Workflow identity/config hash chứa năm bundle và `energy_quantity` version;
không silent reuse v5 hoặc quantity contract khác. Candidate statistics refresh
sau bulk load và trước publish (kể cả skipped-load resume) được giữ nguyên.

Grain fact: **publication × cycle × source date × component**. Mỗi ResourceDay
được chọn có đúng ba hàng, kể cả measure missing/invalid/suspect. Không join
Production để tạo tập ngày; ngày trước harvest đầu vẫn ở energy. Ngày nguồn
hoàn toàn thiếu không có revision giả trong mart; service tạo ba missing DTO
cho ngày kỳ vọng đó. Calendar là `[cycle.start,cycle.end)`, source-naive, ngày
trọn. Row ngoài vụ không góp quantity. Query con cắt theo vụ và giữ reason.

| Component | Measure nguồn | Đơn vị nguồn | Chuẩn hóa |
|---|---|---|---|
| `heat` | `Heat_cons` | MJ/m²/day | chia **Decimal3.6**, ra kWh/m² cho bucket ngày |
| `elec_high` | `ElecHigh` | kWh/m²/day | giữ nguyên; không đổi peak source window |
| `elec_low` | `ElecLow` | kWh/m²/day | giữ riêng với high |

Fact đóng băng value, unit, quality/applicability/reason **riêng từng measure**,
quality-policy version, source revision/logical key, correction ID/version, basis
hiệu lực và provenance/assumption. Quality không được suy ra lại theo UI/cost.
Accepted zero là valid zero. NULL/invalid/suspect/applicability unknown không
thành zero và không góp accepted quantity; raw value vẫn giữ cho drilldown.

`normalized_kwh_m2` và `absolute_kwh` dùng **50 significant digits, half-even**
chỉ tại phép MJ/3.6. SQL dùng `mart.ratio_sig`, DTO kiểm lại bằng Decimal; không
dùng float3.6 hoặc rounding hiển thị. Source sums và quantity×area là Decimal
chính xác, tổng các normalized buckets giữ precision/scale đã khai báo, không
round thành hai chữ số trước SUM. Heat absolute được tính `(MJ/m² × area)/3.6`,
không nhân area vào số kWh đã round hiển thị. Original `source_value` MJ/m² và
`absolute_source_quantity` MJ giữ nguyên cho bước giá0.0083EUR/MJ sau này;
không tái dựng MJ từ kWh. Không có phép tính giá trong release này.

## Basis hiệu lực và đóng băng

PK của `config.basis_application` và `meta.publication_basis` được mở rộng bằng
`basis_version_id` để giữ nhiều version **không chồng hiệu lực** cho một
scope/compartment/measure/context. Vẫn chọn tường minh, không lấy latest.
Trigger từ chối các enabled selection có `[valid_from,valid_to)` overlap;
adjacent intervals hợp lệ, gap giữ unknown. Basis/assumption versions append-only.
Assumption tắt hoặc application tắt không đi vào workflow pins. Không tự lấy
`source_verified` thay cho `shared_analysis` khi lựa chọn thiếu.

Water/harvest materialization, core water SELECT và published diagnostic view
resolve pin **cùng ngày trong LEFT JOIN**, tránh một NULL row thừa cho mỗi
version không hiệu lực. Điều này cho phép Production/water thay area mà không
nhân event/day. Existing single-version selections giữ nguyên dữ liệu.

Energy fact copy đầy đủ basis ID/version, effective dates, area, kind/status,
basis evidence, assumption ID/version/statement/evidence. Query đọc fields đã
freeze, không join metadata current, không đọc lại core value. Sau config toggle,
correction hoặc publication mới, query publication-ID cũ vẫn trả đúng DTO cũ.

`EffectiveAreaBasis` có scope/compartment/measure và interval; `EnergyQuantity`
từ chối basis sai scope/measure/day. Absolute quantity mỗi ngày/component dùng
đúng area đó. Verified areas khác nhau có thể đổi sang lượng tuyệt đối rồi cộng;
không cộng các lượng **per-m²** giữa area/definition khác nhau hoặc thay đổi.
Component totals giữ `basis_totals` riêng cho từng frozen basis để không mất
contributions khi một tổng per-m² phải NULL. Nhóm source-only basis unknown
giữ observed value của riêng measure, kèm reason; không mở gate official total.

### Compatibility contract dùng cho downstream

`energy_basis_gate(bases)` trả `EnergyBasisGate`: `absolute_compatible`,
`per_m2_compatible`, status/label/reasons. Không gọi helper water equality cho
energy/Production vì verified area100 và area50 phải đổi absolute riêng.

- Tất cả verified, cùng scope/compartment: được đổi absolute theo từng area;
  per-m² chỉ được pool khi cùng area **và** definition cố định toàn tập góp.
- Assumed basis phải cùng assumption ID/version. Numeric area bằng nhau không
  hòa giải các assumption/definition khác nhau. Missing/disabled → unknown/NULL.
- Bridge user tường minh: Resources `AGC_SHARED_BASIS_V1` v1,
  `growing_area_assumed`62.5 có thể gặp Production `verified/production_area`62.5.
  Kết quả vẫn **assumed_unverified**, nhãn **“giả định chưa xác minh”**; verified
  Production không verify Resources. Các bridge khác không được đoán theo area.
- Sensitivity khai báo `AGC_FLOOR_96_SENSITIVITY_V1` v1/`floor_area_sensitivity`96
  cho Resources với verified Production62.5 chỉ mở absolute conversion, không
  mở per-m² pooling. Absolute Resources và later intensity thay đổi theo
  **96/62.5=1.536**; source per-m² không đổi. Đây là giả định đối chiếu, không
  rewrite primary metadata/source hoặc mở gate harvest còn thiếu.

BR-04 tiếp theo vẫn phải aggregate energy và accepted A riêng theo cycle, kiểm
completeness/A>0 và các gate so sánh. Reference unresolved harvest quarantine
không được bỏ; nó chặn intensity, không chặn quantity/cost Resources BR-06.
BR-06 phải dùng cùng quantities/IDs và thêm rate/scenario gates; không tính
lại own-quality theo một policy khác hoặc đổi ranh giới high/low từ bucket ngày.

## Query DTO và rollup

[Adapter](../../src/greenhouse/db/energy_adapter.py) nhận publication ID bất biến,
kiểm sealed provenance + receipt đúng version, rồi chỉ SELECT energy mart.
[EnergyService](../../src/greenhouse/queries/energy.py) dùng `QueryContext`
publication/snapshot/scope/cycle/compartment; reject threshold-rule ID, mixed
pin/grain/duplicate/out-of-window. Không lấy current pointer giữa action.

- `get_quantities(context)` → tuple `EnergyQuantity` theo day/component ổn định.
  Shared `quantity_id` là `(publication_id,cycle_id,compartment_id,source_date,
  component,revision_id,quantity_version)`; cả BR-04/06 dùng nguyên identity này.
- `get_summary(context)` → `EnergySummary`; `get_cycle(context)` thay window
  bằng toàn vụ. `get_lineage(publication, revision_id=...)` dùng membership và
  raw hash/physical line chung; không accept revision ngoài publication đã pin.
- `EnergyComponentTotal` có accepted/expected/absolute day counts, coverage,
  observed source/normalized/absolute riêng và grouped basis totals. Observed
  absolute là subtotal có basis usable, không claim đủ kỳ khi `absolute_days`
  còn thiếu. Official component cần đủ mọi ngày và basis tương thích.
- `complete_days` là giao ngày **cả ba own measures accepted/applicable**,
  độc lập với basis readiness. `complete_coverage_pct=100×complete/expected`;
  `incomplete_dates` giữ ngày thiếu. Không dùng ngày có harvest làm mẫu số.
- `observed_total_kwh` cộng tất cả accepted components khi bases của chúng
  hòa giải được; có thể partial. `complete_day_total_kwh` chỉ cộng complete-day
  intersection khi bases của tập đó tương thích, có thể là subtotal/zero.
- `official_total_kwh` chỉ có khi đủ ngày/component **và** absolute basis gate;
  `official_total_kwh_m2` cần thêm common fixed basis. Missing/invalid/day gap
  làm official NULL; không scale/extrapolate. Tất cả accepted zero + basis đủ
  cho official0. Không có accepted/complete contributors → NULL, không0 giả.

DTO frozen và JSON codec v2 giữ Decimal string, enums, dates, publication versions,
correction và full provenance. Chưa có intensity/cost result DTO/service/UI.

## Kiểm chứng synthetic và tái lập

[Tests energy](../../tests/unit/test_energy_mart.py) dùng real migrations,
ingestion/core/pipeline/mart/transactions và disposable databases. Spark stage
được thay bằng prepared membership artifact của fixture, không phải benchmark
Spark/actual AGC. Expected values literal; không import main-owned oracle.

Fixture hai ngày: heat3.6/7.2MJ/m², high1/0, low0/1kWh/m²; energy100m²,
Production50m², ngày2 ProdA2kg/m². Nhiệt300kWh, điện200kWh, tổng500kWh;
ngày1 chưa harvest vẫn200kWh; accepted A2×50=100kg. Đây là các lượng đầu vào
cho bước intensity sau:500/100=5, không chấp nhận ratio per-m²5/2=2.5.
Nếu energy area ngày2 đổi200, total800kWh và official per-m² NULL. Tests kiểm
area intervals, no-fanout harvest, NULL/invalid/suspect/applicability/zero/gap,
assumption tắt/unknown/khác version, verified Production bridge, sensitivity,
50-digit heat conversion, same quantity identity, exact correction/lineage,
sealed immutability, đủ cả năm receipt, rollback/resume và idempotence.
Upgrade001–010→011 giữ basis versions/selections/checksums; migrate lại skip.

Chạy tuần tự trong Docker với source/tests read-only; actual publication AGC
vẫn là v5 `518aafbbff244e3f96b61a15252aa96a`, actual energy publication dành
cho bước nghiệm thu tiếp theo. Không cần migrate hay publish actual DB để chạy:

```sh
GREENHOUSE_RAW_PATH="$PWD/data/raw" docker compose run --rm --no-deps -T \
  -v "$PWD/src:/review-src:ro" -v "$PWD/tests:/workspace/tests:ro" \
  -e PYTHONPATH=/review-src:/workspace/tests/unit job \
  python -m pytest tests/unit/test_energy_mart.py -q -s
```

### Kết quả cuối

| Kiểm tra | Kết quả | Evidence |
|---|---|---|
| Full backend `tests/unit` | **295PASS74.58s**, EXIT0, peak565,161,984bytes/cap2GiB, CPU1.5 | [log](../evaluation/assets/energy-mart/backend-tests.txt), [state](../evaluation/assets/energy-mart/backend-state.json) |
| Full existing UI `tests/ui` | **68PASS39.84s**, EXIT0, peak171,532,288bytes/cap768MiB, CPU0.5 | [log](../evaluation/assets/energy-mart/ui-tests.txt), [state](../evaluation/assets/energy-mart/ui-state.json) |
| Actual AGC preservation, read-only | Current v5/seven publications, source counts/checksums001–010 unchanged; no actual energy publication/migration | [report](../evaluation/assets/energy-mart/actual-preservation.json) |

Hai suite cuối chạy tuần tự, cùng source/test state gồm251file,
SHA256 **`f0adf6c13083f3022ade60f7c1d616ed04501790a9e08e494e37a2950517b131`**,
không đổi trước/sau mỗi run. Manifest giữ hash từng file; không có functional
edit sau đó. Backend gồm migration upgrade/checksum, cả năm missing-receipt seal
cases, pipeline atomicity/resume/statistics và climate/water/harvest/drain/shared
contract regressions. UI run đầu EXIT1 do full test-root mount kéo vào backend
runtime reporter yêu cầu Java trong UI image; không dùng làm acceptance. Run cuối
giới hạn `--confcutdir=/workspace/tests/ui`, giữ UI fixtures/imports, không thêm
Java hoặc thay image/caps. Warning ScriptRunContext của AppTest là bare-mode
warning; suite cuối EXIT0. Log lỗi harness đầu giữ local để truy vết.

[Runner attestation](../evaluation/assets/energy-mart/test-state.py) dùng hashlib
trên source/tests trước và sau pytest; exit97 nếu source bị đổi giữa run. Có thể
tái lập các run cuối (output vào scratch để giữ evidence) như sau:

```sh
mkdir -p tmp/energy-mart-tests
GREENHOUSE_RAW_PATH="$PWD/data/raw" docker compose run --rm --no-deps -T \
  -v "$PWD/src:/review-src:ro" -v "$PWD/tests:/workspace/tests:ro" \
  -v "$PWD/docs/evaluation/assets/energy-mart/test-state.py:/check.py:ro" \
  -v "$PWD/tmp/energy-mart-tests:/evidence" \
  -e EVIDENCE_REPORT=/evidence/backend-state.json \
  -e PYTHONPATH=/review-src:/workspace/tests/unit job \
  python /check.py tests/unit -q -s

GREENHOUSE_RAW_PATH="$PWD/data/raw" docker compose run --rm --no-deps -T \
  -v "$PWD/src:/review-src:ro" -v "$PWD/tests:/workspace/tests:ro" \
  -v "$PWD/docs/evaluation/assets/energy-mart/test-state.py:/check.py:ro" \
  -v "$PWD/tmp/energy-mart-tests:/evidence" \
  -e EVIDENCE_REPORT=/evidence/ui-state.json -e PYTHONPATH=/review-src ui-test \
  python /check.py tests/ui --confcutdir=/workspace/tests/ui -q -s
```

Không công bố benchmark/intensity/chi phí AGC từ các fixture hoặc runtime checks
này. Python3.12.12/Linux aarch64; backend Spark4.0.1/OpenJDK17.0.17, psycopg3.3.7,
pytest8.4.2. Main-owned independent probes/oracles không là product dependency.
