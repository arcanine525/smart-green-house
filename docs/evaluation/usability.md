# Đánh giá khai thác sáu BR

## Phạm vi T26 được cập nhật ngày 2026-11-07

Theo quyết định user, T26 dùng **walkthrough tự động Playwright**, tối thiểu sáu
video MP4 1080p, một video mỗi BR, với chú thích tiếng Việt, số bước và vị trí
thao tác; không giọng đọc. Mỗi luồng bao gồm bộ lọc/context, KPI/biểu đồ,
rule/basis/grain khi áp dụng, chi tiết/lineage và xuất dữ liệu. BR06 có tạo,
lưu, mở lại scenario trên database demo riêng. Script và hướng dẫn ở
[`tools/walkthrough`](../../tools/walkthrough/README.md).

Đánh giá người thật là mở rộng tùy chọn, không còn là điều kiện đóng T26.
Kết quả video chứng minh luồng thao tác tự động và đáp án được kiểm tra; không
suy ra khả năng hiểu coverage, phản hồi UX hay thời gian thao tác của con người.
Các kết quả AppTest và protocol người tham gia bên dưới được giữ làm lịch sử
và tài liệu bổ sung; chúng không thay thế bộ video Playwright mới.

## Kết quả video Playwright ngày 2026-11-07

**6/6 luồng đạt, 93 bước, 19 file xuất; tổng video 16 phút 40 giây.** Mỗi video
MP4 1920×1080, H.264, không audio, nhịp `PACE=1`. Đã giải mã toàn bộ sáu MP4,
kiểm checksum, xem một khung hình đại diện mỗi BR và kiểm sáu player cùng nút
chuyển bước bằng Chromium. Đây là bộ hướng dẫn theo BR, riêng với video thuyết
trình project 20–30 phút của T30.

| Video | Số bước | Thời lượng | Kết quả |
|---|---:|---:|---|
| BR-01 · Khí hậu | 16 | 4:00 | Pass |
| BR-02 · Tưới và drain | 14 | 2:15 | Pass |
| BR-03 · EC / pH drain | 15 | 2:42 | Pass |
| BR-04 · Năng lượng | 13 | 2:02 | Pass |
| BR-05 · Thu hoạch A / B | 15 | 2:33 | Pass |
| BR-06 · Chi phí và kịch bản giá | 20 | 3:08 | Pass |

Mở trên máy đã quay, từ root repository:

```bash
open tmp/walkthrough/20261106T173153Z-9455/videos/index.html
```

Trang local có sáu video và nút chuyển tới từng bước. Video/trace/file xuất giữ
ngoài Git. [Danh mục bằng chứng](assets/playwright-t26/README.md),
[kết quả máy đọc được](assets/playwright-t26/summary.json) và
[kiểm tra MP4](assets/playwright-t26/verification.json) được lưu trong repository.

Ứng dụng ở commit `ae9303d`, Playwright 1.65.0; publication
`df3e4bcbacab45a5b86fc37d2f53c00a`, database demo riêng
`test_walkthrough_20261106T173153Z_9455`. Rule nước được bổ sung chỉ trong bản sao.
BR06 tạo scenario qua UI, bỏ chọn rồi mở lại đúng version; JSON baseline
**17,60006771 EUR** và scenario **20,840734377 EUR** khớp oracle raw Decimal.
BR01–05 tương tác biểu đồ; BR06 dùng bảng đóng góp hiện có trong ứng dụng.
Chi tiết thao tác, snapshot, rule, basis và SHA recorder có trong bằng chứng.

T26 hoàn thành theo phạm vi user đã chốt; không đo khả năng hiểu hoặc hiệu quả
thao tác của người thật. Các lượt AppTest bên dưới là bằng chứng trước đó.

## Kết quả AppTest trước đó

Cập nhật 2026-11-06. Sáu kịch bản dưới đây có đáp án trước và được chạy qua
Streamlit AppTest với fixture tổng hợp, PostgreSQL test và UI production. Cả sáu
kịch bản tự động đều đạt. **Chưa có phiên đánh giá bởi người dùng**: không có
thời gian hoàn thành, điểm giải thích coverage hay phản hồi giao diện của người
thật. Vì vậy kết quả hiện tại chứng minh các thao tác được script kiểm tra có thể
chạy và trả về đáp án đã định; chưa đủ để kết luận người dùng thao tác hiệu quả
hay project làm giảm tài nguyên/tăng lợi nhuận.

## Sáu kịch bản có đáp án trước

Mỗi trường hợp sử dụng scope/publication fixture riêng; rule, basis và giá là
synthetic, không phải chỉ dẫn vận hành nhà kính. Thời gian là thời gian test
máy, **gồm setup fixture**, không phải thời gian người dùng.

