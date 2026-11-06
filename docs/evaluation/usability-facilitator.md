# Hướng dẫn người điều phối — đáp án AGC

**Không đưa đáp án này cho người tham gia trước hoặc trong phiên.** Các tác vụ
ở [phiếu người tham gia](usability-participant.md) dùng dữ liệu lịch sử,
publication `df3e4bcbacab45a5b86fc37d2f53c00a`, snapshot
`agc-2nd-2019-c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68`.
Không thay đổi publication, rules hay scenario giữa các phiên; nếu bối cảnh thay
đổi cần tính lại đáp án và ghi version mới trước khi quan sát.

## Chuẩn bị ứng dụng chỉ đọc

Các lệnh dưới đây dành riêng cho máy hiện có database benchmark
`greenhouse_hybrid_bench_t25_20261106_104814`, snapshot raw đã mount, publication,
profile demo và scenario benchmark được nêu. Database trên máy khác có thể không
tồn tại: cần chuẩn bị bản test có cùng dữ liệu/config, không đoán tên/ID và
không chỉnh sửa publication lịch sử. `PGOPTIONS` buộc session chỉ đọc, nên bài 6
chỉ mở scenario đã lưu. Đổi cổng nếu 18572 đang bận:

```bash
docker compose build app
docker compose up -d --wait db
GREENHOUSE_APP_PORT=18572 docker compose run --rm --no-deps --service-ports \
  -e PGDATABASE=greenhouse_hybrid_bench_t25_20261106_104814 \
  -e PGOPTIONS='-c default_transaction_read_only=on' app
```

Mở `http://127.0.0.1:18572`, chọn publication trên, xác nhận UI hiển thị raw
lineage khi có snapshot mount, sau đó đưa [phiếu tác vụ](usability-participant.md).
Người tham gia cần tự chọn từng khoang, cửa sổ và rule; không cho xem đáp án.
Công cụ ghi `python3 scripts/record_usability.py --participant P01 --output tmp/usability/P01.json` đo thời gian do facilitator bấm, không thay thế ghi chú.
Ghi loại người dùng hay thành viên đóng vai, mức quen, trợ giúp và version. Nếu
người tham gia đổi task hoặc không xong, ghi đúng thực tế, không sửa thành pass.

## Đáp án định trước

| BR | Context và đáp án | Bằng chứng / giải thích cần nghe |
|---|---|---|
| 01 | IUACAAS, toàn vụ, requested, `climate-demo-b01:IUACAAS:v1`, temperature: dài nhất **1.180 phút**, 21/05/2020 11:45 → 22/05/2020 07:25; peak lệch 9,2 °C lúc 21:55. | [Đối soát và browser](br_01.md). Coverage là thời gian đủ điều kiện đánh giá; rule là demo, không phải chuẩn nông học. |
| 02 | Automatoes, 02–03/03/2020: Irr **9,36**, Drain **3,89 L/m²**; ratio **41,56%**, paired coverage **100%**. | [Oracle CSV độc lập](assets/effectiveness/agc-walkthrough-20261106.json); tỷ lệ là tổng drain / tổng tưới trên cùng ngày paired. |
| 03 | Reference, toàn vụ, `drain-demo-b03:Reference:v1`, EC: dài nhất **80.400 phút**, 31/12/2019 12:35 → 25/02/2020 08:35; peak lệch 4,5 dS/m lúc 19/01/2020 19:10. | [Raw oracle và chất lượng](br_03.md) dùng cùng profile. Đợt gốc và giao cửa sổ khác nhau khi cửa sổ bị cắt. |
| 04 | Automatoes, 02–03/03/2020: **308,078 kWh**, **0 kg A**, tỷ số **NULL**. | [Oracle nguồn](assets/effectiveness/agc-walkthrough-20261106.json); không chia cho 0; 62,5 m² là giả định chưa xác minh; portfolio toàn vụ là context khác. |
| 05 | Digilog, 19/02/2020: A **0,05**, B **0,10 kg/m²**, share B **66,67%**; rule `harvest-demo-b10:Digilog:v2` demo 10% → ngoài dải. | [Đối soát record raw](br_05.md); dữ liệu record không phải số trung bình ngày hay ngưỡng đã xác minh. |
| 06 | Mở `Benchmark Scenario Hybrid`, version `scenario:bench-scen-h-ab2ee38e:v1`. Scenario tự pin Automatoes toàn vụ `[2019-12-16, 2020-05-30)`; baseline **1.161,25 EUR**, scenario **1.175,15 EUR**, tăng **13,90 EUR** sau làm tròn. Giá nhiệt tăng từ **0,0083** lên **0,0095 EUR/MJ**, điện giữ nguyên. | [Benchmark](performance.md#correctness-trước-khi-tính-hiệu-năng): giữ lượng, so cùng publication/context; 62,5 m² là giả định, giá là phân tích lịch sử, không phải hóa đơn thật. |

Các đáp án 01 và 03 đã đối chiếu thêm với service trên publication benchmark ở
chế độ chỉ đọc; đáp án 02/04 đối chiếu trực tiếp CSV raw, đáp án 05 có raw
reconciliation và đáp án 06 được kiểm trên UI chỉ đọc. Đây là **đáp án chuẩn bị
trước**, chưa phải kết quả quan sát người dùng.

Chấm từng đáp án và phần giải thích đúng/sai/chưa đánh giá theo bằng chứng;
không dùng thời gian AppTest làm thời gian người tham gia. Ghi nguyên phản hồi,
trợ giúp và bất kỳ bối cảnh bị lệch; nếu lệch context, không chấm theo đáp án của
context khác. Sau phiên, tổng hợp kết quả và hạn chế vào [usability](usability.md).
