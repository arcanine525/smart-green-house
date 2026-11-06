# Môi trường local Docker-first

Scaffold T04 và snapshot T05 mở rộng trực tiếp Compose/Dockerfile của [ADR-002](../adr/002-container-stack-compatibility.md). Host chỉ cần Docker Engine + Compose v2 trở lên. Không có Python/Java/Spark trên host trong các lệnh runtime. Snapshot/inventory, database pipeline, simulator, collector và stream replay đều có lệnh local; live replay view đã có; nghiệm thu end-to-end và fault matrix tiếp tục ở T36.

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
| app | Base: batch, sensor | UI / [ứng dụng nước](../../src/greenhouse/app/README.md) (Streamlit) | `127.0.0.1:8501` mặc định | Raw host → `/data/raw:ro` chỉ để xác minh lineage; theme nằm trong image |
| job | Base: batch, sensor | Processing / `greenhouse setup` mặc định | Không | Import host → `/data/import:ro`; raw host → `/data/raw:ro`; `outputs` → `/data/output` |
| broker | sensor, test | Mosquitto 2.0.22 | Không | Config read-only; `broker_state` → `/mosquitto/data` |
| simulator | sensor | Sensor / `greenhouse simulate` (stub) | Không | Raw host read-only; `simulator_outbox` → `/data/outbox` |
| collector | sensor | Sensor / `greenhouse collect` (stub) | Không | `collector_spool` → `/data/spool`; `raw_ready` → `/data/ready` |
| stream-worker | sensor | Processing / `greenhouse stream` (stub) | Không | `raw_ready` read-only; `spark_checkpoints`; raw host read-only; `outputs` |
| test-runner | test | Processing / `greenhouse test -q -s` | Không | Không mount dữ liệu host; fixture synthetic đóng gói trong image |
| ui-test | test | UI / `pytest tests/ui` (AppTest + service/export) | Không | Không mount dữ liệu host; database fixture `test_ui_*` tự drop; dùng budget `GREENHOUSE_UI_CPUS`/`GREENHOUSE_UI_MEMORY` (mặc định như app) |
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

## CLI và phạm vi hiện tại

Một entrypoint: `python -m greenhouse <command>` bên trong container. Ví dụ:

```bash
docker compose --profile batch run --rm job python -m greenhouse setup
docker compose -p smart-greenhouse run --rm job python -m greenhouse ingest
docker compose --profile batch run --rm job python -m greenhouse transform
docker compose --profile test run --rm test-runner
```

