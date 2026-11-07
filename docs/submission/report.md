---
title: "Smart Greenhouse: hệ thống dữ liệu và phân tích nhà kính"
subtitle: "Báo cáo kỹ thuật môn Data Engineering — bản dự thảo"
date: "07/11/2026"
lang: vi-VN
---

# 1. Bối cảnh, người dùng và yêu cầu

Nhà kính tạo dữ liệu khí hậu, tưới, năng lượng và thu hoạch ở các độ hạt khác
nhau. Người vận hành cần nhận biết khoảng điều kiện bất thường; kỹ sư canh tác
cần xem EC/pH cùng bối cảnh tưới; quản lý cần đối chiếu sản lượng, năng lượng
và độ nhạy chi phí. Các bảng tính rời khó giữ cùng thời gian, đơn vị, phiên bản
và điều kiện chất lượng. Project xây dựng warehouse và ứng dụng phân tích
chạy hoàn toàn local, hỗ trợ các quyết định đó với nguồn và giới hạn rõ ràng.

Đây là **Project cuối kỳ**, đối chiếu `DE_Requirements.pdf`: nghiệp vụ/dữ liệu,
kỹ thuật dữ liệu và ứng dụng; tối thiểu 2×n BR, một phương án thay thế và đánh
giá correctness, performance, effectiveness. Sáu BR đáp ứng nhóm ba thành viên.
Video thuyết trình Project 20–30 phút do nhóm ghi offline sau khi chốt gói;
khác bộ walkthrough tự động từng BR đã có. Phân công dưới đây là gợi ý để nhóm
điền danh tính và xác nhận đóng góp thực tế, không phải ghi nhận đã đóng góp.

| Người trình bày | Phạm vi trình bày gợi ý | Hai BR |
|---|---|---|
| Thành viên A | Khí hậu, dinh dưỡng và quality/rule | BR-01, BR-03 |
| Thành viên B | Tưới, thu hoạch và lineage | BR-02, BR-05 |
| Thành viên C | Năng lượng, chi phí và scenario | BR-04, BR-06 |

## 1.1. Sáu yêu cầu nghiệp vụ

**BR-01 — Tìm các đợt lệch điều kiện khí hậu.** Người vận hành chọn khoang/vụ,
cửa sổ và cơ sở requested/realized để tìm thời điểm, thời lượng, đợt dài nhất
của nhiệt độ/CO₂ ngoài điều kiện. Đầu vào Climate có slot năm phút. Thời lượng
chỉ tính trên slot hợp lệ; gap, unknown hoặc đổi rule làm ngắt episode. Khi cắt
cửa sổ vẫn giữ mốc gốc của episode. Tổng OR của nhiều điều kiện dùng các slot
jointly valid, không cộng đúp. Rule demo giữ nhãn, không fallback ngầm từ
setpoint requested sang VIP realized. UI có actual/cận, coverage, drill-down
và CSV. Fixture chuẩn: out 15 phút/valid 25, hai đợt 5 và 10 phút.

**BR-02 — Theo dõi tưới và drain.** Người vận hành xem lượng observed riêng,
lượng paired và tỷ lệ theo ngày/tuần/vụ từ `Resources.Irr/Drain`. Tỷ lệ kỳ bằng
100 × tổng drain paired/tổng irrigation paired; không lấy trung bình các tỷ lệ
ngày. Mẫu số 0 trả NULL. Coverage riêng và paired cho biết tập đóng góp; drain
lớn hơn tưới có cờ giải thích. Ví dụ fixture 40 L/m² tưới,14 L/m² drain cho 35%.
Chỉ đổi sang lượng tuyệt đối khi basis tương thích, có diện tích được ghim.

**BR-03 — Tìm các đợt EC/pH drain ngoài mục tiêu.** Kỹ sư dinh dưỡng chọn rule,
metric và cửa sổ trên Climate, xem episode, thời lượng, unknown/suspect và tưới
cùng ngày. Áp dụng cùng hợp đồng slot/gap/quality với BR-01; rule không có evidence
vận hành phải là demo. Đây là chỉ báo từ dữ liệu process computer, không suy ra
phân tích hóa học độc lập hoặc chẩn đoán nguyên nhân. Export giữ rule/version.

