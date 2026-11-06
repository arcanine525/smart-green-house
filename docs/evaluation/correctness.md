# Kiểm chứng correctness và phục hồi

Tài liệu này nối kết quả theo từng BR với oracle độc lập, contract và kiểm thử
phục hồi. Đây là bằng chứng về phần mềm và snapshot/fixture được nêu; không phải
ước lượng hiệu quả canh tác. Các lần đối soát lịch sử dưới đây có publication và
điều kiện khác nhau; không ghép các con số thành một lần chạy duy nhất.

## Chạy lại bộ kiểm tra local

Chuẩn bị Docker/Compose, `.env`, các image và dependency như [hướng dẫn local](../implementation/local-environment.md),
sau đó chạy:

```bash
./scripts/test_local.sh
```

Lệnh build image processing/UI, chạy `tests/unit` (gồm contract T31, parser,
policy, BR và pipeline), `tests/ui` (bao gồm export và điều hướng), rồi gọi
`test_sensor_acceptance.sh` để chạy fault matrix và các lần restart. Log có thời
điểm, HEAD và trạng thái working tree trong `tmp/local-tests/<run-id>`; log của
sensor ở `tmp/sensor-recovery/<run-id>`. Test dùng DB fixture mang tiền tố `test_`;
acceptance sensor tạo DB/broker riêng và chỉ dọn container do chính lượt đó tạo.
Không thay thế các volume, publication hay raw của người dùng. Cần đủ tài nguyên
và ảnh/dependency build đã tải trước; lệnh build có thể cần mạng nếu thiếu cache.

Các test điều phối pipeline dùng Spark stage giả lập để cô lập fault; suite sensor
kiểm tra Structured Streaming và restart Spark thật. Fixture không phải là phép đo
hiệu năng hay chứng minh chất lượng dữ liệu thực tế. Trường hợp phải truy cập AGC
raw hoặc publication lịch sử cần chuẩn bị snapshot như báo cáo từng BR nêu.

## Kết quả lượt kiểm tra 2026-11-06

Lượt chạy bắt đầu **14:59:19 UTC**, source HEAD `5c74d81cce9b316a144b67281a9d2d5f875cca06`;
script điều phối mới chưa commit tại thời điểm chạy. Docker 29.9.2, Compose 5.6.1,
Python 3.12.12, PostgreSQL 17.10. Backend **419 passed**, UI **97 passed**;
sensor **19 passed**: replay 1, Spark restart 1, fault matrix 7, broker QoS 8,
PostgreSQL restart 1, broker/collector/simulator restart 1. Chi tiết số đếm và
thời gian: [kết quả máy đọc được](assets/local-correctness/run-20261106.json).
Đây là một lượt test local, không phải benchmark hoặc thử nghiệm người dùng.

## Oracle và bằng chứng từng BR

| BR | Oracle/đối soát ngoài core-vs-mart | Phạm vi và giới hạn |
|---|---|---|
| 01 khí hậu | [Runner](../../src/greenhouse/evaluation/br01.py), [kết quả AGC](assets/br01-agc-reconciliation.json), [báo cáo](br_01.md) | So 8 giá trị raw với frozen slot trên sáu vụ; oracle KPI/episode tự thực hiện, nhưng dùng quality frozen chứ không phân loại lại policy từ raw. Hai basis, 12 evaluation. |
| 02 nước | [Runner](../../src/greenhouse/evaluation/br02.py), [đối soát raw](assets/br02-agc-reconciliation.json), [báo cáo](br_02.md) | CSV + Decimal độc lập trên sáu khoang; tổng và tỷ lệ khớp exact. Fixture có partial, zero, NULL, duplicate/conflict, pin và export. Basis nước vẫn assumed-unverified. |
| 03 EC/pH | [Runner](../../src/greenhouse/evaluation/br03.py), [đối soát raw](assets/br03-agc-reconciliation.json), [báo cáo](br_03.md) | Phân loại raw và scan episode độc lập trên sáu khoang; so quality, phút, joint và lineage, tolerance tỷ lệ `1e-6`. |
| 04 năng lượng | [CSV oracle](assets/br04/reconcile.py), [kết quả](assets/br04/raw-reconciliation.json), [báo cáo](br_04.md) | Đối soát nguồn Resources/Production; số nguồn và population exact, tỷ số quy đổi tolerance `1e-40`. Diện tích dùng giả định được gắn nhãn. |
| 05 thu hoạch | [Runner](../../src/greenhouse/evaluation/br05.py), [đối soát raw](assets/br05-agc-reconciliation.json), [báo cáo](br_05.md) | CSV + Decimal độc lập, tổng trong cửa sổ và record loại B, quarantine ngoài vụ; không biến ngày không có sự kiện thành sản lượng bằng 0. |
| 06 chi phí | [Oracle](assets/br06/oracle.py), [báo cáo](br_06.md) | So Resources với phép nhân giá theo thành phần/ngày và scenario; đăng ký biểu giá/scenario kiểm thử có ghi DB như báo cáo công bố, không sửa publication nguồn. Không xem giá/scenario là dự báo thực tế. |

Kỳ vọng fixture có hằng số riêng trong các báo cáo; so sánh service với cùng
công thức service hoặc chỉ so core với mart không đủ để coi là oracle độc lập.
Publication và checksum từng phép đối soát được giữ trong artifact tương ứng.

## Phục hồi, thay đổi và publication

- [`test_pipeline.py`](../../tests/unit/test_pipeline.py): rerun cùng snapshot;
  duplicate/conflict và missing; fault ở các điểm của workflow, resume so với
  chạy sạch; correction tạo revision/publication mới nhưng pin cũ giữ giá trị cũ.
- [`test_action_exports.py`](../../tests/ui/test_action_exports.py) và các test
  UI từng BR: pin publication xuyên nhiều truy vấn/CSV, giữ NULL khác 0 và
  metadata/cửa sổ; [scenario UI](../../tests/ui/test_cost_ui.py) mở lại version
  và baseline đã pin. UI không giữ cache connection; service climate/drain có
  cache kết quả giới hạn, key gồm database, publication/version, context, basis
  và chữ ký nội dung rule sau khi kiểm tra identity. Chi phí/scenario dùng context
  và version đã pin; không suy ra cache scenario từ cache climate/drain.
- [Sensor replay](sensor-replay.md): fault, trùng/conflict, late, restart broker,
  collector, PostgreSQL, Spark và pause/resume; đối soát batch với cùng scope
  và dữ liệu nguồn. Các restart này chạy trên dịch vụ test riêng.

Các báo cáo từng BR ghi cụ thể test, phiên bản, ngoại lệ và giới hạn đo. Một test
pass trên fixture không chứng minh mọi tổ hợp hoặc mọi dataset đều đúng.
