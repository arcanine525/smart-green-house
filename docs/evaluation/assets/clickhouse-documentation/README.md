# Kiểm tài liệu ClickHouse — T52

Clone sạch source82c5a98, overlay **chỉ docs ứng viên**; project DB/volume mới,
raw/import rỗng, migrate và CLI từ image đã chuẩn bị. UI Chromium hiển thị chưa
có publication, không fixture giả; PG environment/service không có, internal
egress bị chặn.26,012s. Đã dừng fixture riêng; không xóa volume/người dùng.
Không chạy lại normal Docker build hoặc full AGC trong phép smoke này;
full clone/offline/rebuild nằm ở [T50](../../clickhouse-local-repro.md).

`clone-check.py` là snapshot helper đã chạy, image IDs/source SHA ởresult.json;
đường dẫn local trong helper là môi trường diễn tập, không yêu cầu máy người đọc.
`links.json` kiểm local targets của docs thay đổi tại thời điểm chạy;
`mermaid.json` kiểm13 diagrams bằng MermaidCLI. Report/slidePDF xuất được,
text selectable và visual review; bản cuối/metadata/gói chuyển T29–T30.
Không lưu .env, Dockerconfig hoặc raw vào evidence.
