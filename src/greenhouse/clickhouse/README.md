# ClickHouse candidate backend

[Thiết kế](../../../docs/adr/003-clickhouse-warehouse.md),
[hợp đồng](../../../docs/implementation/clickhouse-migration-contract.md),
[môi trường Docker](../../../docs/implementation/clickhouse-local-environment.md).

T40 đã kiểm client HTTP synchronous CPython 3.12.12 và ClickHouse 25.8.33.6.
T41 cung cấp schema/repository/ingestion candidate; sáu BR và publication hoàn tất thuộc các task tiếp theo.

- `schema.py`: typed revision/measure, disposition, config envelope và load receipt.
  MergeTree giữ append-only evidence. Sorting theo scope/type/compartment/logical
  key/revision; chưa partition nhỏ theo event/publication. Quy mô AGC dùng một
  partition mặc định trước benchmark. Không dựa vào merge để loại trùng.
- `encoding.py`: Decimal exact canonical strings; Decimal256(76,38) chỉ là
  projection khi biểu diễn chính xác, còn lại NULL kèm trạng thái; source clock
  microsecond không tự gắn timezone. Payload không chấp nhận binary float.
- `repository.py`: retry/revision conflict và config envelope immutable dưới
  local lock. Canonical reader kiểm source metadata/hash và đầy đủ measures, trả revision mới nhất hoặc membership đã pin. Publication hoàn tất tiếp tục T42.
- `load.py`: checksum source, dùng lại ingestion/parser; giữ accepted/duplicate/
  quarantine/conflict và source provenance. Load receipt có checksum/count và bảng membership theo load ID; vẫn là candidate, không
  phải publication. Vòng nạp hiện ưu tiên correctness, chưa tối ưu batch.
- `coordinator.py`: POSIX file lock local; xử lý query chưa rõ kết quả và fault
  recovery phải hoàn thiện trước đường pipeline ứng dụng.

Bộ kiểm `tests/clickhouse/` có unit tests và real server tests; real tests chỉ
chạy với `GREENHOUSE_CH_INTEGRATION=1` trên project test mới do harness tạo.
Không chạy destructive cleanup trên volume của người dùng.

Correction chỉ sửa values/null reasons, giữ source identity/time/lineage; phải có
actor, evidence và previous revision, từ chối stale correction và ID dùng lại.
Người gọi muốn aggregate phải dùng `canonical_records` hoặc `load_membership`,
không SUM trực tiếp bảng append-only. Identical physical retries collapse bằng
SELECT DISTINCT, bất kỳ payload/metadata/measure mâu thuẫn nào đều fail closed.
Config envelope có checksum/schema version và request/version identity, chưa
thay validator nghiệp vụ của từng loại rule/rate ở T43–T46.

Ví dụ sử dụng loader trong container client với mount của caller:

```bash
docker compose -f compose.clickhouse.yaml -p greenhouse-ch-dev run --rm --no-deps \
  -v "$PWD/data/raw:/data/raw:ro" -v "$PWD/data/output/ch-candidate:/candidate" \
  ch-client python -m greenhouse.clickhouse.load \
  --raw /data/raw/agc-2nd-2019/SNAPSHOT_SHA --output /candidate/output \
  --coordinator /candidate/coordinator --scope SNAPSHOT_SCOPE
```

Chuẩn bị thư mục `/candidate/coordinator` trong mount, quyền ghi UID 10001 và
filesystem local hỗ trợ flock trước khi chạy; raw/output phải tách nhau. Thay
SNAPSHOT_SHA/SCOPE bằng manifest đã khóa; export config/scenario theo API repository.
Đây là nạp candidate, không làm dashboard đọc dữ liệu chưa publish. Unknown-write
reconciliation/concurrent pipeline và warehouse recovery hoàn chỉnh thuộc T42.
