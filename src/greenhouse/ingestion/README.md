# Ingestion: parser, staging và manifest theo run

Parser Python thuần ở [parser.py](parser.py), writer staging ở [run.py](run.py). Không có SQL, core tables, `AreaBasis`, quality policy hoặc KPI ở bước này. Dùng filesystem JSONL gzip trong named volume `outputs` để giữ contract ingestion độc lập DB; T07/T08 sẽ xây core/publication/recovery, T31 cung cấp adapter PostgreSQL. `accepted` ở đây nghĩa là parse được, không phải tất cả measure đã đạt quality hoặc cycle đã complete.

## Chạy bằng Compose

Chuẩn bị `.env` và [snapshot đã khóa](../../../docs/implementation/dataset-snapshot.md), sau đó:

```bash
docker compose -p smart-greenhouse --profile tools build snapshot
docker compose -p smart-greenhouse --profile tools run --rm snapshot
docker compose -p smart-greenhouse run --rm job python -m greenhouse ingest
```

Lệnh ingest đọc import/raw **read-only**, xác minh ZIP size/SHA-256, layout và snapshot/manifest so với ZIP; không cần Internet. Service job kế thừa dependency DB để dùng chung các lệnh setup sau này, nhưng parser/writer không kết nối DB. Không dùng snapshot writer để ingest khi cần bảo đảm raw mount read-only.

Mặc định output `/data/output/ingestion/<run_id>/` trong volume `outputs`. Stdout chỉ có aggregate theo file/run và đường dẫn manifest; không in dòng nguồn. Có thể truyền `--run-id <id>` (1–64 ký tự chữ/số/underscore/hyphen), `--output-root <path>` ngoài raw, `--snapshot <path>` hoặc `--archive <path>`. CLI chỉ nhận snapshot đúng archive AGC đã khóa. Run ID mặc định UUID; run đã tồn tại bị từ chối, không xóa/ghi đè. Khi dùng `--output-root`, người vận hành phải mount nơi ghi riêng với import/raw.

```text
ingestion/<run_id>/manifest.json
                  accepted/<source_file>.jsonl.gz
                  quarantine/<source_file>.jsonl.gz
                  duplicate/<source_file>.jsonl.gz
```

Các file JSONL giữ lineage từng record: snapshot ID/SHA, source path/checksum, physical line start/end (1-based, header là dòng 1), run ID, stable source_record_id, adapter ID/version, parser/time policy version, raw tokens/text/hash và typed values. `raw_hash` SHA-256 của bytes UTF-8 của logical record, gồm CRLF; raw snapshot vẫn là nguồn byte gốc. Header nguyên văn/normalized và BOM nằm trong file manifest. Decimal tuần tự hóa thành chuỗi thập phân chính xác, timestamp ISO không timezone; đọc lại bằng Decimal/datetime, không float.

## Nguồn được đọc và nguồn deferred

- Đọc đủ cột của Resources, Production, GreenhouseClimate và LabAnalysis cho sáu khoang. Climate cung cấp cả actual/requested/realized và EC/pH drain; LabAnalysis là context mẫu lab riêng, không được forward-fill thành drain 5 phút.
- CropParameters, GrodanSens, Weather, TomQuality và hai PDF giữ nguyên trong raw, được ghi `deferred` theo từng file với checksum/expected rows. Đây không phải file đã nạp đủ. TomQuality không phục vụ BR-05 hiện hành; Reference header 7/data 8 được deferred, **không zip/truncate hoặc tự repair**. Adapter riêng chỉ cần khi mở rộng phạm vi.
- Không tạo Site/Compartment/Cycle hoặc áp dụng `AGC_SHARED_BASIS_V1`; T07 tiếp nhận metadata/giả định từ [quyết định dữ liệu](../../../docs/implementation/data-decisions.md).

## Contract parser và adapter v1

