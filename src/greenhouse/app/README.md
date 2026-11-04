# Ứng dụng nước (BR-02) — màn hình tối thiểu

Ứng dụng production chạy Streamlit + Plotly trong image UI. Mọi phép tính nằm ở
[IrrigationService](../queries/README.md) + [PostgresQueryAdapter](../db/README.md#adapter-query-cho-ứng-dụng);
UI không nhúng SQL và không tính lại công thức. Bộ lọc lấy từ
[catalog adapter](../db/catalog.py) qua [CatalogService](../queries/catalog.py) — không dán ID thủ công.

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
- JSON summary: envelope contract **v2** (`contract_version` + `data`) từ codec
  contracts, không đổi schema.
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
- Xếp hạng/nhiều khoang, harvest, scenario chưa thuộc màn hình này; prototype
  `prototypes/dashboard` là demo fixture-only, tách biệt.