| Command | Hành vi hiện tại | Task tiếp theo |
|---|---|---|
| setup | Kiểm config, input mounts đọc được, output ghi được, DB reachable; exit 0 khi đạt | Core qua migrate/load-core; workflow qua run-pipeline |
| test | Gọi pytest trong image processing, trả đúng exit code | Bổ sung kiểm chứng nghiệp vụ theo từng BR |
| preview | Gọi builder prototype trong image UI, output rõ đường dẫn | Chỉ export fixture demo, không phải app nghiệp vụ |
| ingest | Parser/adapter Python, staging JSONL gzip và manifest theo run trong outputs; [contract và lệnh](../../src/greenhouse/ingestion/README.md) | run-pipeline publication/recovery; T31 PostgreSQL adapter |
| migrate | SQL migrations có version/checksum; rerun skip, checksum thay đổi fail | [Core/metadata contract](../../src/greenhouse/db/README.md) |
| load-core | Một run T06 → candidate core/meta/config; không publish | Dùng run-pipeline cho publication/recovery |
| explain-core | Diagnostic value/unit/quality/basis/lineage; không phải app API | T31 query contracts |
| run-pipeline / publish | Bốn step có trạng thái, resume, local Spark staging/profile, một transaction publish; [contract](../../src/greenhouse/db/README.md#workflow-chạy-lại-và-publication) | Marts/KPI T09+ |
| lineage / correct-core | Kiểm hash từ revision về raw / correction có audit và revision mới | Không sửa raw/time hoặc tự mở rộng basis |
| transform | Alias run-pipeline: cả publish transaction và water mart hook mặc định | UI T10 / nghiệm thu BR-02 T11 |
| simulate | Replay snapshot vào outbox bền vững, có fault injection tùy chọn | Xem [sensor](../../src/greenhouse/sensor/README.md) |
| collect | Nhận MQTT và ghi spool/raw-ready bền vững | Xem [sensor](../../src/greenhouse/sensor/README.md) |
| stream | Đọc raw-ready bằng Spark, ghi PostgreSQL và publication replay | Cần scope và checkpoint tường minh; [hướng dẫn](../../src/greenhouse/sensor/README.md) |

Profile sensor có simulator, collector và stream worker với volumes tách biệt. Durable spool/ACK và stream retry có kiểm thử riêng; fault matrix và đối soát batch–streaming đầy đủ vẫn thuộc T36.

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

Bảng trên là bằng chứng scaffold gốc, không phải trạng thái nghiệm thu BR. Trạng thái parser/snapshot và candidate core hiện hành xem các contract ingestion/core bên dưới. Pipeline, marts, sensor durability/streaming và app đã có bằng chứng theo từng phạm vi; backup/restore và runtime amd64 chưa được nghiệm thu. Khả năng mở prototype/đạt synthetic tests không thay nghiệm thu sáu BR.


## Nạp candidate core và giải thích dữ liệu

Sau snapshot và ingest, dùng run_id đã trả về. Schema/basis/quality và các giới hạn của loader được ghi trong [PostgreSQL core contract](../../src/greenhouse/db/README.md).

```bash
docker compose -p smart-greenhouse run --rm job python -m greenhouse migrate
docker compose -p smart-greenhouse run --rm job python -m greenhouse load-core --run /data/output/ingestion/<run_id>
docker compose -p smart-greenhouse run --rm job python -m greenhouse explain-core --compartment AICU --date 2020-01-01 --measure Irr --limit 5
```

`migrate` không tự xóa DB hoặc raw; `load-core` từ chối cùng run và rollback khi lỗi. Candidate core chỉ trở thành public khi coordinator run-pipeline/publish commit pointer. LabAnalysis vẫn raw/staging, core deferred. Không dùng SQL load thành công thay nghiệm thu BR.

## Chạy batch và phục hồi

```bash
docker compose -p smart-greenhouse run --rm job python -m greenhouse migrate
docker compose -p smart-greenhouse run --rm job python -m greenhouse run-pipeline --workflow-id historical-first
docker compose -p smart-greenhouse run --rm job python -m greenhouse run-pipeline --resume historical-first
docker compose -p smart-greenhouse run --rm job python -m greenhouse run-pipeline --workflow-id historical-repeat
```

Outputs ở volume outputs: `pipeline/<workflow_id>/steps.jsonl`, mỗi attempt có artifact manifest/checksums, Spark Parquet và profile JSON/JSONL. Resume kiểm step completed rồi skip; failed step chạy lại với attempt mới. Không xóa pending/raw/staging. DB T07 đã load có thể adopt `--ingestion-run /data/output/ingestion/<run_id>`, không nạp lại core. Snapshot/policy/scope/output phải giữ nguyên khi resume; config drift cần run mới. Same-snapshot không đổi reuse publication, correction được audit và chọn trong publication tiếp theo. Khác snapshot không auto-merge/basis. Resource limits Compose giữ nguyên: job 2 GiB/1.5 CPU, db 512 MiB/0.5 CPU; Spark local[1], driver 768 MiB, shuffle 4. Không restart Docker tự động nếu engine mất; lỗi transaction rollback và resume sau khi engine trở lại. Giữ db/broker chạy cho review.

## Water mart bundle

Chạy migrate (005+) rồi workflow mới: `docker compose -p smart-greenhouse run --rm job python -m greenhouse run-pipeline --workflow-id water-first`. Bundle code `pipeline-v2-irrigation` bảo đảm publication mới có water mart. `transform` là alias cùng flags; rerun không đổi reuse publication và không nhân fact rows. Default adapter từ chối publication cũ chưa có mart, không tự backfill/sửa publication đã seal. Xem [mart/rule config](../../src/greenhouse/db/README.md#mart-nước-và-rule-profiles) và [queries ngày/tuần/vụ](../../src/greenhouse/queries/README.md#day-iso-week-cycle-và-review-rule). Rule profile cần version ID tường minh; không seed threshold AGC. Shared basis vẫn có nhãn giả định chưa xác minh. Đây chưa phải UI hoặc nghiệm thu BR-02.

## Ứng dụng nước

`docker compose -p smart-greenhouse up -d --wait db app` rồi mở `http://127.0.0.1:8501` — [ứng dụng nước production](../../src/greenhouse/app/README.md) đọc publication hiện hành theo scope qua catalog; chọn khoang/cửa sổ, xem KPI/chart/tuần, drill-down ngày + lineage và xuất CSV/JSON. App mount raw read-only để xác minh lineage; thiếu mount chỉ làm lineage "chưa xác minh được", không chặn KPI. DB trống → trạng thái hướng dẫn; publication thiếu mart → lỗi rõ, không fallback. Kiểm thử UI: `docker compose --profile test run --rm ui-test` (AppTest chạy `home.py` thật với database fixture `test_ui_*` tự drop). Nghiệm thu BR-02 end-to-end thuộc task sau; UI không tự publish/migrate.
