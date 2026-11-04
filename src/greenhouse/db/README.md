# PostgreSQL core, metadata và chất lượng

SQL triển khai nằm trong [migrations](migrations/) và [sql](sql/); runner/loader và query adapter nằm cùng package. Không thêm dependency, ORM hoặc migration framework. Parser T06 vẫn độc lập DB. Core có revisions bất biến; coordinator publish membership và config pins. Mart nước BR-02 đã có; các mart khác thuộc các task tiếp theo.

## Lệnh Compose

Sau `.env`, build và snapshot theo [hướng dẫn local](../../../docs/implementation/local-environment.md):

```bash
docker compose -p smart-greenhouse --profile test build test-runner
docker compose -p smart-greenhouse run --rm job python -m greenhouse migrate
docker compose -p smart-greenhouse run --rm job python -m greenhouse migrate
docker compose -p smart-greenhouse run --rm job python -m greenhouse ingest
# Lấy manifest_path/run_id từ JSON aggregate của ingest, thay <run_id> bên dưới.
docker compose -p smart-greenhouse run --rm job python -m greenhouse load-core --run /data/output/ingestion/<run_id>
docker compose -p smart-greenhouse run --rm job python -m greenhouse explain-core --compartment AICU --date 2020-01-01 --measure Irr --limit 5
docker compose -p smart-greenhouse --profile test run --rm test-runner
```

`setup` vẫn chỉ kiểm mount/DB, không tự migrate. `migrate` chạy SQL đánh số dưới một transaction/advisory lock; `meta.schema_migration` giữ SHA-256 từng file. Đã áp và checksum khớp → skip; file đã áp đổi checksum → fail, không sửa lịch sử. Thay schema/seed sau bàn giao phải thêm migration mới. SQL được đóng gói trong wheel/image.

`load-core` nhận đúng **một** run staging T06. Mặc định scope AGC historical, policy `AGC_QUALITY_V1`, snapshot từ raw mount đã khóa. Có thể truyền `--scope-id`, `--snapshot`, `--quality-policy` khi metadata đã được tạo có nguồn (fixture synthetic dùng scope riêng). Không tự đoán mapping folder/khoang hoặc tự tạo basis cho nguồn mới. Import/raw read-only; staging trong volume outputs; SQL dùng database Compose đã cấu hình. Validation dùng database trống riêng để không xóa dữ liệu đang có.

## Contract loader và nguồn

- Đối chiếu snapshot identity, toàn bộ file set/checksum/expected rows với raw snapshot manifest và counts cấp run trước khi đọc dữ liệu; không cho lược file khỏi manifest để giấu phần thiếu. Chỉ đọc các output có tên trong manifest, không glob. File phải `loaded`/`loaded_with_quarantine`, `outputs_consumable` không được false và source run phải kết thúc. `failed`/`deferred` lưu vào `meta.file_ingestion`; run failed chỉ nhập metadata chẩn đoán (`blocked`), không đọc bất kỳ output nào. Run running bị từ chối.
- Resources, Production, GreenhouseClimate được đưa vào typed core. Một source row Climate sinh hai revision climate/drain riêng, cùng source record. LabAnalysis hiện chưa có model trong thiết kế 05: giữ ở raw/staging T06, ghi core status `deferred` dù ingestion status loaded; không làm mất context hoặc nhận nó là drain telemetry.
- Loader đọc lại source parser v1 trên raw file đúng checksum và so từng record với staging: header/adapter, raw text/tokens/hash, physical line start/end, snapshot/file/run/source identity, Decimal values, source timestamp/slot/residual và issues. JSONL bị sửa hoặc thiếu/thừa → transaction rollback. Gzip fragment của failed file không được đọc.
- Khóa source record ổn định từ T06. Revision đầu có số 1, ID MD5 của `scope:source_record_id:record_type:1` (ID kỹ thuật, không dùng làm checksum bảo mật); raw/file/snapshot vẫn SHA-256. Logical key có cycle prefix; Harvest dùng source record ID để không khóa cứng một harvest/ngày. Các CHECK/FK/unique/index bảo vệ snapshot/scope, mapping source/cycle, type, slot/ngày và version.
- Record accepted được nạp. Quarantine **chỉ** do `invalid_numeric`, còn time/header/width hợp lệ, vẫn tạo typed core với quality invalid ở measure lỗi và giữ các measure khác dùng được; quarantine/issue gốc còn nguyên. Conflict, lỗi time/header/CSV hoặc ngoài vụ không tạo core revision. Không chọn first winner hoặc chữa năm.
- Nạp cùng run bị từ chối trước ghi; nạp run khác chứa cùng source/revision cũng bị UNIQUE chặn và rollback. Loader thấp cấp này không có resume/correction/publication; dùng `run-pipeline` bên dưới cho recovery và reuse. COPY streaming tối đa 1,000 source records mỗi batch; mỗi batch có temp revision IDs riêng, mọi typed/quality/DQ query chỉ dùng IDs đó rồi xóa temp data. Không ANALYZE toàn core lặp lại; toàn bộ run vẫn một transaction; exception không để metadata/core nửa chừng. Coordinator bên dưới xử lý audit/recovery xuyên run.

