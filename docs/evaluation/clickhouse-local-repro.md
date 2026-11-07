# ClickHouse: chạy offline, physical restore và dựng lại từ raw

**Đạt trong phạm vi đã kiểm.** Full AGC pipeline offline, physical restore,
raw rebuild, 302 config version/175 scenario và replay selection 30 event đều
đạt, gồm UI sau phục hồi. Giữ riêng lần lỗi storage và kết quả retry; không
coi walkthrough/video hoặc chuyển backend mặc định đã hoàn tất.

## Phạm vi và nguồn

Clone local từ `develop` tại `ae61a7d4ed1810afe550e9a15e7f40f5bcf213ee`, dùng
`.env.example` và ba image build bằng Dockerfile/lock canonical. Không sao chép
secret cá nhân, không kết nối PostgreSQL trong runtime ClickHouse. Metadata và
bốn bộ kết quả PostgreSQL là export read-only đã khóa, publication
`892b4cd58cdf4d099510974d91fa64ab`. ZIP SHA256:
`c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68`.

Runtime dùng private database/user `greenhouse_repro`, project riêng có tiền tố
`gh-ch-repro-20261107032510`. ClickHouse giới hạn 1 CPU/2 GiB; processing và UI
1 CPU/1,5 GiB. Image/dependency được chuẩn bị trước khi cô lập runtime. Đây là
thử nghiệm vận hành/correctness; không phải phép đo p95 thay cho
[benchmark riêng](clickhouse-performance.md).

Lần kiểm clone phát hiện lỗi UI chuyển bộ lọc: nếu publication cũ không có ở
màn hình mới và lựa chọn mặc định hỏng, pending transfer ghi đè mọi lựa chọn
khôi phục của người dùng. Regression thật tái hiện chọn `zzz-valid` nhưng UI
vẫn giữ `aaa-corrupt`. Sửa `shared_context.py` để chỉ áp dụng transfer một lần,
đồng thời giữ việc từ chối publication hỏng. Image UI được build lại từ đúng
hai file thay đổi trên clone (module này và UI test); native pipeline/client
giữ code gốc. Không tuyên bố toàn bộ UI cuối là image clone chưa sửa.

Suite cuối có 85 test: 76 chạy đạt, chín heavy integration skip tường minh;
68,157 giây. Năm unit test chuyển bộ lọc đạt. Heavy MQTT/Spark/physical restart
ở diễn tập bên dưới là bằng chứng riêng, không đổi skip thành pass. Hai test
UI ban đầu bị ảnh hưởng discovery giữa các fault fixture; UI test cuối dùng
database riêng và thêm tình huống lỗi xác định ở trên.

## Full historical pipeline và user state

Publication mới `52b72d65384944798dd7981c0a46615b`, load
`e535d82357da4453a7e8ea26de971553`: 287.989 canonical revision, 14.062.934 typed
measure; giữ một quarantine và phần deferred. Phạm vi raw/staging/core giống
[đối soát full AGC](clickhouse-parity.md), không gọi toàn bộ file ZIP là fact.

Maintenance không deduplicate mất 24,969 giây, giữ số hàng và kiểm ba physical
fingerprint ổn định. Full parity đạt sáu BR, 96 CSV, 30 lineage và 18 nhóm
threshold/67.829 episode, gồm biên vụ. Scenario nguồn 174 version được nhập qua
parity gate thật. Sau đó API native tạo thêm một drain profile `user_unverified`
và một scenario tăng giá điện 20%; retry đọc lại đúng DTO, không tăng version.
Tổng 175 scenario version, 302 config version trước backup.

Native export ghi backend **ClickHouse**, checksum
`ad4c5cb93978313a4f3b690676709156fb85e91d19be6d23c83ac4c842b88836`.
Original envelopes/DTO giữ trong archive local; không giả export PostgreSQL để
đi qua migration gate. Baseline hai ngày Automatoes là `17.60006771 EUR`,
scenario là `20.840734377 EUR`. Tám màn hình production đạt raw CSV oracle và
thao tác rule/episode/scenario; tổng 284,091 giây gồm cold verification.

## Offline và truy cập UI

Cả backend/frontend network đặt `internal=true`. Job truy cập được native DB,
không mở TCP ra hai địa chỉ IP ngoài, không có PG environment; raw/import mount
read-only. Đây là kiểm chứng **runtime đã chuẩn bị**, không tuyên bố tải image/
dependency lần đầu khi mất mạng.

Docker Desktop không tạo published port thực khi relay chỉ nối hai network
internal: cấu hình binding vẫn có, nhưng `NetworkSettings.Ports` rỗng. Để kiểm
UI local mà giữ cô lập, harness dùng tunnel host chỉ bind `127.0.0.1`, qua Docker
exec stdio tới cố định app `127.0.0.1:8501`. HTTP health/HTML thật đạt; không
attach thêm bridge hoặc mở egress cho container. AppTest tám màn hình là kiểm
UI tự động; walkthrough browser/video tương tác thuộc task riêng.

## Physical backup/restore

