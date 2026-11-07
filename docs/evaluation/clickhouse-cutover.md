# ClickHouse mặc định và diễn tập rollback PostgreSQL

Ngày 2026-11-07, main thực hiện trên project/volume riêng. Kết quả cuối **đạt**
trong phạm vi đã kiểm: default native, durable delta journal, restore/rebuild
PostgreSQL, metadata/scenario/replay parity và trả runtime về ClickHouse.
[Summary](assets/clickhouse-cutover/summary.json) và
[manifest](assets/clickhouse-cutover/manifest.json) khóa evidence.

## Dữ liệu và thao tác mới

Cutoff native publication `4b08bd51c092465783304f887d766d47` dùng cùng scope và
AGC snapshot đã khóa với PG baseline `3e4a051009084e11ab19efdd2343c9d3`.
Snapshot PG cũ thiếu các scoped tariffs; nhập delta config và chỉ map legacy
tariff alias sau khi toàn bộ semantic fields/rates/intervals bằng nhau.

Sau khi bật journal, tạo profile EC/pH, tariff và scenario điện +7,5% có audit
time, correction **+1 Irr** ở Automatoes ngày 02/03/2020, cùng replay mới 10→30
event qua MQTT/collector/Spark thật. Retry không nhân bản thao tác. Correction
diễn tập không phải quan sát AGC gốc; raw và publication cũ giữ nguyên.

| Gate cuối | Kết quả |
|---|---|
| Native corrected publication | `5d455686a06649e789908b9708a9d436` |
| PG restored/rebuilt publication | `c51402d9d25f45c182140b072397de78` |
| Configuration envelopes | 315 version được giữ, kể cả recovery provenance |
| Scenario versions | 178; original namespace/request lookup và retry đạt |
| Source correction | Actor/evidence/parent và revision mới được nạp qua API |
| Replay | 7 immutable pins, 4 replay scopes, gồm partial cũ và current |
| Replay metadata | Identity, normalized clock, received clock, state/cursor/checksum đều khớp |
| Full parity mới | Sáu BR, 96 CSV, 30 lineage probes, 18 threshold groups |
| Threshold oracle | 67.829 episode khớp PostgreSQL và raw độc lập |
| Old publication | Business projection không đổi sau correction/rebuild |

Full parity được chạy lại trên **hai publication corrected mới**, không tái
sử dụng ID của proof cũ để tuyên bố rollback đạt. Native video T51 vẫn ghim
publication gốc của thời điểm ghi hình; đây là hai context có provenance riêng.

## Journal và backup

Journal SQLite FULL/WAL ghi intent trước SQL và terminal outcome trước ACK,
chung coordinator lock với pending-query fence. Mốc pause có **62 SQL intents**,
tất cả QueryFinish, head
`bbfd7017ae3b8c72ca4445544b2ee8012eb0ac48576f579408ba11320fce7a03`.
Pause từ chối command mới; SQLite backup readback khớp cả WAL state.

Backup, raw và SQL journal đầy đủ giữ local, chỉ metadata/checksum vào Git:

- Full native delta canonical hash: `c31c588cc87edf265e22c2f58c05f22568abbf4818d6803fe8202f5cfe0ce1bd`.
- Full archive retention: 215.454.925 byte canonical JSON, được nén reversibly
  trong PG; readback SHA/byte count đạt, không bỏ SQL để giảm kích thước.
- Accepted PG dump: 239.741.319 byte, SHA-256
  `be8839827fd1de38e5351606990c1af58277c706b9bf716ac9dbfda3c29b27f9`;
  `pg_restore` đã đọc toàn bộ archive, không chỉ TOC.

PostgreSQL gốc cùng app/broker gốc vẫn dừng và giữ volume. Chỉ database fixture
do main tạo được dựng lại trong quá trình sửa lỗi; forensic dump của lần lỗi
đã kiểm toàn bộ trước thao tác. Không prune Docker volumes.