## Schema và policy

Chi tiết mapping bảng trong [core schema](../../../docs/architecture/05-core-schema.md) và [metadata schema](../../../docs/architecture/06-metadata-schema.md). Core giữ NUMERIC và đơn vị nguồn, không đổi MJ thành kWh. Unit catalog trong `config.measure_definition`; bảng resource/harvest đặt tên cột theo đơn vị. FK measure/type và finite checks không cho NaN/Infinity vào typed core.

Source time dùng `timestamp without time zone`. Giữ **Decimal serial gốc**, token gốc, timestamp T06 đã truncate microsecond và `time_precision=staging_truncated_us`; không âm thầm round lại timestamp. Canonical slot được xác minh bằng parser/Decimal v1, khác với timestamp nguồn. Cột ISO year/week sinh từ canonical time. Biên cuối vụ lưu được cho Climate/drain nhưng `in_cycle=false`, `usable_value=NULL`; không thêm duration. Clock vận hành dùng timestamptz, gồm các cột published/received trong sensor-event schema dự phòng; không tạo event replay ở đây.

`AGC_QUALITY_V1` có hiệu lực theo cửa sổ phân tích (cho phép đánh giá điểm biên để giữ metadata, không mở thêm slot). Definition/hash, nguồn và nhãn demo được version hóa bất biến:

| Trường hợp | Quality / applicability / reason |
|---|---|
| NULL lexical từ T06 | missing / unknown / lý do empty, NaN, NA hoặc N/A |
| Numeric không parse được nhưng phần còn lại của record dùng được | invalid / unknown / invalid_numeric; không ép 0 |
| Irr, Drain, ProdA/B hoặc EC drain âm | invalid / applicable / negative_quantity; giữ giá trị gốc |
| pH drain = 0 | suspect / applicable / demo_zero_ph_suspect; nhãn demo, không khẳng định lỗi đo tuyệt đối |
| CO₂ requested/realized setpoint = 0 | accepted về numeric / **unknown** / off_mode_unverified; usable_value NULL |
| Hữu hạn còn lại | accepted / applicable; không đồng nghĩa trong mục tiêu hoặc xác thực nông học |

Không coalesce requested/realized, không forward-fill, không phân loại out-of-target trong core. `usable_value` của diagnostic chỉ mở khi quality accepted, applicability applicable và trong vụ; metric sau này còn phải kiểm basis/coverage/completeness riêng.

## Basis và giả định

