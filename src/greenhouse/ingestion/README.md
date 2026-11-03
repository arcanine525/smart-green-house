# Ingestion

T05 hiện thực snapshot/checksum/manifest và inventory trong `snapshot.py`/`inventory.py`; fixture verifier không sinh dữ liệu mới. T06 sẽ hiện thực parser typed và DB ingestion. CLI `ingest` vẫn thoát mã 2; dùng `snapshot`, `inventory`, `verify-fixtures` cho phần nguồn đã hoàn thành. Không chuẩn hóa quality/KPI ở bước snapshot.