Trước snapshot, writer MQTT/Spark dừng. Inventory hash từng hàng vật lý (kể cả
exact retry copy), sort SHA256 có external-sort 64 MiB, fold SHA256 và hash DDL
cho từng bảng. Sau đó dừng cả app, DB và broker trước khi tar.

Backup gồm ba volume `ch_data`, `ch_logs`, `ch_broker` và bind archive: raw/ZIP/
manifest, references, output, coordinator, config exports, SQLite DB/WAL/SHM,
outbox/spool/ready, Spark checkpoint/stream identity, config server/broker.
Mỗi archive có byte count/SHA256, không overwrite snapshot; restore từ chối
target không rỗng, kiểm project label, giữ UID/GID/mode và symlink an toàn.
Probe trước khi dùng full volume xác nhận owner 10001, mode 0640 và symlink.

| Archive | Bytes | SHA256 |
|---|---:|---|
| ClickHouse data | 376800379 | `ac0c246e8e6ee5efd043566af829ac65f11752d92595e3a16b00622142b121b6` |
| ClickHouse logs | 11863388 | `1924812ec0b26e8ce97457a89d6f42f17ec789cd7af02057bcfe259e0cf7573f` |
| Broker | 11586 | `df99d36cb36017960b3f0de6e3600662f55ba250b9aff7d0e4c39f3917602ac9` |
| Bind state/inputs | 135484951 | `01c208aa0a514922c15f26f552842204fd55cd0321b4e8615582bd9a9eb26595` |

Project `*-restored` tạo volume trống riêng. Bind restore kiểm 226 file
byte-for-byte trước mở SQLite. Khởi động DB **một mình**, so toàn bộ 28 bảng/
17.513.148 hàng và DDL: inventory SHA256
`7953c9ae504f8c6c9ce838aa1c6904d1df615f35090732595463e593dc6cd177`
khớp trước khi broker/collector/publisher/Spark resume.

Replay chọn 30 dòng thật từ file Automatoes, SHA256
`3415b31b7958c735f836c7fa8f890a93020786be5c86dc2e7c42771fb0a02cd3`.
Seed batch 0 chứa 10 telemetry; 28 message telemetry/control chờ trong broker.
Sau restore, base SQLite bỏ WAL có zero row, WAL/SHM tồn tại và integrity đạt.
Collector nhận đủ 28 message queue **trước khi tạo publisher**. Publisher retry
và checkpoint cũ đi batch 0 → 1, batch mới 56 input row. Có 66 delivery audit
do at-least-once; kết quả có đúng 30 event duy nhất, envelope nguồn nguyên vẹn,
complete/checksum, không mất sequence. Không đánh đồng delivery trùng với event
canonical trùng. Business fingerprint:
`7783fdfa2f63909171709dea99b43bb5e6956af13fde3dca15ce4d1030d1f675`.

Đây là replay **selection 30 event**, không phải chạy hết climate CSV qua MQTT.
Toàn vụ được chứng minh riêng bởi historical pipeline và full parity.

Sau phục hồi Docker và chạy lại tuần tự, tám màn hình lịch sử, custom drain
profile `user_unverified`, custom cost scenario và replay UI đều đạt. Replay có
bốn chart và CSV; kiểm đúng 30 accepted event và complete. Toàn bộ 302 config
envelope/175 scenario DTO đọc lại chính xác. Audit sau resume (66 delivery, hai
batch, 30 member, hai seal/pointer) giữ trong native archive local riêng,
checksum `f25d812f6f6db93d931f1ca3281173c82b76e35dd3edefa2724c4bee045c1c5b`.
Archive này là delta sau resume, không gán nhầm thành snapshot vật lý trước đó.

## Raw rebuild và quy tắc mapping

Lần thử dùng DB/volume/coordinator mới; chỉ dùng raw, reference, native export
và ready envelopes từ bản restore đã kiểm. Không chép SQLite/checkpoint cũ vào
warehouse mới. 126 config version không phụ thuộc physical source được phục
hồi trước full pipeline. Job dừng trong bước kiểm canonical source vì Docker
filesystem chuyển read-only, trước khi có publication hoàn tất. Không coi đây
là raw rebuild thành công. Scenario/replay config vẫn chờ publication mới và
parity proof. Volume thử dở được xóa riêng, inputs/log lỗi giữ lại.

Retry bắt đầu với 6.419 GiB host trống và guard 6 GiB; coordinator/output mới,
state lỗi trước giữ riêng. Không chạy UI/cold proof song song. Pipeline hoàn tất:
publication `4b08bd51c092465783304f887d766d47`, load
`b46b4d1fa95641b891c447faed0c07dc`; số canonical/quarantine/deferred khớp nguồn.
Maintenance 25,003 giây giữ physical row count và xác nhận ổn định. Full external
parity sau rebuild đạt sáu BR/96 CSV/30 lineage/18 nhóm/67.829 episode.

Sau parity, source/target native business projection cũng khớp, rồi nhập đúng
175 scenario version và exact readback. Toàn bộ 302 config version: 126 envelope
không phụ thuộc physical source giữ nguyên, 175 scenario và một replay config
được map explicit; ba envelope archive/workflow/map mới là provenance bổ sung.