Seed theo [quyết định user](../../../docs/implementation/data-decisions.md#7-br-04--quyết-định-basis-của-user), có nguồn cho từng hằng số:

- Site/team/khoang dùng danh tính folder và ReadMe p.1, không bịa số khoang, địa điểm, cultivar hoặc diện tích sàn thực của Reference. Cycle window là quy ước phân tích T03.
- Production A/B có basis 62.5 m² `verified` (p.9), context `source_verified`.
- `AGC_SHARED_BASIS_V1` v1: 62.5 m² `assumed_unverified`, context `shared_analysis`, bảy measure × sáu khoang × cửa sổ đã chọn. Nguồn p.1/p.9 + user 2026-11-04; mở rộng sang Resources là giả định. Explain giữ status, ID/version và nhãn “giả định chưa xác minh”.
- `AGC_FLOOR_96_SENSITIVITY_V1`: basis riêng 96 m² cho energy/water, Production giữ verified 62.5, context `sensitivity`; cả application và assumption đều **tắt mặc định**. Đây là phương án độ nhạy ×1.536 đã yêu cầu, không phải bằng chứng diện tích nguồn.
- `config.basis_application` chọn tường minh một version cho scope/khoang/measure/context; interval của basis quyết định hiệu lực, không tự lấy latest. Tắt application hoặc assumption → không có basis được áp dụng, reason `incompatible_or_unknown_basis`; tổng observed per-m² vẫn giữ. Fixture synthetic chỉ dùng basis verified do fixture khai báo, không nhận seed AGC.
- Basis/assumption/policy/correction là append-only. Bằng chứng mới tạo version mới; pointer áp dụng có thể chọn version mới, version cũ vẫn tồn tại. Publication pin ID/version riêng, không phụ thuộc toggle hiện tại.

## Lineage, quarantine và publication

`meta.source_record` truy về snapshot/file checksum, physical line, raw hash/reference, adapter/time policy và run đầu. `meta.dq_issue` giữ cả lỗi parser ở source và đánh giá per-measure ở revision; số issue không phải số record lỗi duy nhất. `meta.quarantine_record` giữ disposition/reason/policy, correction có old/new/evidence/actor/version riêng và không tự áp lên raw.

Reference/Production.csv:2 vẫn quarantine `out_of_cycle_unresolved`. View `meta.harvest_ingestion_state.source_rows_resolved=false` là bằng chứng đầu vào cho T12; ngay cả true cũng chưa chứng minh harvest completeness vì T12 còn kiểm quality và published snapshot. Không dùng số ngày có harvest làm coverage.

Các bảng `meta.publication`, membership, basis pins và current pointer giữ identity của bản công bố. Composite FK ngăn scope/key/revision/basis trộn; membership chọn một revision mỗi logical key. `core.published_measure` đọc qua pointer + membership + quality policy và basis **được pin của cùng publication**, không dùng basis toggle hiện tại. Loader không ghi publication; `run-pipeline`/`publish` ghi bằng coordinator. App sau này chỉ dùng published views/contracts. `core.explain_measure`/`explain-core` là diagnostic candidate, không phải API app hoặc KPI đã công bố. Role/permission vận hành sẽ được rà soát cùng adapter/application, không cam kết user PostgreSQL superuser bị hạn chế đọc core.

T31 tiếp nhận SQL adapter/contracts. T09–T21 tiếp nhận unit/quality/basis/completeness gates; không có nghiệm thu BR từ việc load core thành công.

## Workflow, chạy lại và publication

```bash
docker compose -p smart-greenhouse run --rm job python -m greenhouse run-pipeline --workflow-id batch-first
# Run mới cùng raw snapshot: kiểm chứng baseline, reuse revisions và publication nếu không đổi.
docker compose -p smart-greenhouse run --rm job python -m greenhouse run-pipeline --workflow-id batch-repeat
# Sau lỗi, dùng cùng input/output/scope/policy và đúng workflow ID:
docker compose -p smart-greenhouse run --rm job python -m greenhouse run-pipeline --resume batch-first
# Adopt staging đã nạp bởi load-core; vẫn kiểm hash, parser, keys và exact values:
docker compose -p smart-greenhouse run --rm job python -m greenhouse run-pipeline --workflow-id adopted --ingestion-run /data/output/ingestion/<run_id>
docker compose -p smart-greenhouse run --rm job python -m greenhouse publish --workflow-id adopted
```

`meta.workflow_run`/`workflow_step` bao bốn step ingest → load-core → spark → publish. Mỗi step giữ attempt/status, timestamps, rows in/out, parser/quality/basis/code versions, artifact SHA-256 và lỗi có type (không ghi DSN). JSONL log ở `/data/output/pipeline/<workflow_id>/steps.jsonl`. API workflow yêu cầu psycopg connection idle, autocommit=true; mỗi chuyển trạng thái dùng transaction tường minh. Session advisory lock bảo vệ một workflow khỏi chạy đồng thời; transaction advisory lock chung theo scope serialize load, correction và publish.

Resume xác minh SHA-256 manifest và mọi file được khai báo, raw manifest/files, rồi skip step completed; chỉ step chưa completed được retry. Step thất bại để nguyên raw, staging, artifact và thư mục pending chẩn đoán. Lần thử tiếp có tên batch/attempt mới; không xóa hoặc ghi đè nguồn. Nếu artifact đã hoàn tất bị sửa hoặc config đổi, fail và cần workflow mới; không lặng lẽ chạy lại step completed. Sau commit publication nhưng trước khi client nhận kết quả, resume trả đúng publication đã lưu.

CLI AGC kiểm snapshot với ZIP đã khóa SHA-256 như ingest trước khi chạy; API Python nhận manifest đã được caller xác minh (fixture tự khai báo snapshot riêng). Baseline reuse được kiểm tường minh: parse lại các source file core từ T06, đối chiếu staging/raw, rồi fingerprint đa tập `(revision ID, logical key, raw_hash, measure, exact numeric/null)` theo parser/policy. Fingerprint là tổng modulo 2^256 của SHA-256 từng tuple + count, không giữ toàn bộ records trong RAM, không dùng làm khóa record. Lần đầu adopt T07 đối chiếu fingerprint với core NUMERIC bằng cursor 1,000 rows; receipt bất biến cho phép các rerun sau không quét lại toàn bộ baseline DB. Typed values/quality/revisions bất biến; khác baseline bị từ chối. Run ingest mới có metadata riêng và ghi reused, không chèn source/revision/quarantine bản sao. Duplicate và conflict **trong file** giữ nguyên quy tắc T06.

Quyết định user: snapshot raw khác phải có `data_scope` riêng, không matching/merge xuyên snapshot và không tự mở rộng basis. “Changed content” trong snapshot khóa là **correction tường minh**, không sửa CSV. `correct-core` nhận source_record_id, subtype, full old/new measure maps (decimal strings hoặc null), actor, evidence và idempotency key. Nó khóa scope, kiểm old_value so với revision hiện hành, append `meta.correction` version n+1 và đúng một typed revision n+1 + quality. Source ID/raw/hash/time/basis giữ nguyên. Cùng correction ID/payload trả revision cũ; stale old_value bị từ chối. Correction không tự chữa Reference timestamp ngoài vụ.

```bash
# old.json/new.json ở volume outputs, gồm đủ measure codes của subtype.
docker compose -p smart-greenhouse run --rm job python -m greenhouse correct-core --source-record-id <source_id> --record-type resource --old-value /data/output/old.json --new-value /data/output/new.json --actor reviewer --evidence 'documented correction' --correction-id <stable_id>
docker compose -p smart-greenhouse run --rm job python -m greenhouse run-pipeline
```

Spark local[1] đọc metadata của **candidate revisions đã freeze** bằng psycopg server cursor 1,000 rows → input JSONL tường minh → Parquet. Timestamp trong Parquet là source-naive text, numeric vẫn ở PostgreSQL NUMERIC; không ép sang Spark Decimal(38). Driver 768 MiB, maxResultSize 64 MiB, shuffle partitions 4, không collect/cache toàn dữ liệu, mỗi part tối đa 50,000 records. Staging nằm ở `/data/output/pipeline/<workflow_id>/spark-<attempt>-<batch>/`, ghi `.pending-*` rồi atomic rename cùng filesystem sau Parquet count check, profiling và checksum manifest. Pending không phải artifact hoàn tất. Spark không publish/JDBC-write bảng public.

Profiling: `profile-days.jsonl` theo khoang × measure × ngày (có cả ngày trống trong calendar vụ), `profile-summary.json` theo khoang × measure. Count/null, missing/invalid/suspect/accepted, applicability unknown và min/max dùng exact NUMERIC; biên đóng vụ bị loại. Expected 288 slot/ngày climate/drain, 1 resource/ngày; harvest là event-grain, không suy coverage từ số ngày có harvest. Min/max observed còn chứa suspect/invalid; quality phân bố riêng. Đây là profiling ban đầu, không có area conversion, interpolation hoặc KPI nghiệm thu.

Coordinator kiểm upstream completed và hashes, candidate membership khớp Spark, subtype và quality đủ, config/basis pins tồn tại và không drift, candidate còn là latest revisions. Trong **một transaction, một psycopg connection**: scope lock → publication → membership → basis pins → water mart hook → optional `hook(conn, publication_id, workflow_id)` → immutable seal → current pointer + workflow/step completed. Hook bắt buộc dùng connection nhận vào, không mở JDBC/connection khác hoặc commit; exception rollback cả mart/membership/pointer. Water hook bắt buộc chạy trước optional hook; đổi semantic/code/hook bundle phải tăng `CODE_VERSION` để no-change không bỏ qua mart mới. Membership/config không đổi → reuse publication có lý do, không thêm membership. Finality hiện provisional; chưa tuyên bố completeness BR.

`pin_publication(conn, scope)` đọc ID một lần; mọi query trong tác vụ dùng ID đó (`pinned_values` là helper diagnostic hiện tại), không đọc lại current pointer giữa chart/export. `core.published_measure` tiện cho một query hiện hành; tác vụ nhiều query phải pin. Seal chặn sửa/thêm/xóa membership/basis của publication cũ; policy/typed values cũng append-only. Đây là invariants qua coordinator/constraints, chưa phải phân quyền hạn chế PostgreSQL superuser.

`--fault` chỉ dành cho validation: `before-<step>`/`after-<step>` (ingest/load-core/spark), `before-publish`, `spark-written`, `spark-before-rename`, `publish-members`, `publish-hook`, `publish-pointer`. Fault trong transaction không để pointer mới hiện ra; không tạo fault data scope hoặc thay raw.

## Kiểm raw lineage

```bash
docker compose -p smart-greenhouse run --rm job python -m greenhouse lineage --revision-id <revision_id>
```

Lệnh trả revision/correction/scope/snapshot/file/physical line range/raw_hash, đọc file raw đúng SHA-256 rồi parser xác minh lại hash và source identity ở record đó. Với publication, lấy revision từ membership đã pin rồi gọi lineage. Không ghi raw payload vào log. File/hash/line không khớp → fail; source timestamp vẫn truncate µs theo T06.

## Adapter query cho ứng dụng

[PostgresQueryAdapter](query_adapter.py) thực thi [query Protocol](../queries/README.md) và trả [DTO thuần Python](../contracts/README.md). Adapter mặc định đọc sealed water mart; `PostgresCoreQueryAdapter` giữ đường core-oracle để đối soát. Các SQL `query_*.sql` tham số hóa theo publication ID, không đổi migration/SQL T07/T08 đã áp. Chỉ `pin_publication` đọc current pointer; các lần đọc sau dùng ID đó, quality và basis pin, kể cả khi pointer hoặc basis toggle đã đổi. Connection do caller sở hữu.

Adapter validate catalog water `L/m²/day`, trả lượng ngày BR `L/m²` cùng source_unit; không đổi quantity. Missing quality/unit, duplicate day/measure hoặc context sai scope/snapshot/cycle/compartment fail. Service giữ formula, calendar gaps, coverage, NULL path, assumption label và rule-not-configured. Lineage kiểm membership trước khi gọi raw-hash verifier T08; ambiguous logical key cần revision ID. Không có SQL trong service/DTO/UI. Không cần migration mới; AGC smoke có thể chỉ SELECT trên publication hiện có.

## Mart nước và rule profiles

Migration 005 thêm water mart, immutable rule config và unique membership index `(publication_id,revision_id)` cho FK fact → selected revision. Không sửa 001–004. Sau migrate, chạy **workflow mới** để có mart; publication lịch sử thiếu bundle bị adapter báo lỗi rõ, không fallback core hoặc backfill vào publication đã seal.

```bash
docker compose -p smart-greenhouse run --rm job python -m greenhouse migrate
docker compose -p smart-greenhouse run --rm job python -m greenhouse run-pipeline --workflow-id water-first
# transform là alias thật của run-pipeline, nhận cùng flags/exit status.
docker compose -p smart-greenhouse run --rm job python -m greenhouse transform --workflow-id water-repeat
```

`materialize_irrigation` là hook mặc định của cả `run-pipeline`/`publish` API và CLI, cùng transaction/connection. Một SQL statement đọc resource membership + quality policy + shared-analysis basis pins của publication, ghi immutable dimensions/facts/receipt. Receipt giữ bundle version, counts và hook start/finish. Source unit khác `L/m²/day` làm publish rollback; quantities không đổi số khi trả nhãn daily `L/m²`. Decimal SQL helper dùng exact div/remainder và half-even 50 significant digits khớp oracle. Sealed facts/receipts/cycle dimensions không được thêm/sửa/xóa; default adapter yêu cầu receipt `irrigation-mart-v1`.

`register_irrigation_rule(conn, IrrigationRuleProfile(...))` chèn version tường minh vào config bằng parameterized SQL. Profile có version ID, logical rule ID/version, scope/compartment đã mapping, min/max drain %, min/max daily irrigation L/m², evidence, is_demo/label. DTO và CHECK/FK/trigger kiểm finite/nonnegative/ordered bounds, units, scope và immutable version. Demo phải có label `demo_rule`. Không có default AGC thresholds, không chọn latest profile ngầm. Muốn dùng profile phải truyền ID qua `QueryContext.rule_version`; source publication/facts không thay. Xem [query service](../queries/README.md) và [contracts v2](../contracts/README.md).