- Đọc `utf-8-sig`, `newline=""`, CSV strict. Header trim/collapse whitespace, chỉ alias `%time`/`%Time` thành `time`; tên measure phân biệt hoa/thường. Phát hiện collision sau normalize và missing/extra columns. Kiểm width **trước** mapping; không silently drop cột.
- Numeric dùng Decimal hữu hạn. Blank, NaN, NA, N/A (case-insensitive, sau trim) → NULL với `empty`/`nan_token`/`na_token`/`n_a_token`. Raw token giữ nguyên. Ký hiệu khác hoặc infinity → `invalid_numeric`, quarantine record; không ép 0. Missing time luôn quarantine; missing measure là NULL có reason để T07 đánh giá theo measure. Zero/âm/setpoint lạ chưa bị gọi là lỗi nghiệp vụ/off-mode bởi parser.
- Excel epoch `1899-12-30`; timestamp nguồn naive, không dịch UTC/DST/năm. Climate dùng nearest slot Decimal `round(serial×288)` và residual theo policy `agc-source-naive-grid-v1`; >0.5 giây → `off_grid_time`, không snap. Source timestamp và canonical slot giữ riêng. Daily source cần serial nguyên; fractional day bị báo lỗi, không lùi/đổi ngày. Điểm climate có canonical slot đúng cuối vụ được lưu `is_close_boundary`, không chứng minh có duration ngoài vụ.
- `agc-standard` version 1 cho header thông thường. `agc-iuacaas-resources-empty-tail` version 1 chỉ áp dụng `IUACAAS/Resources.csv` khi raw header đúng signature 7 cột nguồn + 5 header rỗng, tất cả row width 12 và tail hoàn toàn blank sau trim. Adapter preflight toàn file; tail sai ở dòng sau làm **toàn file failed**, mọi row quarantine, kể cả các dòng trước. Chỉ typed projection bỏ 5 cột; raw vẫn đủ 12. File khác không được hưởng quy tắc truncate này.
- Production ngoài `[2019-12-16,2020-05-30)` quarantine `out_of_cycle_unresolved`, policy `agc-production-window-v1`; Reference dòng 2 giữ năm/serial gốc. Parser không sửa ngày, không xác nhận harvest completeness.
- Lỗi CSV lexical không thể resync an toàn: ghi một quarantine record cho fragment từ physical line đầu đến cuối bị tiêu thụ, giữ raw text; file `failed`. Một fragment lỗi tính là một read attempt, không giả số dòng logic ẩn trong quote chưa đóng. Header lỗi được giữ ở file manifest, không tính như data row. UTF-8 không giải mã được làm file failed (`invalid_utf8`), không có classified records được tính; raw/checksum/expected rows vẫn giữ. Không tiêu thụ bất kỳ staging fragment nào của file/run failed.

## Duplicate, conflict và counts

Logical key trong **một source file của một run** là ngày nguồn (daily sources) hoặc canonical slot (Climate). Fingerprint so sánh vector raw CSV tokens; newline bytes không ảnh hưởng so sánh, nhưng lexeme nguồn khác nhau cùng slot là conflict. Không khử trùng giữa các file/khoang hoặc giữa các run ở bước này.

Hai pass tạo compact key index rồi ghi staging: với record không có lỗi parse/policy, một key chỉ có một content → dòng đầu accepted, lần lặp giống hệt là duplicate; key có nhiều content → **mọi member conflict**, không giữ first winner accepted. Record parse-invalid nhưng còn key hợp lệ cũng tham gia phát hiện conflict. Conflict nằm trong `quarantine/` với disposition riêng; duplicate có file riêng và `duplicate_of_line`.

Mỗi file/run có phương trình:

```text
read = accepted + quarantine + duplicate + conflict
```

Missing lexical tokens được đếm thêm theo measure, không cộng lần nữa vào phương trình disposition. Expected source rows được đối chiếu với read của file được parse; lệch → file failed. Deferred có read=0 và expected rows riêng (`deferred_rows` ở cấp run), không cộng vào accepted.

- `loaded`: parse xong, không quarantine/conflict; duplicate vẫn có audit; `complete_for_ingestion=true` chỉ là trạng thái ingestion, không phải BR/measure completeness.
- `loaded_with_quarantine`: có record bị cách ly/conflict; `complete_for_ingestion=false`.
- `failed`: header/adapter/CSV fatal, count mismatch hoặc source thay đổi; output không được tiêu thụ như file đã nạp đầy đủ.
- `deferred`: chưa parse, `complete_for_ingestion=false`.

Run `completed_with_issues` khi có quarantine/conflict nhưng không file failed (Reference quarantine là trường hợp dự kiến); CLI exit 0 và giữ counts/lý do. File failed → run failed, exit 1. Deferred được liệt kê tường minh ngay cả khi run completed; completed không có nghĩa mọi nguồn/BR đã hoàn tất. Manifest bắt đầu `running`, cập nhật từng file, kết thúc status/UTC clock riêng; crash có thể để run running/partial. Không đọc output run chưa kết thúc hoặc failed. Publication/recovery, idempotency xuyên run và cleanup an toàn thuộc T08; không có public pointer được đổi ở đây.

## Kiểm chứng

```bash
docker compose -p smart-greenhouse --profile tools run --rm snapshot python -m greenhouse verify-fixtures
docker compose -p smart-greenhouse --profile test run --rm test-runner
```

Tests dùng sáu error fixtures đã catalog và dữ liệu synthetic tạo tạm với CRLF/BOM cho adapter, header/width, physical multiline lineage, serial drift/boundary, NULL/error, duplicate/conflict và counts. Không thêm CSV thật hoặc thay oracle/case catalog. Smoke AGC thật chỉ báo aggregate trong REVIEW_REQUEST; output theo run nằm trong volume, không commit vào repo. Không gọi kết quả parse là nghiệm thu sáu BR.
