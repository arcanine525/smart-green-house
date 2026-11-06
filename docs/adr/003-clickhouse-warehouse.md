# ADR-003 — ClickHouse local và hợp đồng chuyển warehouse

Ngày: 2026-11-07. Trạng thái: quyết định thiết kế; triển khai/nghiệm thu theo T40–T52.
Baseline PostgreSQL: `da1d13d44922fad1b83b4a8d1a132c57af15610c`.

## Phạm vi

Chuyển warehouse Hybrid, sáu BR, metadata/config/scenario và sink sensor sang
ClickHouse single-node local. Giữ raw, MQTT, SQLite outbox/spool, transforms
nghiệp vụ và DTO. PostgreSQL tiếp tục là đối chứng/rollback, không là dependency
của đường chạy ClickHouse. Chưa dùng replication, distributed tables hay cloud.
Mỗi engine có project, volume và checkpoint generation riêng.

ClickHouse tối ưu đọc phân tích nhưng không thay nguyên transaction/upsert của
PostgreSQL. Không dùng transaction nhiều câu lệnh experimental. Không dùng
background merge, cửa sổ dedup retry hữu hạn hoặc watermark làm chứng minh
exactly-once, completeness hay tính đúng của correction.

## Tách adapter và thành phần giữ nguyên

Giữ services thuần trong `greenhouse.queries`, DTO/codec trong `contracts`,
parser trong `ingestion` và logic rule/quality dùng chung. Tạo package
`greenhouse.clickhouse` cho client, DDL, loader, publication, adapters và sink.
Factory ở app/CLI chọn backend tường minh; lỗi backend phải được trả về, không
fallback PostgreSQL. Xem [bản đồ triển khai](../implementation/clickhouse-migration-contract.md).

## Writer và command persistence

Single-node này sử dụng **POSIX advisory file lock trên volume coordinator chung**
cho mọi supported writer (app config/scenario, pipeline và stream). Một khóa
exclusive toàn warehouse trước khi đọc trạng thái và ghi, không dùng TTL lease.
Khóa tự nhả khi process chết; descriptor phải giữ suốt critical section. Volume
được mount cùng identity/path vào mọi container writer. File lock chỉ được dùng
trên filesystem local hỗ trợ flock, không trên NFS hoặc nhiều Docker hosts.
Không xóa/thay inode lock trong khi còn process. Khi chạy thiếu mount/không khóa
được phải fail closed. Raw producer/collector vẫn hoạt động độc lập và backpressure.

Mọi INSERT synchronous phải chờ phản hồi (`async_insert=0`), có query ID và
content manifest. Nếu mất ACK, writer mới sau khi lấy lock phải xác định truy vấn
cũ đã terminal bằng system.processes/query ID hoặc chờ/kill và chờ kết thúc;
không khởi động attempt kế tiếp khi server còn ghi attempt cũ. INSERT không rõ
kết quả được đối soát theo semantic key + payload hash, không dựa vào ACK.
DDL/admin ngoài coordinator thuộc bảo trì khi writer dừng; runtime read-only user
không có quyền INSERT. Writer DB credential chỉ cấp cho đường command có lock;
đây không là cơ chế chống một DBA cố ý bypass protocol.

Config/rule/scenario lưu **một envelope bất biến đầy đủ header + segments/rates**
trong một hàng logic, có schema version, hash, request ID và domain version.
Cùng request ID/payload trả cùng kết quả; khác payload bị từ chối. Writer kiểm
hash hiện có trước append. Retry vật lý có thể tạo bản sao giống hệt: reader
collapse theo identity/hash; nếu một identity có nhiều payload khác nhau, fail.
Hai writer hợp lệ được serialization bởi lock, không tự chọn last-write-wins.
Envelope metadata và cấu hình được export/journal cùng raw để rollback.

## Dữ liệu và publication