**BR-04 — Phân tích năng lượng và kWh/kg loại A.** Quản lý xem điện high/low,
nhiệt và tổng năng lượng, rồi chia cho sản lượng A khi đủ dữ liệu/basis. Resources
đã là lượng theo ngày: Heat_cons MJ/m²/ngày đổi sang kWh bằng chia 3,6; điện giữ
kWh/m²/ngày, không nhân thêm số giờ. Từng nguồn tổng hợp trước khi ghép để tránh
nhân dòng. Tử/mẫu phải cùng cửa sổ và basis; A=0 hoặc dữ liệu/basis không đủ
thì intensity NULL có lý do. Fixture 500 kWh/100 kg A cho 5 kWh/kg.

**BR-05 — Theo dõi sản lượng và cơ cấu A/B.** Quản lý xem record thu hoạch,
tổng ngày/tuần/vụ, lũy kế và B/(A+B) trên cùng record paired. Giữ record identity,
không gộp nhầm nhiều record cùng ngày. Không có event thu hoạch không mặc định
thu 0; unresolved/quarantine làm completeness khác một vụ đầy đủ. Ví dụ AGC
Digilog 19/02: A 0,05 và B 0,10 kg/m², share B 66,67%. Reference ngoài vụ giữ quarantine,
không tự sửa năm để tăng sản lượng.

**BR-06 — So sánh chi phí và kịch bản giá.** Quản lý xem lượng×giá hiệu lực theo
ngày/component, baseline và delta của scenario giữ nguyên quantities. Rate
interval nửa mở, đơn vị/currency và overlap/gap được kiểm; scenario có request ID,
author, version, frozen context và toàn bộ rates trong cùng envelope. Cùng request
và payload trả cùng kết quả, khác payload bị từ chối. Ví dụ điện +20% ở cửa sổ demo
làm tổng chi phí tăng 18,41%, không phải 20% vì còn thành phần nhiệt. Đây là mô phỏng
giá trên lượng lịch sử, không phải hóa đơn thật, dự báo hoặc tối ưu điều khiển.

Đặc tả chi tiết công thức, edge cases và AC nằm ở sáu file `docs/br/br_01…06*.md`.
Sensor replay mở rộng BR-01/03, không tính thành BR thứ bảy và không tạo
Resources/Production giả để lấp dữ liệu của các BR còn lại.

# 2. Nguồn dữ liệu, grain và quyết định ngữ nghĩa

Nguồn là Autonomous Greenhouse Challenge 2nd edition 2019–2020, lấy từ dataset
Kaggle [1]. ZIP có SHA-256
`c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68`.
Inventory 45 file gồm 43 CSV và 2 PDF,622.899 dòngCSV. `GreenhouseClimate.csv` có
grain khoang/vụ×slot 5 phút; `Resources.csv` theo ngày; `Production.csv` theo record
thu hoạch. Có sáu khoang; Weather và nguồn đo khác là bối cảnh, không tự nhập vào
KPI. Pipeline nạp các loại nguồn phục vụ BR, không tuyên bố 622.899 dòng đều là
core: full backfill được đối soát 287.989 revision và 14.062.934 source measures.

Raw giữ ZIP, cấu trúc giải nén và manifest checksum bất biến. Parser giữ source
file/row, serial thời gian, giá trị và lỗi. Timestamp nguồn chưa xác minh timezone
nên giữ wall clock naive, không tự gắn UTC. Timestamp nhận/vận hành có UTC thật.
Missing, invalid, suspect, zero và NULL có nghĩa riêng; quality theo từng measure
để một cột lỗi không xóa cột hợp lệ cùng record. Quarantine ngoài vụ và conflict
được hạch toán trong audit, không lặng lẽ bỏ hoặc sửa dữ liệu.

