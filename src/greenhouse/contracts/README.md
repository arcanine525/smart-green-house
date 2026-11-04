# Contracts

DTO thuần Python trong [models.py](models.py), codec trong [serialization.py](serialization.py). Không import PostgreSQL, Spark hoặc module DB. Không có ORM, adapter thay thế hoặc dependency mới. [Service BR-02](../queries/README.md) dùng các DTO này; [PostgreSQL adapter](../db/query_adapter.py) ánh xạ core đã publish.

| DTO | Contract |
|---|---|
| `PublicationRef`, `Versions` | Publication/scope/snapshot, ingestion run, quality/parser/pipeline code/config hash, metric/rule/tariff/scenario versions, operational commit time và finality. Metric version hiện là oracle `br02-summary-v1`, chưa phải mart bundle. |
| `QueryContext` | Publication ID đã pin, scope/snapshot/cycle, đúng một compartment ID trong tuple, whole-date `[start,end)`, optional explicit `rule_version`. Không nhận datetime hoặc window rỗng/đảo. |
| `CycleWindow` | Cửa sổ source-date của cycle và compartment tương ứng; service cắt request theo cycle. |
| `MeasureValue` | Exact Decimal hoặc NULL, unit/source_unit, quality/applicability/reason, basis ID/kind/area/status, assumption ID/version và nhãn. Không coi accepted + applicability unknown là usable. |
| `IrrigationDay` | Một `(cycle_id, resource_date)`, compartment, hai measure độc lập; source revision/logical key/correction version, paired flag, daily ratio và Drain>Irr flag. Missing day không có revision giả. |
| `IrrigationSummary` | Requested context + effective window, expected/accepted/paired/unknown/suspect counts, observed/paired sums, ratio/coverage, status/comparison eligibility, basis/assumption versions, `CorrectionRef` IDs/versions, rule/profile metadata, rule unknown/outside counts, optional ISO year/week và reasons. |
| `IrrigationRuleProfile` | Immutable rule version ID/logical ID/version, scope/compartment, four finite ordered bounds, water/ratio units, evidence và demo label; không có ngưỡng AGC mặc định. |
| `LineageResult` | Pinned publication membership → revision/type/measure units → snapshot/file/physical lines/raw hashes, source serial/naive timestamp, correction identity/version, raw verification flag. Không gồm raw payload. |
| `ScenarioCommand`, `ScenarioChange` | Request ID, baseline version, pinned quantity publication và distinct component changes. Chính xác một giá mới hoặc percent change; price units heat EUR/MJ, electricity EUR/kWh; finite/nonnegative resulting rates. Chỉ validation, chưa ghi scenario. |
| `SinkBatch`, `SinkRecord`, `SinkValue`, `SourceIdentity` | Stable caller request/batch/run/source/event IDs, scope/snapshot, parser version, file/lines/raw hash, exact values/units, source-naive và operational clocks. Chỉ input contract, chưa ghi collector/sink hoặc quyết định dedup/conflict. |

## JSON version 2

`to_json(dto)` trả envelope `{"contract_version":2,"data":...}`. `from_json(DtoClass, text)` yêu cầu class tường minh thuộc module contracts; không đoán type hoặc import class từ payload. Shape v2 thêm rule request/profile/status/counts và ISO week metadata; v1 bị từ chối thay vì nhận thiếu trường. Unknown fields/version, enum lạ hoặc scalar sai type bị từ chối. `to_dict`/`from_dict` dùng cùng quy ước cho data trong envelope. Collections là tuple bất biến trong Python và JSON arrays trên wire.

- Decimal dùng **JSON string** từ `str(Decimal)`: giữ scale/exponent nguồn (`"1.2300"`, `"0.0000"`), không qua float. JSON number không được nhận thay Decimal; NaN/Infinity bị từ chối. NULL là JSON null, khác `"0"`.
- Source dates ISO `YYYY-MM-DD`; source timestamps ISO **không offset**. `observed_at`/`source_timestamp` bắt buộc naive. Published/received/committed times bắt buộc có UTC offset và được giữ offset khi serialize; không tự chuyển timezone nguồn hoặc round microsecond.
- Enums là string ổn định (`accepted`, `missing`, `invalid`, `suspect`, `assumed_unverified`, `not_configured`...). Hash/source/event IDs là strings, không dựa PostgreSQL sequence.
- Source measure values và cộng quantity giữ Decimal exact; derived ratio/coverage dùng 50 significant digits, không làm tròn theo UI hoặc quantize từng ngày. T09 phải khớp oracle trước làm tròn hiển thị; thay metric semantics phải tăng version.

Basis `assumed_unverified` bắt buộc có ID/version và nhãn **“giả định chưa xác minh”**. DTO không tự tạo basis hoặc áp assumption sang snapshot khác. Scenario/sink là ranh giới cho T21/T33; envelope sensor hoàn chỉnh thuộc T32. Không có command persistence/idempotency execution trong task contracts này.