Ready files được xử lý bằng generation `raw-rebuild-v1`, checkpoint mới, một
batch 66 delivery audit. Có đúng 30 event nguồn nguyên vẹn, không thiếu sequence,
complete/checksum và business fingerprint giống physical restore. Historical
publication và replay publication là hai định danh riêng; replay mới:
`33415da46b0acfd5dc87d1ed9b2eb8024eed032c81d3d62e3529197d8417171f`.

Tám màn hình, custom profile/scenario, bốn chart replay và CSV đạt sau rebuild.
Tổng riêng tám màn hình physical restore 32,669 giây, raw rebuild 67,300 giây;
đều chạy sau cold config/publication verification trong cùng process, không
phải cold startup hay p95 mỗi action. Benchmark hiệu năng vẫn là báo cáo T49.

Gate import phải kiểm full six-BR parity và business projection native source/
target, baseline/rates/chain/request, sau đó exact DTO readback. Chỉ map full
publication reference, analysis-context publication ID và replay source
publication/config hash; giữ scenario ID/version/request, author/time, rates,
rule/basis/tariff. Original archive và mapping provenance giữ riêng.

Replay trên empty DB phải đọc lại toàn bộ ready files bằng checkpoint và
generation **mới**. Checkpoint cũ chỉ hợp lệ cùng physical restore đã chứa các
batch commit. Không yêu cầu raw rebuild trùng publication ID hay physical hash.

## Lệnh chạy lại và giới hạn

Xem [hướng dẫn harness](../../scripts/clickhouse_repro/README.md).
`prepare.py` đóng gói lệnh chuẩn bị clone/image mới; lượt này thực hiện các bước
tương đương rồi sửa hai file UI đã nêu. Không coi syntax check của wrapper là
một lần dựng lại bổ sung. Raw/native export đầy đủ và tar ở `tmp/` local, không
commit dataset, config dump, scenario dump hay video.

Harness raw rebuild này từ chối correction/source revision >1 và config kind
chưa hỗ trợ; không âm thầm bỏ state. Bằng chứng hiện tại áp dụng snapshot AGC
chưa correction. Physical backup giữ cả bytes; chuyển delta/correction được
kiểm riêng trong cutover. Cold proof có thể tốn vài phút với CPU giới hạn;
không nới correctness guard để che chi phí này.

Đã dọn Docker build cache khi headroom thấp; không prune volume người dùng.
Fixture nguồn riêng của lượt này chỉ được xóa sau kiểm lại hash cả bốn archive
và all-table restored inventory khớp. Giữ backup/state và các ứng viên cũ;
PostgreSQL gốc cùng các service bảo vệ không bị sửa.

Lần chạy raw rebuild song song cold UI proof đã vượt headroom còn lại, dù cache
được dọn (1,279 GB rồi 572,7 MB). Docker chuyển read-only; restart Docker Desktop
và đưa service gốc về healthy. Đây là storage failure, không suy ra OOM. Giữ
nguyên hai log lỗi; native publication chưa seal nên không đưa candidate dở vào
KPI. Cache input JSONL của benchmark cũ được nén và kiểm SHA decompress; bản
failed pilot byte-identical cùng bản gzip thành công đã giữ, không mất input.

Harness được sửa để khóa tuần tự giữa mọi target, phát hiện helper container
còn chạy sau runner chết và kiểm host headroom trước từng stage: tối thiểu
3 GiB cho reads/verification, 6 GiB cho full backfill/raw rebuild. Chuẩn bị mới
cần 10 GiB cho image, copies và runtime headroom. Chưa chạy lại raw rebuild khi
headroom chưa đủ; không nới correctness hay đánh dấu pass từ output dở.


Khôi phục đủ 6,4 GiB bằng cách dọn 66 image greenhouse cũ không tag/không được
container tham chiếu, cache output orphan của rehearsal cũ sau kiểm lại đủ mười
backup, và bản sao source/restore thuộc lượt thử này sau khi các gate đã đạt.
Đối chiếu lại file hash của raw/ZIP/output trước dọn; giữ tar, native export,
post-resume audit, outbox/ready/checkpoint và bản raw/ZIP độc lập cho rebuild.
Không chạy privileged host reclaim; dữ liệu gốc và video giữ nguyên.


## Hồ sơ nghiệm thu

[Manifest/checksum](assets/clickhouse-local-repro/manifest.json) ràng buộc 50
receipt/log và 29 file code/hướng dẫn. Finalizer từ chối xuất manifest nếu thiếu
bất kỳ gate full parity, inventory, config/scenario hoặc UI/replay nào. Không
commit native metadata/scenario dump, raw, ready payload hoặc tar.

Ngoài suite 85 test và năm unit test UI transfer, năm nhóm archive rejection
đạt (backend/checksum/schema/payload/unknown kind/correction), cùng hai probe
host guard cho thiếu headroom và chạy helper song song. Raw rebuild hỗ trợ
snapshot chưa correction như giới hạn đã nêu; không suy rộng kết quả này thành
đã kiểm workflow delta/correction hay cutover.