Diện tích 62,5 m² `AGC_SHARED_BASIS_V1` là **assumed_unverified**, sensitivity
96/62,5=1,536; không phải diện tích đã xác minh từ nguồn. Rule demo và tariff
lịch sử có version/provenance. `Economics.pdf` trong snapshot ghi heat 0,0083 EUR/MJ,
điện high/low 0,08/0,04 EUR/kWh; đây không phải giá thị trường hiện tại. Decimal
exact text giữ tiền/lượng qua database và CSV; làm tròn chỉ ở hiển thị.

Simulator đọc các dòng Climate thật, gắn replay/event identity và phát qua
SQLite outbox→MQTT→collector. Replay là lịch sử được phát lại, không phải sensor
phần cứng mới đo. Mỗi replay/fault run có scope riêng; không cộng với lịch sử.
Source clock phục vụ episode khác operational clock phục vụ freshness/latency.

# 3. Giải pháp kỹ thuật và ứng dụng

![Kiến trúc thực chạy: native batch và sensor streaming](figures/architecture.png)

## 3.1. Warehouse Hybrid trên ClickHouse

Luồng chính là raw→parser/quality→canonical core revisions→typed marts→verified
COMPLETE publication→shared services→Streamlit/Plotly. ClickHouse 25.8.33.6 là
warehouse mặc định; PostgreSQL 17.10 giữ adapter đối chứng/rollback trong Compose
và project riêng. Native historical batch dùng Python 3.12.12. Spark 4.0.1 thực sự
chạy Structured Streaming cho sensor; không gọi đường historical Python là Spark.

Core append-only giữ logical key, source identity, revision ordinal, exact
payload hash, correction parent/actor/evidence và membership của mỗi load.
Marts có grain rõ: water theo ngày/measure, harvest theo record, threshold theo
slot và loại climate/drain, energy theo ngày/component. Intensity/cost là
services trên quantities, basis, production, tariff và scenario đã ghim. ERD
trong `docs/architecture/` mô tả quan hệ logic: ClickHouse không cung cấp các
SQL FK/UNIQUE constraints giống PostgreSQL trong mô hình này.

**Ví dụ kỹ thuật 1 — retry/correction:** cùng identity+payload sau lost ACK được
đối soát và collapse trước aggregate; cùng ordinal nhưng khác payload là conflict.
Correction tạo revision mới, publication mới; pin cũ vẫn chọn đúng revision cũ.
Không dùng eventual background merge, ReplacingMergeTree hoặc watermark làm bằng
chứng exactly-once. Supported writers cùng lấy POSIX flock trên coordinator local.
Intent/query ID được ghi bền trước SQL; outcome chưa rõ phải reconcile terminal
query_log trước retry. Đây là thiết kế single-node, không phải distributed lock.

**Ví dụ kỹ thuật 2 — publication:** loader viết bundle với attempt/config IDs,
kiểm count, checksum, exact membership và công thức rồi append complete envelope.
Reader chỉ nhận generation complete hợp lệ, pin một ID cho toàn action/CSV.
Data viết dở hoặc scenario chỉ có header không được lộ. Full scenario/rates nằm
trong một envelope. App không tự migrate, nạp fixture hay điền số 0 khi DB trống.

## 3.2. Sensor và vận hành local

**Ví dụ kỹ thuật 3 — transport và warehouse receipt:** publisher giữ outbox bền;
collector manual ACK sau SQLite spool commit và materialize raw-ready nguyên tử,
giữ exact payload bytes. Spark foreachBatch ghim stream/generation/batch/config;
sink kiểm raw delivery count, ordinal, hash rồi mới ghi receipt và replay seal.
PUBACK không chứng minh warehouse đã nạp. Manifest/cursor/checksum đầy đủ và không
missing/conflict mới thành complete. Late/conflict tạo pin mới, không sửa pin cũ.
Warehouse mới cần checkpoint generation mới; không dùng checkpoint PG đã ACK để
bỏ qua event chưa vào CH. Finite producer dừng có thể khiến collector stale trong
khi replay vẫn complete, hai nhãn mô tả hai trạng thái khác nhau.

