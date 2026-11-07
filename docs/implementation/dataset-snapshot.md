# Snapshot AGC và fixture tổng hợp

Tài liệu snapshot/inventory ban đầu được giữ cùng evidence lịch sử. Commands dưới
đã cập nhật cho ClickHouse mặc định; chất lượng/basis và nghiệm thu ở ma trận yêu cầu.

## Nguồn đã khóa

- GET theo redirect, không HEAD: `https://www.kaggle.com/api/v1/datasets/download/piantic/autonomous-greenhouse-challengeagc-2nd-2019`.
- ZIP: `autonomous-greenhouse-challengeagc-2nd-2019.zip`, **12,793,984 byte**.
- SHA-256: `c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68`.
- Snapshot ID: `agc-2nd-2019-c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68`.
- Đường dẫn trong container: `/data/raw/agc-2nd-2019/c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68/`.
- Mặc định trên host: cùng phần đuôi dưới `data/raw/`; đổi root bằng `GREENHOUSE_RAW_PATH` trong `.env`.

Snapshot đã đo gồm **45 file = 43 CSV + 2 PDF**, **103,682,625 byte** giải nén và **622,899 dòng CSV** không tính header. Chi tiết từng file/header/cadence/time range nằm trong [inventory tái tạo từ snapshot](data-inventory.md). Không commit ZIP/CSV/PDF hoặc dòng dữ liệu thật; Git chỉ giữ code, fixture synthetic và số liệu kiểm kê tổng hợp.

## Clone mới và nhập ZIP local — không cần Internet ở runtime

```bash
test -e .env || cp .env.example .env
mkdir -p data/import data/raw
docker compose --profile tools build ch-job
docker compose --profile tools run --rm --no-deps --user 0:0 \
  -v "$PWD/data/raw:/data/raw" ch-job python -m greenhouse snapshot
```

Đặt ZIP trong thư mục `data/import/` trước lệnh cuối; hoặc cấu hình `GREENHOUSE_IMPORT_PATH` trong `.env` trỏ tới **thư mục** chứa một bản copy ZIP trên host. Archive mặc định trong container là `/data/import/autonomous-greenhouse-challengeagc-2nd-2019.zip`. Nếu tên file local khác:

```bash
docker compose --profile tools run --rm --no-deps --user 0:0 \
  -v "$PWD/data/raw:/data/raw" ch-job python -m greenhouse snapshot --archive /data/import/custom-name.zip
```

Invocation snapshot `ch-job --no-deps` không khởi động DB/broker, chỉ dùng backend `internal: true`. Import luôn read-only. Raw mount override trên chỉ dành cho snapshot writer; job/stream/app bình thường vẫn raw read-only.
Nếu đổi GREENHOUSE_RAW_PATH, thay host path trong override tương ứng. ZIP và thư mục giải nén thủ công trong import không bị sửa/xóa/di chuyển. Bản giải nén thủ công không được dùng như snapshot.

Chạy lại lệnh cùng archive trả `already_exists_verified`, không ghi lại file hoặc acquisition time. Mỗi lần vẫn kiểm ZIP size/hash, kiểm snapshot trên disk và manifest so với nội dung ZIP đã khóa. Sai khác file/hash/metadata/counters làm lệnh thất bại, không tự sửa hoặc thay snapshot.

## Nhánh tải thật

```bash
docker compose -p greenhouse-source-setup -f compose.postgres.yaml --profile tools \
  run --rm --no-deps dataset-download
```

Đây là setup-only service legacy trong Compose PG riêng, không khởi động PG và
không dùng project native. Service này dùng cùng processing image, chỉ mount raw read-write; không mount import hoặc DB credentials. Network `download` riêng có Internet cho setup-only GET. Job/test/stream vẫn chỉ dùng backend nội bộ. Không publish thêm cổng, không có token Kaggle.

Download ghi `.downloads/download-*.part` trong raw root, giới hạn số byte nhận; kiểm size/SHA-256 trước rename thành `.downloads/<sha256>.zip`. Cache đúng checksum được dùng lại; import local không tạo cache này nên nhánh tải đầu tiên sau nhập local vẫn thực hiện GET. Nếu cần kiểm chứng một GET mới, dùng một raw root scratch khác qua `GREENHOUSE_RAW_PATH` thay vì xóa bằng chứng cũ.

