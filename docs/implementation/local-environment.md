# Môi trường local Docker-first

Scaffold T04 và snapshot T05 mở rộng trực tiếp Compose/Dockerfile của [ADR-002](../adr/002-container-stack-compatibility.md). Host chỉ cần Docker Engine + Compose v2 trở lên. Không có Python/Java/Spark trên host trong các lệnh runtime. Snapshot/inventory AGC đã có lệnh riêng; typed DB pipeline, migrations nghiệp vụ và sensor replay vẫn chưa hiện thực.

## Clone mới → build → setup → test → prototype

Từ root repository:

```bash
cp .env.example .env
docker compose --profile '*' build
docker compose --profile batch run --rm job
docker compose --profile test run --rm test-runner
docker compose --profile batch up -d --wait db app
docker compose exec -T app python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health').read().decode())"
docker compose --profile sensor config --quiet
```

Mở **http://127.0.0.1:8501**. Lệnh setup mặc định của `job` là `python -m greenhouse setup`; chạy lại idempotent, chỉ kiểm tra env/mount/quyền ghi output và `SELECT 1`, chưa tạo schema/core/mart. Có thể dùng `docker compose --profile batch up -d` để dựng cả job setup một lần cùng app/db; dùng `up -d --wait db app` như trên để chỉ đợi các dịch vụ sống lâu có healthcheck.

`db`, `app`, `job` là nền batch luôn được bật: `docker compose build` build processing/UI. `--profile '*' build` bổ sung image sensor, không khởi chạy stub. Profile sensor thêm các dịch vụ vào nền batch; khi chỉ chạy test bằng `run test-runner`, Compose chỉ dựng dependencies của test, không tự khởi chạy app/job. Lần build đầu cần Internet; dependencies/JDBC đều đóng gói sẵn. Package `greenhouse` được cài trong từng image từ [pyproject.toml](../../pyproject.toml), không dựa vào bind mount source hoặc pip install khi chạy.

## Service, profile, port và storage

| Service | Profile | Image / command | Port host | Volumes / mount |
|---|---|---|---|---|
| db | Luôn bật khi cần | PostgreSQL 17.10 | Không | `pg_data` → `/var/lib/postgresql/data` |
| app | Base: batch, sensor | UI / prototype Streamlit | `127.0.0.1:8501` mặc định | Không mount source/dataset; theme và prototype nằm trong image |
| job | Base: batch, sensor | Processing / `greenhouse setup` mặc định | Không | Import host → `/data/import:ro`; raw host → `/data/raw:ro`; `outputs` → `/data/output` |
| broker | sensor, test | Mosquitto 2.0.22 | Không | Config read-only; `broker_state` → `/mosquitto/data` |
| simulator | sensor | Sensor / `greenhouse simulate` (stub) | Không | Raw host read-only; `simulator_outbox` → `/data/outbox` |
| collector | sensor | Sensor / `greenhouse collect` (stub) | Không | `collector_spool` → `/data/spool`; `raw_ready` → `/data/ready` |
| stream-worker | sensor | Processing / `greenhouse stream` (stub) | Không | `raw_ready` read-only; `spark_checkpoints`; raw host read-only; `outputs` |
| test-runner | test | Processing / `greenhouse test -q -s` | Không | Không mount dữ liệu host; fixture synthetic đóng gói trong image |
| snapshot | tools | Processing / `greenhouse snapshot` | Không | Import host RO; raw host RW; outputs |
| dataset-download | tools | Processing / `greenhouse snapshot --download` | Không | Raw host RW; network download setup-only có Internet |
| preview-build | tools | UI / `greenhouse preview --output /preview/preview.html` | Không | Output export host → `/preview` |