Docker Compose đóng gói DB, app, tool job, relay và sensor services. DB/app/job
trên network internal, chỉ fixed-target relay bind localhost 18581 mặc định.
Image/dependency chuẩn bị trước; runtime không cần cloud hay tải package.
Native client dùng HTTP standard library; image processing chung còn JDBC cho PG
compatibility nhưng không yêu cầu PostgreSQL server. Dependency locks/digest ở
`requirements*.lock`, Dockerfiles và Compose. Chỉ arm 64 đã kiểm runtime thật;
không suy kết quả sang kiến trúc máy chưa chạy.

**Ví dụ kỹ thuật 4 — backup và rollback:** tại cutoff dừng writer/producer,
drain stream, giữ DB/config, raw/manifests, coordinator, broker, SQLite DB/WAL/SHM,
spool/ready và checkpoint cùng mốc. Physical restore và raw rebuild là hai đường
khác nhau; rebuild cần mapping pin/generation. Sau cutover, durable rollback
journal và immutable source/config archives giữ cả correction, scenario và
sensor mới. Quay lại PG cần import/rebuild/parity trước khi mở app, không chỉ đổi
connection string về snapshot cũ. Thiếu journal/sai namespace/intent chưa rõ
phải chặn ghi khi cờ require-journal đã bật.

## 3.3. Ứng dụng phục vụ người dùng

Chín màn hình gồm sáu BR, tổng quan vụ, quality và sensor replay. Người dùng
chọn publication hiện hành theo scope, khoang/vụ, ngày, rule/basis và scenario;
context tương thích được giữ khi đổi màn hình. Biểu đồ, bảng, drill-down,
lineage và export lấy cùng DTO/services, không có công thức KPI riêng ở UI.
CSV giữ exact Decimal/NULL, units, requested/effective window, quality, basis,
rule, scenario và publication/config versions. Lineage kiểm hash raw khi mount
khả dụng; không có raw verification thì hiển thị chưa xác minh.

Read proof cache giới hạn hai entries trong process, kiểm namespace/server/
schema/parts/mutations và config; physical change làm proof mất hiệu lực.
Cold proof có thể mất vài phút. Đổi backend reset context/write intent, không
fallback PG khi CH lỗi. Lưu/mở lại scenario phải giữ frozen context; không tự
áp scenario của publication cũ lên publication mới sau correction.

# 4. Phương án thay thế và phương pháp đánh giá

Có hai phép so riêng. **Kiến trúc:** Hybrid và Kimball gọn chạy trên cùng PG,
raw/parser/quality/công thức/workload. Kimball đưa staging chuẩn hóa trực tiếp
vào dimensional facts, ít lớp vật lý hơn; Hybrid giữ core revision/lineage.
Kimball đã có parity sáu BR, nhưng loader từ chối ghi đè publication và chưa
chứng minh correction/recovery tương đương; không gọi rerun bị từ chối là thành công.

**Engine:** native Hybrid ClickHouse và Hybrid PostgreSQL giữ snapshot, semantic
contracts, sáu BR và resource caps. PG lifecycle có Spark candidate stage, CH
historical builders dùng Python; lifecycle so hai implementation, không cô lập
chi phí riêng DB. Query warm đo trong một process với CH proof cache như UI,
không lấy cold startup làm warm sample. Hai phép so không gộp thành kết luận
rằng đổi cả engine và kiến trúc làm hệ thống nhanh hơn.

ClickHouse phù hợp mô hình columnar/append-only phân tích đã chọn và footprint
được đo nhỏ hơn. Đổi lại phải tự triển khai immutable publication, writer
serialization và reconciliation. PG có transaction/constraints tiện hơn cho
scenario/config, đồng thời nhanh hơn ở các workload warm đã đo. Lựa chọn CH là
hướng triển khai đã được kiểm, không phải tuyên bố nó cần thiết hoặc nhanh hơn
cho dataset nhỏ này. Không dùng cloud, Kafka, Airflow hay Kubernetes làm phụ
thuộc bắt buộc khi phạm vi local đã đáp ứng được bằng stack hiện tại.