## Hai lỗi được phát hiện trong gate

Một PG rehearsal cũ và native fork dùng cùng request/family ID
`offline-restore-electricity-plus20` nhưng ghim tariff khác nhau. Giữ bản PG
bất biến và namespace một native family bằng cutoff hash. Migration mapping
giữ original request/version IDs, payload hash và target IDs; rates/tiền/context/
audit giữ nguyên. Tất cả 178 version đọc lại và retry đạt; bản PG cũ không đổi.

Importer ban đầu chép raw cursor vào cột `observed_through`, gây caption lệch
0,384 giây dù JSON state/envelopes và KPI khớp. Main phát hiện qua ảnh browser;
sửa bằng shared `normalize_cursor`, mở rộng gate sang **toàn bộ metadata tuple**
và dựng lại fixture từ backup. Bản cuối kiểm cả clock 02:25:00, đồng thời giữ
raw timestamp 02:24:59.616 trong original envelope/state. Không tắt immutable
trigger hoặc sửa pin cũ để vượt kiểm tra.

Browser harness còn được sửa để đợi export control render và chấp nhận hai
cách format datetime hợp lệ `T`/dấu cách; không bỏ assertion dữ liệu.

## Runtime, smoke và thời gian

Compose mặc định chỉ cần ClickHouse, app và localhost relay. App cuối có backend
`clickhouse`, không có PG environment keys, required journal bật. Database volume
native đã nghiệm thu được giữ; source và image IDs nằm trong summary/stage maps.

- Native smoke khi các PG của project dừng: **9 màn hình**, 432,350 giây cho
  cả lượt, gồm cold proof và đọc saved contexts.
- PG rollback UI read-only sau mọi gate dữ liệu: **9 màn hình**, 8,339 giây;
  các đồng hồ và replay state đều đúng. Không dùng số này làm benchmark engine.
- Chuyển cuối PG→CH: HTTP healthy sau **9,250 giây**; KPI thực sẵn sàng sau
  **198,954 giây**. Native default CLI setup cũng đạt khi PG dừng.

Đây là rehearsal có dừng ghi, backup, verify và sửa lỗi. `phase-times.jsonl`
giữ các mốc; initial preparation stop chưa được timestamp đầy đủ. Không gọi
lượt này zero-downtime hoặc dùng return HTTP time làm tổng outage của cả đợt.
Cold proof nhiều phút là giới hạn thực tế, khác warm query p95 ở benchmark.

Native suite: **100 total / 91 chạy đạt / 9 heavy gate skip**. Routing suite:
**8 đạt**, gồm default CH và explicit PG. PG cost regression: **24 đạt**, có
case bảo toàn created_at/author và từ chối retry audit khác. Không cộng các
suite chồng lặp thành một số test độc lập.

Docker build cache đã dọn **108,6 MB** khi storage xuống dưới ngưỡng rebuild;
volume/raw/backup/coordinator được giữ. Các spools tạm cũng được giải phóng khi
tool/app kết thúc; thay đổi free space không được quy hết cho build cache.

## Phạm vi và chạy lại

Single-host/local flock, không phải distributed lock. Diễn tập dùng locked AGC,
resource correction, profiles/tariff/scenario và finite sensor replay. Snapshot,
basis hoặc config family mới cần translator/parity trước khi mở PG; archive
giữ dữ liệu không tự chứng minh query semantics. Legacy native timestamp vốn
NULL giữ nguyên trạng thái unknown; PG import time được phân biệt rõ.

[Runbook](../implementation/clickhouse-cutover-rollback.md) và
[harness](../../scripts/clickhouse_cutover/) ghi identity guard, stop/drain,
backup, source/target mappings và failure recovery. Images chuẩn bị trước;
không cần cloud. Evidence log normalize CR/trailing whitespace khi đưa vào Git,
giữ cả source-log SHA và archived-log SHA; executed helpers lưu theo content hash.
