# PostgreSQL core, metadata và chất lượng

SQL triển khai nằm trong [migrations](migrations/) và [sql](sql/); runner/loader nằm cùng package để T31 bọc adapter sau này. Không thêm dependency, ORM hoặc migration framework. Parser T06 vẫn độc lập DB. Các bảng hiện là **candidate core**, chưa có marts/KPI hoặc coordinator publication/recovery T08.

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
- Nạp cùng run bị từ chối trước ghi; nạp run khác chứa cùng source/revision cũng bị UNIQUE chặn và rollback. Không có resume, upsert correction hay publication trong loader này. COPY streaming tối đa 1,000 source records mỗi batch; mỗi batch có temp revision IDs riêng, mọi typed/quality/DQ query chỉ dùng IDs đó rồi xóa temp data. Không ANALYZE toàn core lặp lại; toàn bộ run vẫn một transaction; exception không để metadata/core nửa chừng. Audit/recovery xuyên run là T08.

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
- Basis/assumption/policy/correction là append-only. Bằng chứng mới tạo version mới; pointer áp dụng có thể chọn version mới, version cũ vẫn tồn tại. Publication sau này pin ID/version riêng, không phụ thuộc toggle hiện tại.

## Lineage, quarantine và publication

`meta.source_record` truy về snapshot/file checksum, physical line, raw hash/reference, adapter/time policy và run đầu. `meta.dq_issue` giữ cả lỗi parser ở source và đánh giá per-measure ở revision; số issue không phải số record lỗi duy nhất. `meta.quarantine_record` giữ disposition/reason/policy, correction có old/new/evidence/actor/version riêng và không tự áp lên raw.

Reference/Production.csv:2 vẫn quarantine `out_of_cycle_unresolved`. View `meta.harvest_ingestion_state.source_rows_resolved=false` là bằng chứng đầu vào cho T12; ngay cả true cũng chưa chứng minh harvest completeness vì T12 còn kiểm quality và published snapshot. Không dùng số ngày có harvest làm coverage.

T07 chỉ định nghĩa `meta.publication`, membership, basis pins và current pointer. Composite FK ngăn scope/key/revision/basis trộn; membership chọn một revision mỗi logical key. `core.published_measure` đọc qua pointer + membership + quality policy và basis **được pin của cùng publication**, không dùng basis toggle hiện tại. Loader không ghi các bảng publication; mặc định view này rỗng. App sau này chỉ dùng published views/contracts. `core.explain_measure`/`explain-core` là diagnostic candidate, không phải API app hoặc KPI đã công bố. Role/permission vận hành sẽ được rà soát cùng adapter/application, không cam kết user PostgreSQL superuser bị hạn chế đọc core.

T08 tiếp nhận publication transaction, membership/config pins, recovery và revisions. T31 tiếp nhận SQL adapter/contracts. T09–T21 tiếp nhận unit/quality/basis/completeness gates; không có nghiệm thu BR từ việc load core thành công.
