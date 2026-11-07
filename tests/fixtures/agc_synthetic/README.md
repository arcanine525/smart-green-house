# AGC-format synthetic fixtures

Tất cả CSV ở đây được dựng bằng tay theo mục 7 của sáu BR; không copy dòng dữ liệu AGC. Header nguồn giữ nguyên (kể cả `%Time `); thư mục Synthetic đóng vai trò khoang. Giá trị trống dùng ô CSV rỗng, thời gian là Excel serial. Nhãn synthetic ở catalog và từng case, không thêm cột ngoài định dạng nguồn.

`catalog.json` chứa 61 case BR-01…BR-06/AC, file liên quan, tham số, oracle literal và checksum. Các thao tác version/export/recovery là dữ kiện yêu cầu cho task sau; T05 chưa chạy các tính năng đó. Area/rule/tariff trong fixture là giả định được BR khai báo; không xác minh basis/timezone/biểu giá AGC (phụ thuộc T03).

`errors/` tách format sai, CSV hỏng, thiếu cột, thiếu giá trị, duplicate và conflict. T06 phải kiểm lỗi, không sửa/xóa raw. Chạy `python -m greenhouse verify-fixtures` trong processing container để xác minh catalog/nhãn/header/hash; không sinh lại ngẫu nhiên. Test inventory/snapshot không dùng ZIP AGC thật.

Mỗi case là một dataset/context độc lập; không cộng hoặc join dữ liệu giữa các case. Metadata version/export/recovery là input điều khiển cho test task sau, không phải event AGC thật.

CSV giữ CRLF như nguồn; `br_03/ac_08/Synthetic/LabAnalysis.csv` có UTF-8 BOM như biến thể nguồn. Catalog ghi variant/hash; `.gitattributes` dùng `-text` để giữ nguyên byte qua mọi checkout (kể cả `core.autocrlf=true`).