| BR · tác vụ | Đầu vào, thao tác và đáp án trước | Kết quả tự động | Thời gian test |
|---|---|---|---:|
| 01 · tìm đợt khí hậu | Fixture 2 ngày, nhiệt độ ngoài dải lúc 00:00 và 08:20 ngày 01/01; chọn rule demo, mở đợt thứ hai, đổi basis và cửa sổ. Đợt 08:20 còn đúng khi đổi basis và trở lại khi chọn lại ngày đầu. | Pass; hiển thị 08:20 và không giữ đợt cũ khi đổi sang ngày 2. | 3,991 s |
| 02 · xem ngày drain | Hai ngày lần lượt Irr/Drain 10/2 và 30/12 L/m². Chọn ngày thứ hai: Irr 30, Drain 12, tỷ lệ paired 40%, coverage 100%. | Pass; cả bốn KPI khớp. | 0,187 s |
| 03 · tìm đợt EC | Fixture EC vượt ngưỡng tại slot 0, 1 và 100; chọn rule demo, chọn đợt thứ hai rồi lọc pH và đổi thời lượng tối thiểu. Danh sách đổi theo lọc, tổng phút EC không đổi. | Pass; chọn đợt thứ hai, bộ chọn chuyển sang pH và tổng EC giữ nguyên. | 5,626 s |
| 04 · kiểm tra kWh/kg | Fixture tổng năng lượng 500 kWh, sản lượng loại A 100 kg, dự kiến 5 kWh/kg A. Chọn ngày trước thu hoạch: năng lượng 200 kWh, tỷ số NULL; portfolio nguyên vụ giữ 5. | Pass; bốn KPI nguyên vụ và cửa sổ con khớp, lineage mở được. | 0,425 s |
| 05 · tìm kỳ B cao | Hai lần thu A/B = 1/0,2 và 2/0, rule demo 10%. Record đầu 16,67% vượt, tổng cửa sổ 6,25% trong dải; đổi grain ngày/tuần không thay ý nghĩa từng grain. | Pass; một record và một ngày vượt, tuần không vượt. | 0,370 s |
| 06 · tạo scenario giá | Fixture nhiệt 100 MJ/m², điện cao/thấp 10/5 kWh/m², basis giả định 62,5 m²; tăng hai giá điện 20%. Kỳ vọng 2,03 EUR/m², baseline 114,38 EUR và scenario 126,88 EUR sau làm tròn hiển thị. | Pass; scenario lưu và đọc được qua kết nối DB thứ hai, các KPI khớp. | 0,311 s |

Nguồn kiểm thử và assertion nằm trong `tests/ui/test_climate_ui.py`,
`test_app_ui.py`, `test_drain_ui.py`, `test_energy_ui.py`, `test_harvest_ui.py`
và `test_cost_ui.py`. Bằng chứng máy đọc được:
[JUnit XML](assets/effectiveness/automated-20261106.xml). Lượt chạy trên source
`7c6979e3ac8b7f8c0bf3f69d4529b0245267dcd0`, image UI đã build, ngày
2026-11-06 15:06 UTC: **6/6 pass, tổng 11,62 s**. Chạy lại bằng
`./scripts/test_effectiveness.sh`; kết quả mới ở `tmp/effectiveness/<run-id>`.
Thời gian có setup chung và phụ thuộc cache/máy, không dùng so sánh giữa BR.

## Protocol đánh giá người dùng tùy chọn

Có [phiếu tác vụ AGC](usability-participant.md) và [đáp án cho facilitator](usability-facilitator.md) dùng publication và rule đã pin; chúng chưa phải kết quả người tham gia.

Để bổ sung hiệu quả khai thác thực tế, người đánh giá chuẩn bị fixture đúng sáu
kịch bản, không cung cấp đáp án trong khi quan sát. Có thể nhờ thành viên nhóm
đóng vai người dùng; ghi rõ đây là **đánh giá mô phỏng**, kinh nghiệm và mức quen
hệ thống. Người tham gia tự chọn publication/vụ/cửa sổ và thực hiện từng tác vụ;
không dùng thời gian của AppTest thay cho đồng hồ người dùng.

Với mỗi BR ghi:

1. Mã người tham gia ẩn danh, vai trò, mức quen, ngày giờ và phiên bản ứng dụng;
   ghi fixture/scope/publication, rule/basis/scenario và cửa sổ được dùng.
2. Thời điểm bắt đầu/kết thúc, hoàn thành không trợ giúp / có trợ giúp / không
   hoàn thành; câu trả lời và đối chiếu đáp án định trước trong bảng.
3. Yêu cầu người tham gia giải thích ý nghĩa coverage, NULL/partial và giới hạn
   rule/basis/scenario liên quan; ghi nguyên ý và đánh giá đúng/sai theo đáp án.
4. Ghi bước gây nhầm lẫn, câu hỏi, phản hồi và mức độ nghiêm trọng; không sửa
   kết quả sau khi thấy đáp án. Sau mỗi phiên lưu bằng chứng không chứa bí mật.

Một facilitator có thể ghi phiên thật bằng công cụ local:

