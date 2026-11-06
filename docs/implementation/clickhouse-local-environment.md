# ClickHouse candidate: chạy local

Candidate có client, ingestion/publication và [luồng nước BR-02](clickhouse-water.md) / [thu hoạch BR-05](clickhouse-harvest.md) / [khí hậu và drain BR-01/03](clickhouse-thresholds.md). [Năng lượng/scenario BR04/06](clickhouse-energy-cost.md) cũng có adapter; sensor và tích hợp toàn bộ màn hình tiếp tục theo T47–T48; PostgreSQL giữ vai trò baseline/mặc định đến T39. Thiết kế ở [ADR-003](../adr/003-clickhouse-warehouse.md).
Không có fallback ngầm khi ClickHouse lỗi. Server và client dùng Compose riêng
để không khởi động/sửa database người dùng trong giai đoạn chuyển đổi.

## Chuẩn bị

Host chỉ cần Docker/Compose và shell. Copy `.env.example` thành `.env` nếu chưa
có; giữ file cá nhân hiện hữu. Chỉnh CH_PASSWORD cho môi trường local. Ví dụ:

```bash
docker compose -f compose.clickhouse.yaml pull clickhouse
docker compose -f compose.clickhouse.yaml build ch-client
```

Server pin image index digest (cả arm64/amd64). Client HTTP synchronous dùng
standard library của CPython **3.12.12**, base image pin digest; không thêm driver
pip hoặc JDBC chưa cần thiết, không tải thư viện ở runtime. Client tests có
unittest trong image. arm64 được chạy thật; manifest có amd64, chưa tuyên bố
đã chạy amd64. Full version thực tế được lấy bằng `SELECT version()` trong smoke.

## Smoke tự động trên project mới

```bash
./scripts/test_clickhouse_stack.sh
```

Wrapper gọi Compose, tạo project/volumes mới, kiểm kết nối, tham số SQL, Decimal,
unit tests, restart giữ dữ liệu và egress bị chặn. Backend internal,
image đã chuẩn bị trước, `--pull never`. Khi pass chỉ dọn tài nguyên thuộc chính
lượt smoke; khi fail giữ lại cho chẩn đoán. Evidence tại `tmp/clickhouse-smoke/`.
Chưa phải test full warehouse, workload performance hoặc all fault scenarios.

## Candidate giữ lại để phát triển

```bash
docker compose -f compose.clickhouse.yaml -p greenhouse-ch-dev up -d --wait --no-build --pull never clickhouse
docker compose -f compose.clickhouse.yaml -p greenhouse-ch-dev run --rm --no-deps --pull never ch-client
docker compose -f compose.clickhouse.yaml -p greenhouse-ch-dev run --rm --no-deps --pull never ch-client python -m unittest discover -s tests/clickhouse -v
docker compose -f compose.clickhouse.yaml -p greenhouse-ch-dev stop clickhouse
```

Warehouse không publish HTTP trực tiếp. Dashboard publish `127.0.0.1:18581` (GREENHOUSE_CH_APP_PORT) qua `ch-web`, relay TCP đích cố định `ch-app:8501`; app và warehouse chỉ ở backend internal. Relay là thành phần truy cập localhost, không là forward proxy. Default
server cap 1 CPU / 2 GiB, query max 2 threads / 768 MiB, server budget 1.5 GiB;
đây là cấu hình nền, benchmark sẽ công bố riêng. Lưu data/logs trong named volume.
Không dùng chung tên project/volume với ứng dụng PostgreSQL hoặc một lượt restore.
Client đọc CH_HOST/PORT/DATABASE/USER/PASSWORD/TIMEOUT, không yêu cầu PG env.
Client không tự retry request thất bại vì write outcome có thể chưa biết;
coordinator sẽ reconcile theo query ID trước retry ở T42.