Nguồn append-only giữ source ID, semantic key, revision ordinal, payload hash,
correction và scope. Canonical revision xác định trước mọi aggregate; revision
cùng ordinal khác hash là conflict và không được ngầm tie-break. Published
membership gắn với revision cụ thể, không đọc latest revision của publication cũ.
Sorting/partition key phải giữ mọi revision của cùng logical key cùng miền dedup.

Publication có attempt riêng: PREPARING → WRITING → VERIFIED → COMPLETE.
Không có chuyển trạng thái ngược. Nạp core/marts có publication/attempt ID; xác
minh manifest từng bundle (semantic count, hash, config hash, schema version)
rồi ghi một complete envelope chứa toàn bộ receipt và pointer generation.
Mất ACK của complete được đọc lại bằng hash. Reader chọn complete generation
cao nhất hợp lệ trong scope, pin ID một lần; mọi truy vấn/CSV dùng ID đó.
Không cập nhật payload publication đã complete. Retry trước complete kiểm và
hoàn thành attempt cũ hoặc tạo attempt mới; orphan chưa complete không hiển thị.
Complete envelope chỉ là commit record sau xác minh, không thay kiểm bundle.
Config mới tạo version mới; query-time rule/scenario pins thuộc context/cache.

Marts ban đầu được tính lại từ canonical membership theo publication/cycle,
không cộng incremental view trực tiếp từ nguồn còn retry/correction. Tối ưu
incremental chỉ thêm sau khi có parity và fault evidence. Chiến lược này giữ
correction, late event và episode qua biên micro-batch đúng với baseline.

## Kiểu dữ liệu và kiểm chứng

Giữ Decimal chính xác ở Python/DTO. Persist biểu diễn thập phân canonical
String để round-trip **mọi precision baseline**; cột số phục vụ SQL dùng
Decimal256 khi giá trị biểu diễn chính xác được, kiểm round-trip trước ghi.
Không truncate/round hoặc chuyển Float64 để vượt giới hạn scale; giá trị ngoài
miền projection phải tính bằng semantic transform chính xác, có trạng thái rõ.
Các số lượng tiền/năng lượng công bố vẫn dùng oracle Decimal hiện hành.
Ngày nguồn Date/Date32; timestamp nguồn là wall clock không xác minh timezone:
lưu serial/string gốc và integer microsecond source-clock riêng, không tự gắn UTC.
Timestamp vận hành có UTC thật, phân biệt rõ trong schema và DTO.

Xem tolerance/gates ở contract. Bản thiết kế không tuyên bố ClickHouse đã chạy
hoặc nhanh hơn. Benchmark engine giữ Hybrid/workload/resources giống nhau;
không gộp với thí nghiệm Hybrid/Kimball PostgreSQL.

## Recovery và chuyển backend

Backup tại mốc dừng writer, gồm DB/config/envelopes, coordinator journal,
raw/ZIP, outputs, broker, SQLite WAL/SHM, ready và checkpoint cùng mốc.
Checkpoint ClickHouse là generation mới; không dùng checkpoint PG đã ack để
bỏ qua những event chưa ghi vào ClickHouse. Cutover dừng ghi, drain và khóa
cutoff; rollback phải mang theo config/scenario/correction/events phát sinh mới.
Đối soát full source và các fault, offline/restore, walkthrough trước cutover.

## Tài liệu chính thức đã đối chiếu

- [INSERT transaction guarantees và experimental multi-statement transactions](https://clickhouse.com/docs/concepts/features/operations/insert/transactions).
- [ReplacingMergeTree: merge nền không bảo đảm loại trùng ngay; FINAL tại query](https://clickhouse.com/docs/reference/engines/table-engines/mergetree-family/replacingmergetree).
- [Docker self-managed, version tags và architectures](https://clickhouse.com/docs/get-started/setup/self-managed/docker).

Phiên bản/digest thực chạy sẽ khóa và ghi bằng chứng tại T40, mọi giả định runtime
ở trên phải có fault test trước nghiệm thu T42/T47/T51.