Đánh giá chia ba nhóm theo đề:

| Trục | Phương pháp và tiêu chí | Giới hạn |
|---|---|---|
| Correctness | Fixture oracle độc lập, raw reconciliation, six-BR parity, exact CSV/lineage, fault/recovery, immutable retry/pin | Chứng minh phần mềm trên nguồn và faults đã chạy |
| Performance | Fresh/resume lifecycle, storage,5 warm-up+30 query samples/BR,p50/p95 nearest rank; warm summary mục tiêu≤2 s | Không SLA/cold/concurrent users; một máy và một snapshot |
| Effectiveness | AppTest và Playwright tác vụ có đáp án trước, filter/drill-down/export/save/reopen | Chưa có người tham gia; không đo thời gian/hiểu biết người dùng |

# 5. Kết quả và thảo luận

## 5.1. Correctness, sensor và recovery

Full native AGC parity đạt sáu BR,96 CSV,30 lineage views,18 threshold groups và
67.829 episode qua cả biên vụ. PG/raw oracle độc lập giữ nguyên expected values;
không sửa kỳ vọng để làm parity đạt. Sau cutover và correction, gate đầy đủ này
đã được chạy lại. Suite cuối T39 có 100 test,91 chạy đạt/chín heavy skips;24 PG cost
và 8 routing pass ở các suite riêng có thể giao nhau, không cộng thành một tổng.

T50 diễn tập clone/offline, physical restore 28 bảng/17.513.148 physical rows,
raw rebuild cùng config/scenario và MQTT/WAL/checkpoint. Physical row count
không đồng nghĩa business record count. T47/fault tests kiểm lost INSERT ACK,
duplicate/conflict, late/out-of-order và restart các service. Replay sạch 30 dòng
Climate AGC khớp batch với business fingerprint
`7783fdfa2f63909171709dea99b43bb5e6956af13fde3dca15ce4d1030d1f675`.
Đây là finite sensor window, không phải replay toàn vụ hay bảo đảm mọi lỗi mất điện.
Container internal không có egress; host vẫn online trong các đợt diễn tập.

T39 cutover giữ 315 config envelopes,178 scenario versions,62 journal intents và
bảy replay pins thuộc bốn scopes, gồm partial pins cũ. Reverse import giữ cả
identity, normalized replay clock, received time, state/cursor/checksum và original
envelopes. Một scenario family trùng ID từ hai lineage được namespace và map
bất biến; PG original không bị ghi đè. Backup PG cuối 239.741.319 byte đã đọc
**toàn bộ** archive, không chỉ liệt kê header. Chỉ fixture agent được dựng lại;
originalPG containers/volumes không bị thay đổi. App native chín màn hình đạt khi
PG dừng; quay lại native với journal active đã kiểm.

Lỗi importer đã được main phát hiện qua caption replay clock: raw source timestamp
khác normalized slot cursor. Đã sửa importer và gate đủ metadata tuple, giữ forensic
dump và dựng lại fixture riêng, không sửa immutable pin hoặc nới expected output.
HTTP switch 9,250 s và KPI cold 198,954 s là hai phép đo; không gọi 9 s là tổng downtime.

## 5.2. Ví dụ AGC có publication rõ ràng

Video ClickHouse ghim publication gốc `4b08bd51c092465783304f887d766d47`.
Automatoes 02–03/03/2020 có tưới 9,36/drain 3,89 L/m², paired ratio 41,56%; năng lượng
308,078 kWh với basis 62,5 m² giả định. A=0 nên intensity NULL. Tariff lịch sử cho
17,60006771 EUR; điện +20% cho 20,840734377 EUR, delta 3,240666667 EUR, tổng+18,41%.
Giá trị này là một cửa sổ cụ thể, không đại diện toàn vụ hay hóa đơn thực tế.

