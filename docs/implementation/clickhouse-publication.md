# ClickHouse publication và phục hồi writer

Theo [ADR-003](../adr/003-clickhouse-warehouse.md), đường supported writer dùng
cùng thư mục coordinator trên filesystem local và POSIX flock. API ở
`greenhouse.clickhouse.publication.Publications`; chưa là nghiệm thu mart/UI sáu BR.

## Pending write fence

Trước mỗi write, lưu query ID duy nhất và SQL hash vào `pending-write.json`,
fsync file rồi atomic rename và fsync thư mục. Gửi synchronous HTTP request;
chỉ khi nhận phản hồi thành công mới xóa marker và fsync thư mục. Exception,
process kill hoặc mất response để lại marker; process mới lấy lock phải reconcile
**trước khi đọc trạng thái để quyết định ghi tiếp**.

Reconcile flush query log, kiểm query ID còn active và terminal event trên server.
Active → giữ fence/chờ; terminal → được tiếp tục đối soát semantic records;
không có terminal evidence → giữ fence, không kết luận request chưa tới server.
Mỗi request có wire query ID mới kể cả khi logical operation ID lặp; log của
retry cũ không được dùng làm bằng chứng terminal cho request mới.

Khi terminal là lỗi, dữ liệu có thể đã ghi một phần qua các block: không xem
reconcile thành công là publish thành công. Manifest/canonical reader kiểm lại
content/count/checksum trước khi hoàn tất workflow. Identical retries collapse
tại reader, conflicting payload không chọn first/last.

Nếu không có terminal evidence, giữ marker và diagnostic logs; kiểm đúng query
trong system.processes/query_log dưới quyền bảo trì. Không xóa marker hoặc retry
mù. Nếu cần restart để tạo một mốc quiescent phải có kế hoạch dừng writer/server,
đối soát dữ liệu và ghi bằng chứng riêng; API hiện không tự thực hiện bước này.
Coordinator journal phải backup cùng DB và sensor state. Không dùng TTL lease
để ép writer khác đi tiếp khi server vẫn đang ghi.

## State machine

PREPARING giữ load ID, publication ID, generation, config pins và danh sách bundle
bắt buộc; WRITING là giai đoạn build/rebuild các candidate typed marts; VERIFIED
lưu receipt từng bundle; COMPLETE thêm committed_at UTC vận hành. Mỗi state là
một immutable envelope có content hash. Reader từ chối state thiếu tiền nhiệm,
identity thay đổi và cùng workflow/stage có payload mâu thuẫn.

Một workflow retry phải giữ load và bundle registry. Builder chạy lại trước
complete phải idempotent theo publication/semantic key; bundle validator phải
kiểm dữ liệu thực, trả version/semantic count/checksum. Publisher kiểm canonical
membership/config pins và từng bundle trước VERIFIED và COMPLETE. Retry COMPLETE
chỉ kiểm dữ liệu, không gọi builder để sửa publication đã công bố.

Publication core-only có receipt core/config nhưng không có receipt các BR;
adapter của từng BR phải từ chối nếu bundle bắt buộc chưa có. API không tuyên bố
một fixture bundle đã thay thế mart thật. T43–T46 bổ sung typed builders/validators.

Reader chọn complete generation cao nhất trong scope, pin ID rồi dùng cho mọi
truy vấn/export. `pin(scope, publication_id=...)` truy bản cũ; config version mới
không làm thay đổi pins của publication cũ. Mỗi pin kiểm lại dữ liệu/receipt;
cache tối ưu sau phải giữ cùng bảo đảm. Đây không phải transaction đa bảng của
ClickHouse và không cần experimental transaction.

## Kiểm chứng

Suite container có crash injection sau mỗi state, retry cùng workflow, giữ
publication cũ khi candidate mới chưa complete, validator từ chối bundle chưa
đủ, config mới không thay pins cũ, mất response sau server completion, marker
không có terminal log, process kill giữa một INSERT đang chạy trên server và
hai publisher process tranh cùng workflow/lock. Full AGC, mart thật và streaming
checkpoint parity còn có gate ở các task tiếp theo.