`backend` là network `internal: true`: DB/broker/job/test/sensor không có route Internet ở runtime. Chỉ app tham gia thêm network `frontend` để publish port; không có dependency tải lúc app chạy. Healthcheck: `pg_isready`, MQTT publish QoS 1, HTTP `/_stcore/health`. Chờ `service_healthy` không thay thế retry/durable ACK/recovery ở các task sau. Tham khảo [Compose profiles](https://docs.docker.com/reference/compose-file/profiles/), [service/network/mount/healthcheck](https://docs.docker.com/reference/compose-file/services/) và [Streamlit Docker](https://docs.streamlit.io/deploy/tutorials/docker).

DB và broker không publish port debug mặc định. Đổi app port bằng `GREENHOUSE_APP_PORT` trong `.env`. Các containers xử lý/UI/sensor chạy UID 10001; chỉ service export HTML dùng root để ghi được thư mục host mới trên Linux. Không dùng privileged mode hoặc Docker socket trong container.

## Credentials và mount

[.env.example](../../.env.example) chỉ có giá trị mẫu local; copy thành `.env` bị Git ignore, thay password khi cần. Compose dùng `${GREENHOUSE_DB_PASSWORD:?...}` và dừng rõ nếu thiếu. CLI không in password/DSN khi lỗi; dataclass settings không đưa password vào repr. Khi thay password sau khi PostgreSQL đã khởi tạo volume, cần đổi password trong DB hoặc chủ đích dựng lại volume; env mới không tự sửa user đã tồn tại.

- `GREENHOUSE_IMPORT_PATH` mặc định `./data/import`, có thể trỏ tới **thư mục** chứa ZIP AGC có sẵn trên host. Mount read-only; không cần archive để kiểm tra scaffold. Logic ZIP/checksum/manifest đã có ở [snapshot T05](dataset-snapshot.md).
- `GREENHOUSE_RAW_PATH` mặc định `./data/raw`; xử lý và simulator đọc read-only. Scaffold không ghi nguồn. Service snapshot/dataset-download T05 là writer được mount raw read-write; job/stream giữ read-only.
- Output xử lý tách khỏi nguồn trong named volume `outputs`; image seed quyền ghi cho UID 10001. Setup chỉ tạo file tạm để kiểm quyền rồi đóng/xóa ngay, không sửa CSV/ZIP/raw. Docker tạo các thư mục bind mount còn thiếu trên host khi dựng container.
- `GREENHOUSE_PREVIEW_PATH` mặc định `./data/output/preview`; output HTML nằm trong vùng bị ignore. Nếu muốn xuất ra nơi khác, đổi biến này; đường dẫn host cá nhân chỉ nằm trong `.env`, không hard-code trong source.

Không đưa `data/`, `.env`, backlog, scratch hoặc venv vào build context. [.dockerignore](../../.dockerignore) dùng allowlist; [.gitignore](../../.gitignore) loại dataset/output/checkpoints/cache, vẫn giữ [fixtures synthetic](../../tests/fixtures/). Default commands không tạo thay đổi Git ngoài các đường dẫn bị ignore.

## Snapshot dataset

[Hướng dẫn snapshot T05](dataset-snapshot.md) có nhánh ZIP local/download, manifest bất biến, lệnh tái tạo [inventory](data-inventory.md) và fixture × AC. Service snapshot chỉ dùng backend nội bộ; service dataset-download dùng network egress riêng và chỉ mount raw, không mang DB credentials. Cả hai kiểm fixture synthetic trước khi công bố; không nạp DB hoặc xác minh ngữ nghĩa T03.

Bộ test hiện hành sau T05 đạt 47 case; số 23 trong bảng bằng chứng scaffold T04 bên dưới là kết quả lịch sử trước phần snapshot.

## Images và phiên bản

Giữ nguyên baseline T02 ở ADR-002: Python 3.12.12, Temurin JRE 17.0.17+10, PySpark 4.0.1, PostgreSQL 17.10, pgJDBC 42.7.14, psycopg 3.3.7, Mosquitto 2.0.22, paho-mqtt 2.1.0, pytest 8.4.2, Streamlit 1.66.1, Plotly 7.1.0. Base images pin tag + manifest digest; không ép amd64 trên Apple Silicon.

| Image | Dockerfile / lock | Lý do dùng chung |
|---|---|---|
| `smart-greenhouse-processing:local` | [Processing Dockerfile](../../docker/processing/Dockerfile), [requirements.lock](../../requirements.lock) | Job/stream-worker/test-runner dùng cùng JRE, PySpark và JDBC đã kiểm SHA-256 lúc build; không tải Maven/Ivy khi chạy |
| `smart-greenhouse-ui:local` | [UI Dockerfile](../../docker/ui/Dockerfile), [requirements-ui.lock](../../requirements-ui.lock) | App/prototype và export HTML dùng cùng Streamlit/Plotly; khóa đủ 35 dependencies Linux/CPython 3.12, `pip check` khi build |
| `smart-greenhouse-sensor:local` | [Sensor Dockerfile](../../docker/sensor/Dockerfile), [requirements-sensor.lock](../../requirements-sensor.lock) | Simulator/collector dùng chung Python + MQTT client, không cần mang JVM/PySpark vào từng container; commands hiện là stub |

Build tools cũng pin pip 25.2, setuptools 80.9.0, wheel 0.45.1. Khóa phiên bản không phải bit-for-bit reproducibility; lock chưa pin hash mọi Python artifact và apt phụ thuộc repository. Dependency closure UI được resolve trong container Linux arm64 rồi pin; chưa kiểm chứng runtime amd64.

## CLI và ranh giới chưa hiện thực

Một entrypoint: `python -m greenhouse <command>` bên trong container. Ví dụ:

```bash
docker compose --profile batch run --rm job python -m greenhouse setup
docker compose --profile batch run --rm job python -m greenhouse ingest
docker compose --profile batch run --rm job python -m greenhouse transform
docker compose --profile test run --rm test-runner
```

| Command | Hành vi hiện tại | Task tiếp theo |
|---|---|---|
| setup | Kiểm config, input mounts đọc được, output ghi được, DB reachable; exit 0 khi đạt | Migration thật T06–T08 sẽ gắn sau |
| test | Gọi pytest trong image processing, trả đúng exit code | Bổ sung kiểm chứng nghiệp vụ theo từng BR |
| preview | Gọi builder prototype trong image UI, output rõ đường dẫn | Chỉ export fixture demo, không phải app nghiệp vụ |
| ingest | Exit 2, “chưa hiện thực” | T06 typed parser/DB ingestion |
| transform | Exit 2, “chưa hiện thực” | T07–T09 core/publication/marts |
| simulate | Exit 2, “chưa hiện thực” | T32 |
| collect | Exit 2, “chưa hiện thực” | T33 |
| stream | Exit 2, “chưa hiện thực” | T34 |

Profile sensor có cấu hình hợp lệ nhưng không có simulator/collector/streaming hoạt động. Các volumes đã được phân tách, chưa chứng minh durable ACK, recovery hoặc batch–stream reconciliation. Không dùng container stub đã exit làm minh chứng tính năng hoàn tất.

## Tạo HTML và dừng dịch vụ

```bash
docker compose --profile tools run --rm preview-build
```

File mặc định: `data/output/preview/preview.html`. Khi chủ đích cập nhật artifact tracked, dùng `GREENHOUSE_PREVIEW_PATH=./prototypes/dashboard docker compose --profile tools run --rm preview-build`; không cần sửa file Python để đổi nơi xuất. Service chỉ bind output; source trong image không bị che bằng host venv/cache.

`docker compose --profile '*' down` giữ dữ liệu named volumes. `docker compose --profile '*' down -v` xóa named volumes **của project smart-greenhouse**, bao gồm DB, outputs, broker/outbox/spool/raw-ready/checkpoints; không xóa import/raw/preview bind mounts hoặc image của project khác. Đây không phải lệnh backup/restore an toàn cho pipeline thật; hướng vận hành đó sẽ được kiểm chứng ở T28.

## Bằng chứng scaffold

Kiểm chứng ngày 2026-11-04 trên Docker Linux arm64, VM 4 CPU/~7.62 GiB. Runtime kiểm tra ở checkout làm việc dùng input/export scratch mounts ngoài repository để giữ nguyên `data/` của người dùng.

| Kiểm tra | Kết quả |
|---|---|
| Build toàn bộ profiles và plain `docker compose build` | Thành công; ba image có dependencies khóa và `pip check` đạt; plain build dựng processing/UI |
| `down -v` rồi `run --rm job` hai lần | Cả hai lần `Setup OK`; mount input chỉ đọc, output ghi được, DB reachable; không migration/ingest |
| `--profile test run --rm test-runner` | **23 passed in 5.57s**: bảy stack checks kế thừa T02, CLI stub failures, invalid config/port, password redaction, setup lỗi và idempotency trên DB thật |
| Prototype health | App healthy; cả HTTP trong container và `http://127.0.0.1:8501/_stcore/health` trên host trả `ok` |
| Prototype rendering | Streamlit AppTest trong image UI: không exception, hai Plotly charts được render |
| HTML export | `preview-build` tạo file ~6.7 MiB tại output mount, không sửa source/preview tracked |
| Sensor containers | simulator/collector/stream-worker chạy `--no-deps` đều thoát **2** và ghi rõ task sở hữu; không giả vờ đã triển khai |
| Cấu hình | Profile sensor hợp lệ; backend internal, processing/test/sensor chỉ backend; chỉ app publish loopback; import/raw read-only; thiếu password bị Compose từ chối |
| Test container RAM | `memory.peak` 495,677,440 byte (~472.71 MiB), limit 2 GiB; chỉ là số đo fixture, chưa phải sizing AGC |

Không tạo báo cáo nghiệm thu BR bởi scaffold. Chưa kiểm chứng AGC parser/snapshot, migrations core/marts, pipeline recovery, sensor durability/streaming, app nghiệp vụ, backup/restore hoặc runtime amd64. Khả năng mở app/đạt synthetic tests không thay nghiệm thu sáu BR.
