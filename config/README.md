# Cấu hình local

Copy `.env.example` thành `.env` tại root trước khi gọi Compose. Biến bắt buộc `GREENHOUSE_DB_PASSWORD` được Compose kiểm tra; giá trị mẫu chỉ phục vụ local. Settings trong container đọc các biến `PG*` và `GREENHOUSE_*_DIR`. Import/raw/preview host paths cấu hình qua `GREENHOUSE_*_PATH`, không đưa credential vào Git/image.

Rule, quality policy, basis và tariff có version thuộc T03/T07/T14/T20, chưa được dựng bằng scaffold.