Checksum/size khác → exit 1 với expected/actual; giữ file `.part`, không chấp nhận snapshot mới. Network/ZIP/parse failure cũng exit khác 0; file tạm chưa công bố được giữ để điều tra. Không tự chấp nhận checksum mới khi Kaggle thay dataset. URL redirect có thể đổi; manifest chỉ lưu URL gốc, không lưu URL ký tạm hoặc credential.

## Công bố bất biến và manifest

Archive được kiểm checksum rồi kiểm toàn bộ entry trước extraction: chặn path traversal, absolute/drive/backslash paths, symlink/special files, duplicate paths, file-directory collision, encrypted entries; giới hạn 128 entries, 64 MiB/file và 256 MiB tổng expanded size. AGC còn phải khớp đúng 45 đường dẫn nguồn đã khóa.

Snapshot ghi vào `.pending-*` cùng filesystem với đích, fsync file, ghi manifest, chmod file `0444`/directory `0555`, rồi rename atomic dưới publisher lock Linux. File incomplete không bao giờ nằm ở tên snapshot công bố. `.pending-*` lỗi là bằng chứng chưa công bố; không dùng nó làm đầu vào pipeline. Tham chiếu [Python zipfile](https://docs.python.org/3.12/library/zipfile.html), [os rename/fsync](https://docs.python.org/3.12/library/os.html#os.rename), [urllib GET/redirect](https://docs.python.org/3.12/library/urllib.request.html).

Read-only permissions là bảo vệ vận hành, không phải storage WORM: owner/root vẫn có thể thay quyền. Vì vậy rerun xác minh disk/manifest với ZIP đã khóa và từ chối thay đổi, kể cả khi ai đó sửa payload rồi sửa hash trong manifest. Không coi chmod là chứng minh chống mọi tác nhân có quyền root.

Trên Docker Desktop/macOS arm64 đã kiểm chứng, container thấy file `0444`/directory `0555` nhưng host thấy `0644`/`0755` qua bind share. Không cam kết host owner bị chặn ghi bởi chmod trong container. Job/stream vẫn được bảo vệ bằng mount read-only; integrity được xác minh lại bằng hash khi dùng lệnh snapshot. Kiểm permissions runtime phải thực hiện trong container, không suy từ host stat.

`manifest.json` giữ:

- Version, snapshot/dataset ID; archive size/SHA-256; tổng file/CSV/PDF/expanded bytes/CSV rows.
- Nguồn import dạng `import/<filename>` hoặc URL download gốc, method và acquisition time UTC của **lần publish đầu**; không ghi đường dẫn host.
- Từng file: relative path, source folder, size, SHA-256, loại. ReadMe.pdf/Economics.pdf được giữ nguyên và truy nguồn như CSV.
- CSV: header raw và từng tên cột giữ whitespace/tab, số cột/dòng/row widths, các cột toàn rỗng; count token rỗng/NaN/NA/N/A theo index.
- Cột thời gian nguồn: min/max raw numeric, delta dương min/median/max, số bước lùi/lặp/không numeric. Hiển thị Excel serial qua epoch 1899-12-30 để đọc inventory, không gắn timezone và không sửa dữ liệu.

## Tái tạo inventory và kiểm fixture

Inventory đọc snapshot + manifest, kiểm file/hash/counters trước khi render. Output ổn định, bỏ acquisition clock và đường dẫn máy:

```bash
docker compose --profile tools run --rm --no-deps ch-job python -m greenhouse inventory > docs/implementation/data-inventory.md
docker compose --profile tools run --rm --no-deps ch-job python -m greenhouse verify-fixtures
docker compose -p greenhouse-pg-tests -f compose.postgres.yaml --profile test run --rm test-runner
```

Lệnh inventory có thể dùng `--output /data/output/data-inventory.md` để ghi named volume output, hoặc `--snapshot` chỉ rõ thư mục snapshot trong raw mount. Redirect ở ví dụ đầu là thao tác **chủ đích cập nhật tài liệu tracked**; các lệnh snapshot/test thông thường chỉ tạo dữ liệu trong vùng Git ignore.

Mỗi lệnh snapshot/download tự chạy verify-fixtures trước: một lệnh Compose chuẩn bị snapshot đúng checksum và xác minh fixture có sẵn, không sinh fixture ngẫu nhiên. [Catalog oracle](../../tests/fixtures/agc_synthetic/catalog.json) có **61 AC IDs**, **82 CSV synthetic**, trong đó **6 error files**; [README fixture](../../tests/fixtures/agc_synthetic/README.md) mô tả nhãn và định dạng.

| Fixture | AC được chuẩn bị | Ví dụ/oracle literal |
|---|---|---|
| `br_01/ac_01…ac_10/Synthetic` | BR-01/AC-01…10 | Vector Tair 19/21/26/26/NULL/24: out 15, valid 25 phút, hai đợt 5/10; boundary, fallback, gap, version, cửa sổ |
| `br_02/ac_01…ac_10/Synthetic` | BR-02/AC-01…10 | Irr 40, Drain 14 → 35%; 0/NULL/partial/120%, rule, duplicate/conflict và export filter |
| `br_03/ac_01…ac_10/Synthetic` | BR-03/AC-01…10 | EC out 15, pH out 5, OR 15/20 jointly valid; suspect pH, lab riêng, bối cảnh nước |
| `br_04/ac_01…ac_10/Synthetic` | BR-04/AC-01…10 | 500 kWh/100 kg A = 5 với diện tích **synthetic** 100/50 m²; thiếu basis/energy/harvest, ngoài vụ, tỷ số gộp |
| `br_05/ac_01…ac_10/Synthetic` | BR-05/AC-01…10 | A=3, B=0.2 → A share 93.75%; record synthetic ngoài cửa sổ, partial, calendar, lũy kế |
| `br_06/ac_01…ac_11/Synthetic` | BR-06/AC-01…11 | Baseline 1.83 → 2.03 EUR/m²; tariff overlap/gap/unit, zero, basis, version/rerun |
| `errors/…/Synthetic` | Handoff parser T06 | Invalid format, broken CSV, missing column, missing value, duplicate, conflict |

CSV giữ CRLF; LabAnalysis có một biến thể UTF-8 BOM được ghi trong catalog. `.gitattributes` đặt fixture CSV `-text` để checksum không đổi theo `core.autocrlf`; verifier đọc `utf-8-sig`.

CSV dùng header nguyên văn/Excel serial/thư mục khoang Synthetic. Nhãn synthetic nằm ở catalog và từng case, không thêm cột khác định dạng nguồn. Oracle là literal độc lập lấy từ AC, không gọi code KPI triển khai. Các tham số area/rule/tariff chỉ là giả định fixture đã nêu trong BR; [quyết định ngữ nghĩa AGC](data-decisions.md) tách bằng chứng nguồn, convention và phần chưa xác minh; basis fixture không thay bằng chứng diện tích thật. Case UI/version/export/recovery cung cấp input/control/expected cho task sau, không chứng minh những chức năng đó đã chạy.

## Bằng chứng và chuyển tiếp

Đã chạy local import và GET thật qua Compose trên Linux arm64; cả hai khớp archive 12,793,984 byte và SHA-256 khóa. Rerun trả already-exists sau kiểm tra, inventory được sinh hai lần byte-identical. Các test tự động chỉ dùng ZIP tổng hợp nhỏ, không cần Internet hoặc dataset thật. Bộ pytest hiện đạt **47 tests** (trong container, không cần Internet/dataset thật); chi tiết lệnh/kết quả được bàn giao trong REVIEW_REQUEST.

Inventory ghi nhận header rỗng dư IUACAAS Resources và header/row width khác biệt Reference TomQuality; đây là quan sát, không tự trim/bỏ cột. [Quyết định ngữ nghĩa](data-decisions.md) ghi basis/timezone/off-mode còn chưa xác minh, quarantine Reference và profile giá lịch sử; T06 phải parse có lineage, phát hiện lỗi/duplicate/conflict theo version. Chưa có accepted coverage, DB ingestion, KPI hoặc nghiệm thu BR.

`down -v` không xóa raw/import bind mounts; snapshot/cache/evidence vẫn nằm trên host. Không dùng `docker system prune` hoặc xóa ZIP/bản giải nén gốc để chạy lại. Dữ liệu thật luôn bị Git ignore và không vào build context.
