# Đánh giá khai thác sáu BR

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

## Protocol đánh giá người dùng cần thực hiện

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

Mẫu ghi một dòng: `participant | BR | context/version | thời gian | đáp án |
đúng/sai | coverage & giới hạn | trợ giúp | phản hồi`. Hiện chưa có dòng quan
sát người thật; không điền giả định hoặc tổng hợp từ test tự động.