```bash
python3 scripts/record_usability.py --participant P01 --output tmp/usability/P01.json
```

Công cụ hỏi mã ẩn danh, context/version, kết quả, giải thích coverage và phản hồi
cho từng BR; đo thời gian giữa hai lần facilitator xác nhận bắt đầu/kết thúc bằng
đồng hồ monotonic. Nó yêu cầu facilitator tự đối chiếu đáp án, phân biệt người
dùng với thành viên đóng vai, lưu JSON quyền `0600`, checkpoint sau mỗi tác vụ và từ chối ghi đè file. Phiên
bị gián đoạn giữ `status: in_progress`; chỉ phiên đủ sáu tác vụ mới có `completed`. Cần
có người tham gia thực sự; input giả lập chỉ dùng để test công cụ và không phải
bằng chứng usability. Dữ liệu trong `tmp/` không được commit tự động.

Mẫu ghi một dòng: `participant | BR | context/version | thời gian | đáp án |
đúng/sai | coverage & giới hạn | trợ giúp | phản hồi`. Hiện chưa có dòng quan
sát người thật; không điền giả định hoặc tổng hợp từ test tự động.

## Walkthrough tự động bổ sung trên AGC đã publish

Ngày 2026-11-06, [script](assets/effectiveness/agc_walkthrough.py) chạy UI
production ở chế độ PostgreSQL **read-only** trên database benchmark hiện có,
publication `df3e4bcbacab45a5b86fc37d2f53c00a`, snapshot AGC khóa và khoang
Automatoes. Nó chuyển lần lượt qua sáu BR, tổng quan và chất lượng với cùng hai
ngày 02–03/03/2020; mỗi màn hình phải giữ đúng publication, khoang và cửa sổ,
không có lỗi UI. Toàn bộ tám màn hình đã qua. Thời gian của máy trong lượt này
là **6,523 giây**, không phải thời gian thao tác của người dùng.

Độc lập với service, script kiểm SHA-256 của `Automatoes/Resources.csv` theo
manifest, đọc hai dòng CSV gốc bằng `csv`/`Decimal`, tính Irr **9,36 L/m²**,
Drain **3,89 L/m²**, tỷ lệ paired **41,559829…%**. Màn hình nước hiển thị
**9,36 / 3,89 / 41,56% / 100% coverage** và màn hình drain hiển thị cùng tổng
nước theo ngày; assertion đã kiểm các giá trị. Cùng hai dòng raw, oracle
chuyển đổi nhiệt MJ/3,6, cộng điện và nhân diện tích giả định 62,5 m² cho
**308,078125… kWh**; nhân giá lịch sử nhiệt **0,0083 EUR/MJ**, điện cao/thấp
**0,08/0,04 EUR/kWh** cho **17,60006771 EUR**. UI hiển thị lần lượt
**308,078 kWh** và **17,60 EUR**; cả hai đều được assertion kiểm tra. Diện tích
và giá là cơ sở tính được công bố, không phải diện tích đã xác minh hay hóa đơn
thực. [Kết quả và toàn bộ metrics](assets/effectiveness/agc-walkthrough-20261106.json)
ghi đầy đủ scope/version/cửa sổ. Các KPI khác được kiểm điều hướng và ngữ cảnh trong lượt này, không được coi là có oracle độc lập mới.
Chưa chọn rule climate/drain nên coverage đánh giá là 0%; đây không phải khẳng
định dữ liệu nguồn thiếu. UI có giải thích riêng điều này.

Để chạy lại cần **database đã có publication benchmark tương ứng**, raw snapshot
và image UI đã build; lệnh chỉ đọc database/raw và ghi evidence vào thư mục tạm:

```bash
mkdir -p tmp/agc-ui-walkthrough
chmod 777 tmp/agc-ui-walkthrough
GREENHOUSE_UI_MEMORY=1536m docker compose --profile test run --rm --no-deps \
  -e PGDATABASE=greenhouse_hybrid_bench_t25_20261106_104814 \
  -e PGOPTIONS='-c default_transaction_read_only=on' \
  -e GREENHOUSE_WALKTHROUGH_PUBLICATION=df3e4bcbacab45a5b86fc37d2f53c00a \
  -e GREENHOUSE_WALKTHROUGH_OUTPUT=/evidence/agc-walkthrough.json \
  -e GREENHOUSE_RAW_DIR=/data/raw -v "$PWD/data/raw:/data/raw:ro" \
  -v "$PWD/docs/evaluation/assets/effectiveness/agc_walkthrough.py:/tmp/agc_walkthrough.py:ro" \
  -v "$PWD/tmp/agc-ui-walkthrough:/evidence" ui-test python /tmp/agc_walkthrough.py
```

Đây vẫn là AppTest tự động, không có người tham gia hay phản hồi UI của con
người; không thay thế quan sát con người nếu thực hiện protocol tùy chọn ở trên.