Sau đó, diễn tập T39 tạo correction có audit `cutover-resource-20261107`, cộng
**1 L/m² Irr ngày 02/03** và publish bản
`5d455686a06649e789908b9708a9d436`. Raw AGC và pin gốc không đổi. App hiện hành
vì vậy hiển thị tưới 10,36, drain 3,89 L/m² ở cửa sổ trên; tỷ lệ mới≈37,55%.
PostgreSQL rollback pin `c51402d9d25f45c182140b072397de78` khớp bản đã correction.
Không trộn tỷ lệ gốc 41,56% với tử/mẫu mới, hoặc gọi correction là quan sát AGC gốc.
Các BR năng lượng/thu hoạch giữ cùng đầu vào vì chỉ Irr thay đổi. Catalog UI chọn
generation hiện hành; xem pin cũ qua semantic API hoặc video đã ghim, không hứa
UI có selector cho mọi publication cũ.

## 5.3. Performance theo engine

Máy đo Apple M 4/RAM 16 GiB; Docker 4 CPU/khoảng 7,62 GiB. Benchmark engine đặt mỗi
DB 1 CPU/2 GiB, processing 1,5 CPU/2 GiB. Latency Automatoes toàn 166 ngày,47.808 slots,
5 warm-up+30 mẫu/BR. Giữ rule/config/công thức, cache và post-compaction state rõ
trong artifact. P 95 nearest-rank; không flush OS cache hoặc đo concurrent load.

| Workload | PG p95 (ms) | CH p95 (ms) |
|---|---:|---:|
| BR-01 climate | 1497.88 | 1786.48 |
| BR-02 water | 11.53 | 108.25 |
| BR-03 drain | 1052.17 | 1362.62 |
| BR-04 intensity | 20.06 | 118.41 |
| BR-05 harvest | 5.18 | 99.19 |
| BR-06 cost | 16.86 | 134.12 |

Cả sáu native workloads đạt mục tiêu **warm summary p95≤2.000 ms** trong lượt
cuối; PG nhanh hơn ở cả sáu. Cold process/proof+catalog trong T49: PG 0,167 s,
CH 141,242 s; T51 cold browser sau sửa memory 356,497 s trong điều kiện khác.
Không gộp các lần này hoặc biến warm target thành startup SLA. Fresh lifecycle
PG 360,290 s/CH 561,298 s; resume 4,449/170,453 s nhưng mức kiểm lại khác nhau.
Native scenario writes và verification có chi phí đáng kể; đã công bố thay vì
suy tốc độ ghi từ benchmark SELECT. Active CH table footprint thấp hơn; không
đồng nhất active parts với toàn bộ Docker disk/image/log/cache.

Benchmark kiến trúc PG trước đó giữ riêng: Hybrid/Kimball fresh 880,33/40,35 s,
DB 3,319/0,623 GiB; BR-01 p95 gốc 4060,81/3028,13 ms, cả hai vượt 2 s. Follow-up Hybrid
sau tối ưu 1770,93 ms là lượt riêng; Kimball chưa đo lại. Không thay số gốc bằng số
engine benchmark sau này hoặc gọi một lần đạt là SLA. Nguồn raw samples,
limitations và profiler ở `docs/evaluation/performance.md`,
`rule-engine-optimization.md`, `clickhouse-performance.md`.

## 5.4. Khả năng khai thác và vấn đề thực tế

Bảy video native 1080 p đạt 109 bước,21 file xuất, tổng 27 phút 22,56 s: sáu BR và một
sensor replay với MQTT/Spark thật. Full decode/player/chapter frames đã kiểm;
metadata và checksums ở `docs/evaluation/assets/clickhouse-walkthrough/`.
Video PG cũ là bộ riêng 93 bước/19 exports, không đổi nhãn thành CH. Walkthrough
kiểm điều hướng, chọn context/rule, drill-down, export, lưu/mở lại scenario và
replay 10→30 events. **Chưa có người tham gia**, không suy thời lượng automation
thành thời gian thao tác/độ hài lòng hoặc mức tiết kiệm của người dùng.

