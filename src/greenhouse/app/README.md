# Ứng dụng nước (BR-02) + thu hoạch (BR-05) — màn hình tối thiểu

Ứng dụng production chạy Streamlit + Plotly trong image UI. Mọi phép tính nằm ở
[IrrigationService](../queries/README.md) / [HarvestService](../queries/README.md) +
[PostgresQueryAdapter](../db/README.md#adapter-query-cho-ứng-dụng) /
[PostgresHarvestQueryAdapter](../db/harvest_adapter.py); UI không nhúng SQL và không
tính lại công thức. Bộ lọc lấy từ [catalog adapter](../db/catalog.py) qua
[CatalogService](../queries/catalog.py) — không dán ID thủ công. Entry:
[home.py](home.py) có radio chọn màn hình: nước (irrigation-mart-v1) và thu hoạch
(harvest-mart-v3; [harvest.py](harvest.py)).

## Chạy

```bash
cp .env.example .env            # đặt GREENHOUSE_DB_PASSWORD
docker compose -p smart-greenhouse --profile '*' build
docker compose -p smart-greenhouse run --rm job python -m greenhouse migrate
docker compose -p smart-greenhouse run --rm job python -m greenhouse run-pipeline --workflow-id water-first
docker compose -p smart-greenhouse up -d --wait db app
```

Trước `run-pipeline` cần snapshot đã khóa: xem [chuẩn bị snapshot AGC](../../../docs/implementation/dataset-snapshot.md)
(lệnh `snapshot`/`dataset-download` đặt raw vào `data/raw/`). Clean flow đầy đủ:
build → migrate → snapshot → ingest/pipeline → `up -d --wait db app`.

Mở **http://127.0.0.1:8501** (đổi cổng bằng `GREENHOUSE_APP_PORT`). App service
mount raw read-only (`GREENHOUSE_RAW_PATH` → `/data/raw`) chỉ để xác minh lineage:
`raw_snapshot_mapping` nhận diện snapshot **từ manifest.json bên trong thư mục**
(không đoán theo tên), hỗ trợ cả snapshot root lẫn layout khóa `raw/<dataset>/<sha>`;
manifest lỗi/không đọc được/trùng snapshot_id bị bỏ qua — các snapshot đó hiển thị
**chưa xác minh được**, không bịa "đã xác minh". Việc băm file/record vẫn nằm ở lineage.

## Luồng một tác vụ (mỗi rerun)

1. Đọc catalog: publication hiện hành theo scope + receipt water mart
   (`usable = bundle irrigation-mart-v1`). Publication cũ chưa có mart → trạng thái
   lỗi kèm hướng dẫn chạy workflow mới; app không tự publish/backfill, không fallback fixture.
2. Chọn publication → pin **theo ID** đúng một lần (`adapter.publication(id)`);
   day/weekly/summary/lineage/export dùng lại ID đó, không đọc lại pointer giữa tác vụ.
3. Chọn khoang/vụ (một cycle + một khoang mỗi context) và cửa sổ ngày trọn vẹn;
   đầu UI là ngày inclusive, đổi sang half-open chỉ ở `wiring.context_window` (một chỗ duy nhất).
4. Rule: mặc định `not_configured` (không có kết luận đạt/không đạt); chọn được
   profile immutable hợp lệ cho scope/khoang từ catalog. Đổi scope/vụ reset ngày + rule.
5. KPI, biểu đồ ngày (NULL để trống, không vẽ về 0), bảng tuần ISO (ratio of sums,
   có tử/mẫu paired), chi tiết ngày + lineage, xuất CSV/JSON.

## Xuất CSV/JSON

- CSV ngày (`*-days.csv`) và CSV tuần (`*-weekly.csv`): tái sử dụng đúng DTO đang hiển thị.
- Decimal giữ **exact string** (không làm tròn), NULL → ô rỗng, `0` giữ `'0'`,
  bool → `true`/`false`, tuple → `a;b`. Làm tròn chỉ xảy ra trên màn hình.
- JSON summary (nước): envelope contract **v2** (`contract_version` + `data`) từ codec
  contracts, không đổi schema. JSON summary của harvest dùng wrapper
  `harvest_export_version: 1` (`export_kind: window_summary`; body `summary` giữ
  nguyên envelope v2 ở trên) — không nới lỏng codec.
- Metadata dưới dạng dòng `# key=value` trước header: đơn vị (`quantity_unit=L/m²`,
  `ratio_unit=%`), scope/snapshot/publication/finality/committed_at, cycle/compartment,
  `requested_window`/`effective_window` (nửa mở), time basis, **status/expected days/
  accepted days/coverage từng measure+paired/paired numerator+denominator/day counts**,
  versions (pipeline run, quality policy, parser, code, config bundle, metric), basis
  status/label/assumption ID+version, rule status/version/profile (kèm numeric version,
  evidence, demo) và corrections nếu có — cùng áp cho CSV ngày và CSV tuần.
- Không repin khi tải; file tên theo khoang + cửa sổ đã chọn.

Cột CSV ngày: `resource_date`, `irrigation_l_m2`, `drain_l_m2`, chất lượng/applicability/
reason/source unit/basis (status, version id, area, kind, assumption id+version, label)
cho Irr và Drain, `paired`, `drain_fraction_pct`, `drain_exceeds_irrigation`, `rule_status`,
`rule_version`, `revision_id`, `logical_record_key`, `correction_id`, `correction_version`,
`reason_codes`.

Cột CSV tuần/summary: `iso_year`, `iso_week`, `window_start`, `window_end`,
`expected_days`, tổng observed và paired Irr/Drain, `drain_fraction_pct`, ba coverage,
các count ngày (accepted/unknown/suspect/drain>irr/outside_rule/rule_unknown),
`status`, `comparison_eligible`, basis/assumption ids+versions, `corrections`,
rule metadata, đơn vị, time basis, identity publication/scope/snapshot, versions, `reason_codes`.

## Kiểm thử

```bash
docker compose --profile test run --rm ui-test          # tests/ui (AppTest + service/export)
docker compose --profile test run --rm test-runner      # bộ backend hiện có
```

`ui-test` chạy trong image UI (pytest/psycopg trong `requirements-ui.lock`), tạo database
fixture có prefix `test_ui_*` và tự drop; giữ bằng `GREENHOUSE_KEEP_TEST_DB=1`.
AppTest chạy `home.py` thật với DB fixture (không Spark — stage thay bằng artifact nhỏ).

## Giới hạn T10

- Một khoang mỗi lần xem (context một cycle + một khoang); so sánh nhiều khoang thuộc task sau.
- Không caching kết quả (mỗi rerun truy vấn lại; cache phải mang đủ identity publication/
  context/version — chưa cần cho quy mô hiện tại).
- Connection mở mỗi rerun, không cache resource (an toàn đa session).
- Xếp hạng/nhiều khoang, scenario chưa thuộc màn này; prototype
  `prototypes/dashboard` là demo fixture-only, tách biệt.

## Màn hình thu hoạch (BR-05, T13)

[harvest.py](harvest.py) mở rộng cùng luồng tác vụ cho BR-05:

1. Publication cần **harvest-mart-v3**: publication chỉ có water vẫn xem được ở màn
   nước nhưng không hiện ở màn harvest; thiếu bundle → trạng thái lỗi tường minh
   (không tự publish/backfill).
2. KPI: `A observed`/`B observed` (Σ theo measure) vs `Tổng paired A+B`
   (chỉ record đủ cặp) vs `Share B` (100×ΣB/Σ(A+B) trên CÙNG tập paired; NULL khi
   mẫu số 0/không usable — không trung bình share từng record). Counts: read/
   accepted/quarantine/duplicate/conflict, usable per measure, event dates, chưa
   phân giải.
3. Snapshot chưa hoàn chỉnh: cảnh báo + không xếp hạng; không cộng per-m² giữa các
   khoang; ngày không event: complete → known 0 (record_present=false), incomplete →
   NULL/unknown.
4. Navigation cumulative: `cumulative_origin` (đầu vụ) hiển thị riêng trên chart;
   record sau cửa sổ không góp; zoom không mang reason clipping
   (`cumulative_from_cycle_start`), cắt biên thật → `window_clipped_to_cycle`.
5. Rule (tùy chọn, version tường minh): `not_configured` mặc định; chọn profile demo
   `harvest-demo-b10:*` có nhãn demo/evidence; share B > ngưỡng được flag ở record/
   day/week, bằng ngưỡng nằm trong dải, không denominator → unknown.
6. Chi tiết record: correction identity, lineage về raw (hash xác minh qua mount
   read-only; lựa chọn record re-resolve theo identity bền vững trên mỗi render nên
   kết luận dòng luôn theo DTO hiện hành); excluded provenance + audit raw có xác minh
   (hiển thị token raw sau khi file+record khớp; thiếu/drift → unverified tường minh,
   không giá trị bịa).
7. Xuất: CSV records/days/weekly (khai báo `evaluation_grain` và ngữ nghĩa verdict
   trong metadata) + JSON summary là wrapper `harvest_export_version: 1` với
   `export_kind: window_summary` và body `summary` giữ nguyên envelope canonical
   `{contract_version: 2, data: …}` (water JSON vẫn là direct v2) + CSV excluded
   provenance + CSV audit theo từng item; cột/metadata chung theo mục xuất dưới đây.

Xuất CSV harvest (v1): `harvest_date`, A/B, chất lượng/basis từng measure, `paired`,
`class_b_share_pct`, `rule_status`/`rule_version`, correction id/version,
revision/key, `reason_codes`. Summary/tuần: ISO week, observed/paired/total từng
measure, shares, accepted/paired records, cumulative a/total + origin + label, rule
metadata + counts, trạng thái completeness + counters, reasons, unit/metric/versions.
CSV excluded: identity/disposition/reason/issue_column/relative_path/line/serial
(không giá trị quarantined); CSV audit: chỉ verified mới có token raw, metadata mang
đầy đủ publication/cycle/khoang/cửa sổ yêu cầu và pipeline/quality/parser/code/config
versions của publication đã pin.