Cold proof từng lỗi memory. Chẩn đoán ghi background metric_log merge khoảng
889 MB khi Docker/server cgroup gần giới hạn; đã tắt periodic metric_log writer,
giữ log cũ và query_log terminal evidence, không tăng memory cap để che lỗi.
Late HTTP 200 stream exception được client phát hiện thay vì coi error JSON là
row dữ liệu. Những lỗi và phép kiểm hồi quy giữ trong evidence, không bị xóa.

# 6. Tái lập, bàn giao và kết luận

README trình bày fresh path: chuẩn bị images/dependency và ZIP đúnghash,
`docker compose` mặc định native; snapshot→migrate→run-pipeline với basis giả
định→register tariff→mở app. Data chưa nạp thì app phải báo chưa có publication.
PG dùng `compose.postgres.yaml` và project riêng. Không đổi `.env`/project/volume
của deployment hiện hữu như cách bootstrap mới. Sau cutover phải giữ required
journal/coordinator cùng namespace. Cần disk headroom cho verified temporary rows,
warehouse và backup; thiếu storage có thể dọn Docker build cache, không xóa raw,
backup hoặc data volumes để lấy chỗ cho phép đo.

Gói nộp local ở `project-submit/`: technical report PDF, slides PDF, source/app
và bộ walkthrough; kịch bản 24 phút và checklist chuẩn bị video thuyết trình.
Kiểm **pre-video** khác kiểm cuối đủ bốn sản phẩm. Nhóm điền danh tính/MSSV,
quay video 20–30 phút có phần trình bày của ba thành viên, xem/nghe toàn bộ và kiểm
lại trước nộp. Agent không tự gửi bài hoặc tạo video đen để vượt kiểm tra.

Project chứng minh sáu nhóm phân tích và sensor replay local với exact numeric,
quality, immutable versions và lineage; có alternative designs, raw oracle,
performance và walkthrough evidence. Giới hạn còn lại: single-node/flock, cold
proof nhiều phút, chi phí scenario writes, basis/timezone chưa xác minh,
rule demo, thiếu user study và không có sensor/actuator thật. Không kết luận
hệ thống tăng năng suất, giảm tài nguyên hoặc lợi nhuận nếu chưa đo thực nghiệm.

**Nguồn và tài liệu kiểm chứng**

1. Dataset AGC: [Kaggle Autonomous Greenhouse Challenge](https://www.kaggle.com/datasets/piantic/autonomous-greenhouse-challengeagc-2nd-2019/); ReadMe/EconomicsPDF trong ZIP đã khóa. Nguồn trực tiếp của các cột và tariff lịch sử.
2. Đề môn: `DE_Requirements.pdf`, mụcProject và rubric; nguồn số lượngBR, ba trục đánh giá và bốn sản phẩm nộp.
3. Dataset inventory/decisions: `docs/implementation/dataset-snapshot.md`, `data-inventory.md`, `data-decisions.md`.
4. Thiết kế: `docs/adr/003-clickhouse-warehouse.md`, `docs/architecture/README.md` và sáu đặc tả `docs/br/`.
5. Evidence CH: `docs/evaluation/clickhouse-parity.md`, `clickhouse-performance.md`, `clickhouse-local-repro.md`, `clickhouse-acceptance.md`, `clickhouse-cutover.md`; mỗi manifest cóhash/source/image/version.
6. PG alternatives/effectiveness: `docs/evaluation/kimball.md`, `performance.md`, `usability.md`; không dùng sốPG làm sốCH.
7. Mã nguồn và hướng dẫn: [smart-green-house repository](https://github.com/arcanine525/smart-green-house/tree/develop), `readme.md`; source archive và delivery manifest nằm trong gói nộp.
