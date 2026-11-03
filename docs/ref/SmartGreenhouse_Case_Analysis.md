# Phân tích case Smart Greenhouse Management System

> **Nguồn phân tích:** `GroupID_DE_Presentation.pdf` (20 slide), môn Data Engineering CO5173, HK1 2026–2027
> **Phạm vi:** tổng quan case · dataset · schema Inmon / Kimball / Hybrid · so sánh · lý do chọn Hybrid
> **Ghi chú:** tài liệu có tóm tắt nội dung slide và nhận xét phản biện. Các chỗ các slide chưa khớp nhau được gom ở mục cuối để sửa trước khi nộp.

---

## Mục lục

1. [Tổng quan case Smart Greenhouse](#1-tổng-quan-case-smart-greenhouse)
2. [Dataset: quy mô, grain và vấn đề dữ liệu](#2-dataset-quy-mô-grain-và-vấn-đề-dữ-liệu)
3. [Schema đại diện Inmon](#3-schema-đại-diện-inmon-lõi-3nf)
4. [Schema đại diện Kimball](#4-schema-đại-diện-kimball-star-factclimate)
5. [Schema đại diện Hybrid](#5-schema-đại-diện-hybrid)
6. [So sánh ba approach](#6-so-sánh-ba-approach-trên-case-này)
7. [Lý do chọn Hybrid và các đánh đổi](#7-lý-do-chọn-hybrid-và-các-đánh-đổi)
8. [Checklist sửa trước khi nộp](#8-checklist-sửa-trước-khi-nộp)

---

## 1. Tổng quan case Smart Greenhouse

### 1.1 Bối cảnh

- **Nhà kính (greenhouse)** có mái và tường trong suốt, cho phép điều tiết khí hậu bên trong để trồng cây quanh năm.
- **Smart greenhouse** gắn thêm cảm biến và bộ điều khiển để đo liên tục nhiệt độ, độ ẩm, ánh sáng, CO2, dinh dưỡng và nước tưới.
- Nhìn từ góc độ data engineering, mỗi nhà kính là **một nguồn phát sinh dữ liệu liên tục** trong suốt cả vụ.

### 1.2 Vấn đề của người trồng

Người trồng gặp năm nhóm khó khăn:

1. Không cung cấp đủ nhiệt, ánh sáng, độ ẩm và CO2 ở mức tối ưu.
2. Bón phân thiếu hoặc thừa so với giai đoạn phát triển của cây.
3. Khí độc tích tụ trong không gian kín.
4. Cây bị nấm, vi khuẩn hoặc virus tấn công.
5. Côn trùng và sâu bệnh phá hoại.

Các vấn đề này vẫn tồn tại vì nhiều lý do:

- Nhiệt độ và độ ẩm khó điều tiết nhất, trong khi chúng ảnh hưởng trực tiếp đến sinh trưởng của cây.
- Phần lớn công việc vẫn làm thủ công. Số liệu nằm rải rác ở nhiều nơi.

Hệ quả là người trồng **không có số liệu để biết mình sai ở đâu, sai bao lâu, và cái sai đó đáng giá bao nhiêu tiền**.

### 1.3 Dữ liệu đã có nhưng chưa trả lời được câu hỏi

Hệ vận hành (OLTP) lưu log theo dạng:

```sql
CREATE TABLE logs (
  pid INT, timestamp DATETIME,
  details JSON   -- {"temp":24.5,"hum":68}
);
```

Cách lưu này có ba hạn chế:

- Mỗi bản ghi là một blob JSON nên không GROUP BY được.
- Không có dimension: không biết dòng dữ liệu thuộc khoang nào, cây gì, vụ nào.
- Chỉ giữ trạng thái hiện tại, không lưu lịch sử.

Trong khi đó, cả ba câu hỏi của người trồng đều là câu hỏi OLAP:

- *Lệch setpoint bao nhiêu giờ?*
- *Tốn bao nhiêu kWh cho mỗi kg quả?*
- *Lô quả này đã trải qua điều kiện khí hậu nào?*

Vì vậy cần một **data warehouse riêng**.

### 1.4 Phạm vi đề tài

| Làm | Không làm |
|---|---|
| Thu thập dữ liệu nhà kính công khai, làm sạch, mô hình hoá thành DW | Điều khiển actuator, phần cứng, firmware |
| Thiết kế trên cùng dataset theo Inmon, Kimball và Hybrid, rồi so sánh | App di động, hệ thống cảnh báo |
| Trả lời 6 business requirement (BR) bằng SQL | Mô hình dự báo năng suất, nhận diện sâu bệnh |

### 1.5 Người dùng

| Người dùng | Câu hỏi điển hình | Kiểu truy vấn |
|---|---|---|
| Người trồng / vận hành | Khoang nào lệch chuẩn, lệch bao nhiêu giờ, tuần nào bất thường | Phân tích |
| Kỹ sư nông học | Đối chiếu khí hậu từng vụ với năng suất và chất lượng quả | Phân tích |
| Quản lý trang trại | Chi phí nước và điện quy về mỗi kg sản phẩm bán ra | Phân tích |
| Bộ phận traceability | Từ một lô quả, mở ngược về khí hậu và chế độ tưới của cả vụ | **Truy xuất** |

**Nhận xét:** Case được đóng khung tốt: đây là bài toán **tổ chức dữ liệu**, không phải tự động hoá. Hai kiểu truy vấn (phân tích và truy xuất) là nền tảng lập luận cho Hybrid ở mục 7.

---

## 2. Dataset: quy mô, grain và vấn đề dữ liệu

### 2.1 Nguồn dữ liệu

| Thuộc tính | Nguồn chính: AGC 2nd Edition (2019) | Nguồn bổ sung: Open-Meteo |
|---|---|---|
| Đơn vị / nền tảng | Wageningen University & Research, 4TU.ResearchData | Open-Meteo Historical Weather API (ERA5 reanalysis) |
| License | CC0 1.0 | CC BY 4.0, không cần API key |
| Địa điểm | Bleiswijk, Hà Lan (52.032 N, 4.531 E) | Có dữ liệu cho mọi toạ độ, từ năm 1940 |
| Nội dung | Cà chua bi cv. Axiany, 6 khoang (5 đội AI và 1 người trồng đối chứng) | Thời tiết theo giờ |
| Vai trò | Dữ liệu cốt lõi | (1) Đối chiếu với Weather.csv của AGC để kiểm tra data quality; (2) Tạo site Đà Lạt làm kịch bản Việt Nam |

Gói AGC gồm các file: `GreenhouseClimate`, `Weather`, `Resources`, `Production`, `TomQuality`, `LabAnalysis`, `GrodanSens`, `CropParameters`.

### 2.2 Quy mô

| Chỉ số | Giá trị |
|---|---|
| Số mốc 5 phút mỗi khoang | ~47.809 |
| Khoảng thời gian | 47.809 × 5 phút ≈ **166 ngày** (16/12/2019 đến khoảng cuối 05/2020), khớp với "một vụ khoảng 6 tháng" |
| Số cột | ~50 |
| Tổng điểm đo (6 khoang) | 47.809 × 50 × 6 ≈ **14,3 triệu** |
| FactClimate (star) | 47.809 × 6 ≈ **287 nghìn dòng** |
| SENSOR_READING (3NF long) | ≈ 14 triệu dòng (trừ phần setpoint/actuator tách sang ACTUATOR_STATE) |

> Sau khi mô hình hoá, dữ liệu vẫn rất nhỏ (cỡ vài chục MB). Điều này ảnh hưởng tới cách lập luận về công nghệ và benchmark ở mục 7.

### 2.3 Grain: ba mức cùng tồn tại

| Grain | Dữ liệu |
|---|---|
| **5 phút** | Khí hậu (Tair, Rhair, CO2air, Tot_PAR), actuator và setpoint, EC/pH vùng rễ, thời tiết tại chỗ |
| **Giờ** | Open-Meteo (temperature_2m, relative_humidity_2m, shortwave_radiation, et0) |
| **Ngày / ngày thu hoạch** | Năng lượng (Heating_Energy, Electricity_Lamp), sản lượng (class_a_kg, class_b_kg), mẫu lab |

Harvest là **fact thưa**: không phải ngày nào cũng có thu hoạch.

### 2.4 Các cột quan trọng

| Cột | Đơn vị | Ý nghĩa | BR |
|---|---|---|---|
| `time` | — | Excel serial (43815 ≈ 2019-12-16) | mọi BR |
| `Tair` | °C | Nhiệt độ không khí trong khoang | 01, 05, 06 |
| `Rhair` | % | Độ ẩm tương đối | 01, 06 |
| `CO2air` | ppm | Nồng độ CO2 | 01 |
| `HumDef` | g/m³ | Humidity deficit (tương đương VPD) | 06 |
| `Tot_PAR` / `Tot_PAR_Lamps` | µmol/m²/s | Ánh sáng quang hợp: tổng / phần do đèn | 04, 05 |
| `t_heat_sp` | °C | Setpoint sưởi | 01 |
| `EC_drain_PC` / `pH_drain_PC` | dS/m / — | Độ đậm đặc dinh dưỡng và pH nước rỉ | 03 |
| `water_sup` / `Cum_irr` | L/m² | Nước tưới trong khoảng đo / lũy kế từ đầu vụ | 02 |
| `Tout, Rhout, Iglob, Rain` | °C, %, W/m², mm | Thời tiết ngoài trời | 02 |
| `class_a_kg` / `class_b_kg` | kg | Sản lượng loại A / loại B | 04, 05 |
| `Heating_Energy` / `Electricity_Lamp` | kWh | Năng lượng sưởi và chiếu sáng | 04 |

ETL tính thêm hai cột phái sinh: `vpd_kpa` (từ Tair và Rhair) và `temp_off_sp = Tair − t_heat_sp`.

### 2.5 Vấn đề dữ liệu chính

| # | Vấn đề | Tác động | Cách xử lý đề xuất |
|---|---|---|---|
| 1 | **Thời gian dạng Excel serial** | Không dùng trực tiếp được | Đổi sang timestamp (UTC và local) |
| 2 | **Giờ mùa hè (DST)**: Hà Lan chuyển giờ ngày 29/03/2020 | Nếu dữ liệu ghi theo giờ local thì sẽ thiếu hoặc trùng slot 5 phút | Kiểm tra quanh mốc này, chuẩn hoá về UTC |
| 3 | **Bảng wide ~50 cột** trộn measurement, setpoint và trạng thái actuator | Khó tích hợp; mỗi sensor mới lại thêm một cột | Unpivot, tách thành SENSOR_READING và ACTUATOR_STATE |
| 4 | **Grain lệch nhau** (5 phút, giờ, ngày, fact thưa) | Không được đặt kg thu hoạch vào fact 5 phút | Mỗi fact một grain; so sánh bằng drill-across |
| 5 | **Cột lũy kế** `Cum_irr` | SUM trực tiếp sẽ sai | Tính delta; xử lý chỗ reset |
| 6 | **Measure non-additive** (Tair, Rhair, CO2) | Không SUM theo thời gian được | Dùng AVG/MIN/MAX; tổng qua các khoang phải quy đổi theo `area_m2` |
| 7 | **Metadata ẩn**: khoang và đội chỉ có trong tên thư mục .7z | Mất ngữ cảnh nếu không trích xuất | Gắn compartment và team ở bước staging |
| 8 | **Hai nguồn thời tiết**: AGC (5 phút, đo tại chỗ) và ERA5 (giờ, reanalysis) | Khác grain và độ tin cậy | Giữ song song theo cột `source`, không trộn |
| 9 | **Missing values** | Sai số khi tổng hợp | `dq_null_flag`; dbt test `not_null` / `accepted_range` |
| 10 | **Khác bối cảnh**: nhà kính kính mùa đông ở Hà Lan, không phải nhà màng ở Việt Nam | Hạn chế khi suy rộng kết quả | Giữ DimSite trung thực. Site Đà Lạt chỉ có dữ liệu thời tiết, không có khoang |

---

## 3. Schema đại diện Inmon (lõi 3NF)

### 3.1 Cấu trúc

```
                 SITE ──1:N── WEATHER_OBS (site_id, ts, source)  ← AGC | OPEN_METEO
                  │
             COMPARTMENT ──── SENSOR ──── SENSOR_READING (sensor_id, ts) value_num, quality_flag
                  │                        ACTUATOR_STATE (actuator_id, ts) value, setpoint
   CROP ──────────┤
   TEAM ──────────┤
                  ▼
           GROWING_CYCLE  (cycle_id, compartment_id, crop_id, team_id, start_ts, end_ts)   ← HUB
                  │
   ┌──────────────┼──────────────────┬──────────────┐
IRRIGATION_EVENT  RESOURCE_CONSUMPTION  HARVEST      LAB_SAMPLE
(cycle_id, ts,    (cycle_id, date, kWh) (cycle_id,   (cycle_id, ec, ph, N, P, K)
 water_sup)                             harvest_date)
```

| Nhóm | Bảng | Ghi chú |
|---|---|---|
| Master entity | SITE, COMPARTMENT, CROP, TEAM, SENSOR | Khoá tự nhiên, không có surrogate key |
| Hub | **GROWING_CYCLE** | Tưới, năng lượng, thu hoạch và lab đều treo vào vụ trồng |
| Time-series | SENSOR_READING, ACTUATOR_STATE | Dạng long-narrow |
| Thời tiết | WEATHER_OBS | PK `(site_id, ts, source)`, giữ song song nhiều nguồn |

### 3.2 Đặc điểm

- Không có fact table ở trung tâm, không có surrogate key, không có dimension denormalized.
- Traceability (lô quả → vụ → khí hậu) chỉ là vài phép JOIN theo foreign key.

### 3.3 Điểm mạnh

- Thêm sensor mới chỉ là thêm **dòng**, không phải đổi schema.
- Mô hình không dư thừa dữ liệu, giữ lineage và lịch sử (atomic, non-volatile).
- Tích hợp nhiều nguồn tốt, ví dụ WEATHER_OBS theo `source`.

### 3.4 Điểm yếu

- SENSOR_READING thực chất là kiểu **EAV**. Mọi câu phân tích đều phải pivot lại, và chạy chậm hơn.
- Người dùng nghiệp vụ khó đọc schema.

### 3.5 Cần bổ sung hoặc sửa

1. ERD **thiếu entity ACTUATOR**. ACTUATOR_STATE có `actuator_id` nhưng không trỏ về bảng nào.
2. SENSOR_READING không có FK tới GROWING_CYCLE. Nối reading với vụ phải dùng range join `ts BETWEEN start_ts AND end_ts`. Nên ghi rõ, hoặc thêm `compartment_id` qua SENSOR.
3. Câu SQL Inmon ở slide 19 có ba lỗi:
   - Join `ACTUATOR_STATE ON a.ts = r.ts` mà **không có điều kiện compartment**, nên setpoint của 6 khoang bị trộn với nhau.
   - Dùng `a.actuator_type`, nhưng ACTUATOR_STATE không có cột này; phải join thêm bảng ACTUATOR.
   - Slide ghi "4 bảng" nhưng câu query chỉ dùng 3.

---

## 4. Schema đại diện Kimball (star FactClimate)

### 4.1 Cấu trúc

```
            DimDate        DimTime (288 slot/ngày)       DimSite
                 \               |                      /
                  \              |                     /
                   ┌──────────── FactClimate ──────────┐
                   │ GRAIN: 1 dòng / khoang / 5 phút    │
                   │ FK: date_key, time_key, site_key,  │
                   │     compartment_key, crop_key,     │
                   │     team_key                       │
                   │ M : tair_c, rhair_pct, co2_ppm,    │
                   │     tot_par, tot_par_lamps, humdef,│
                   │     vpd_kpa*, temp_off_sp*,        │
                   │     t_heat_sp, co2_sp, dq_null_flag│
                   └────────────────────────────────────┘
                  /              |                     \
       DimCompartment         DimCrop                 DimTeam
```

### 4.2 Bus matrix

| Business process | Fact | Grain | Date | Time | Site | Compartment | Crop | Team | Cycle |
|---|---|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| Giám sát khí hậu | FactClimate | khoang × 5 phút | ● | ● | ● | ● | ● | ● | ● |
| Tưới và nước rỉ | FactIrrigation | khoang × 5 phút | ● | ● | ● | ● | ● | ● | ● |
| Dinh dưỡng vùng rễ | FactRootzone | khoang × 5 phút | ● | ● | ● | ● | ● | ● | ● |
| Tiêu thụ tài nguyên | FactEnergy | khoang × ngày | ● | — | ● | ● | ● | ● | ● |
| Thu hoạch | FactHarvest | khoang × ngày thu hoạch | ● | — | ● | ● | ● | ● | ● |
| Thời tiết tại chỗ | FactWeather | site × 5 phút | ● | ● | ● | — | — | — | — |
| Thời tiết bên ngoài | FactWeatherExternal | site × giờ | ● | ● | ● | — | — | — | — |

Thời tiết chỉ gắn tới Site, không gắn tới Compartment, Crop, Team hay Cycle. Đây là cách thể hiện grain trung thực.

### 4.3 Điểm mạnh

- Câu BR-01 chỉ cần **1 JOIN** và trả về 6 dòng.
- Conformed dimension (DimCompartment, DimDate) cho phép ghép hai fact khác grain, ví dụ FactEnergy với FactHarvest.
- Schema trực quan và khớp trực tiếp với công cụ BI.

### 4.4 Vấn đề cần sửa

1. **Additivity bị phát biểu sai.** Slide 14 ghi "cộng được theo thời gian", nhưng Tair, Rhair và CO2 là **non-additive**: chỉ dùng AVG/MIN/MAX. Thứ cộng được là *số slot* hoặc *số giờ vượt ngưỡng*. Riêng PAR thì tích phân theo thời gian mới ra DLI.
2. **Thiếu `cycle_key`.** Bus matrix đánh ● ở cột Cycle cho FactClimate, nhưng sơ đồ slide 14 không có `cycle_key`.
3. **FactIrrigation có grain mơ hồ** ("5 phút (hoặc ngày)"). Làm vậy là vi phạm nguyên tắc *declare the grain* mà chính nhóm trình bày.
4. **Slide 17 mâu thuẫn grain.** Bước core → mart ghi "gộp AGC lên mức giờ", trái với grain 5 phút. Đề xuất: giữ FactClimate ở 5 phút và thêm một bảng aggregate theo giờ để ghép với Open-Meteo.
5. **BR-04 (slide 19) có lỗi logic.**
   - Câu query join FactEnergy với FactHarvest theo `date_key`. Năng lượng của các ngày không thu hoạch vì vậy bị loại, nên kWh/kg bị **đánh giá thấp**.
   - BR-04 hỏi "kg quả **loại A**", nhưng mẫu số lại là A + B.
   - Cách đúng là aggregate từng fact lên grain chung (khoang hoặc vụ) rồi mới join:

   ```sql
   WITH e AS (
     SELECT compartment_key, SUM(heating_energy + electricity_lamp) AS kwh
     FROM marts.FactEnergy GROUP BY 1
   ), h AS (
     SELECT compartment_key, SUM(class_a_kg) AS kg_a
     FROM marts.FactHarvest GROUP BY 1
   )
   SELECT c.compartment_code, t.team_name,
          e.kwh / NULLIF(h.kg_a, 0) AS kwh_per_kg_a
   FROM e JOIN h USING (compartment_key)
   JOIN marts.DimCompartment c USING (compartment_key)
   JOIN marts.DimTeam t ON t.team_key = c.team_key   -- hoặc qua DimCycle
   ORDER BY kwh_per_kg_a;
   ```

6. **BR-01 có vấn đề về ngữ nghĩa.** `t_heat_sp` là ngưỡng dưới (sưởi). Điều kiện `ABS(tair − t_heat_sp) > 0.5` sẽ đếm cả những buổi chiều nắng, khi nhiệt độ cao hơn ngưỡng sưởi là bình thường. Nên dùng dải `[heating sp, ventilation sp]` nếu dataset có cột ventilation setpoint.

---

## 5. Schema đại diện Hybrid

### 5.1 Kiến trúc nhiều tầng

```
Nguồn            AGC 2019 (.7z) · Open-Meteo REST API
   │             Python (requests, py7zr)
   ▼
Staging          BigQuery `stg` — ảnh 1:1 file gốc, kiểu STRING
   │             + _source_file, _team, _loaded_at (lineage)
   │             Spark: explode wide → long, tách measure / setpoint / actuator
   ▼
Core 3NF         BigQuery `edw` — SITE · COMPARTMENT · CROP · TEAM · GROWING_CYCLE
(Inmon)          SENSOR / SENSOR_READING · ACTUATOR_STATE · IRRIGATION_EVENT
   │             RESOURCE_CONSUMPTION · HARVEST · LAB_SAMPLE
   │             → single source of truth
   │             dbt: SELECT … FROM edw, sinh surrogate key, tính vpd_kpa / DLI / temp_off_sp
   ▼
Marts star       BigQuery `marts` — FactClimate · FactRootzone · FactIrrigation · FactEnergy
(Kimball)        FactHarvest · FactWeather · FactWeatherExternal + 7 conformed dimension
   │             partition theo date_key, cluster theo compartment_key
   ▼
Khai thác        SQL cho BR-01…06 · dashboard (Streamlit / Metabase) · báo cáo traceability
```

Airflow điều phối toàn bộ DAG: tải → staging → core → mart → test.

### 5.2 Nguyên tắc then chốt

- **Mart luôn là `SELECT … FROM core`**, không bao giờ parse lại file CSV lần hai.
- Business rule chỉ định nghĩa **một lần**, ở core.
- Mart có thể xoá đi rồi dựng lại từ core bất cứ lúc nào.
- Data quality chạy xuyên suốt: `dq_null_flag` và dbt test (`unique`, `not_null`, `relationships`, `accepted_range`).

### 5.3 Nhận xét

Về bản chất, đây là kiến trúc staging → integration → presentation, tương tự medallion (bronze/silver/gold). Khác biệt là tầng silver được mô hình hoá theo 3NF nghiêm túc chứ không chỉ là dữ liệu "cleaned".

### 5.4 Công nghệ

| Tầng | Chọn | Lý do | Thay thế |
|---|---|---|---|
| Ingestion | Python | File tĩnh và REST API, không có luồng real-time | Kafka Connect |
| Processing | Apache Spark (PySpark batch) | Unpivot 50 cột, đổi Excel serial, tính VPD/DLI | Flink, Spark Streaming |
| Storage | Google BigQuery (`stg · edw · marts`) | Columnar, partition và cluster; có free tier | Snowflake, Databricks, PostgreSQL |
| Transformation | dbt | Mart là SELECT từ core, có test | Spark SQL |
| Orchestration | Apache Airflow | DAG chạy lại được từng bước | Cron + shell |
| Khai thác | Streamlit / Metabase | Mỗi BR một màn hình | Grafana |

Không dùng streaming vì dataset là dữ liệu lịch sử đã đóng; dựng Kafka để phát lại CSV không phục vụ BR nào.

---

## 6. So sánh ba approach trên case này

### 6.1 So sánh tổng quát

| Tiêu chí | Inmon | Kimball | Hybrid |
|---|---|---|---|
| Hướng tiếp cận | Top-down (EDW trước, mart sau) | Bottom-up (từng mart, nối bằng conformed dim) | Theo tầng: core 3NF → mart star |
| Đơn vị xây dựng | Entity của doanh nghiệp | Business process | Cả hai |
| Mô hình dữ liệu | 3NF (normalized) | Star / snowflake (denormalized) | 3NF ở core, star ở mart |
| Single source of truth | EDW | Tập star + conformed dimension | Core 3NF |
| Time-to-value | Chậm | Nhanh | Trung bình |

### 6.2 So sánh trên case Smart Greenhouse

| Tiêu chí | Inmon (3NF) | Kimball (star) | Hybrid |
|---|---|---|---|
| Xử lý grain lệch | Tự nhiên: mỗi entity một grain | Phải khai báo grain từng fact, dùng drill-across | Core giữ grain gốc, mart chọn grain theo BR |
| BR-01 (tuân thủ khí hậu) | Pivot + 3–4 bảng | 1 JOIN | 1 JOIN (trên mart) |
| BR-04 (drill-across năng lượng/sản lượng) | Nhiều JOIN qua GROWING_CYCLE | Ghép qua conformed dim | Như Kimball |
| BR-08 (traceability) | **Mạnh nhất**: lô → vụ → reading | Yếu: phải qua DimCycle, mất chi tiết | Truy vấn trên core |
| Thêm sensor hoặc site mới | Thêm dòng | Thêm cột, rebuild fact | Thêm dòng ở core, mart dựng lại |
| Dư thừa dữ liệu | Thấp | Có (denormalized) | ~2 bản (core + mart) |
| Độ phức tạp pipeline | Trung bình | Thấp | Cao nhất (Spark + dbt + Airflow) |
| Người dùng nghiệp vụ tự khai thác | Khó | Dễ | Dễ (trên mart) |
| Hợp nhất khi | Nhiều nguồn, cần audit, vòng đời dài | Phạm vi rõ, cần dashboard nhanh | Vừa cần tích hợp bền vững vừa cần báo cáo nhanh |

### 6.3 Ví dụ: cùng câu BR-01 trên hai mô hình

**Kimball (mart star):** 1 join, trả về 6 dòng.

```sql
SELECT c.compartment_code,
       SUM(CASE WHEN ABS(f.tair_c - f.t_heat_sp) > 0.5 THEN 5 ELSE 0 END) / 60.0 AS hours_off
FROM marts.FactClimate f
JOIN marts.DimCompartment c USING (compartment_key)
GROUP BY 1 ORDER BY 2 DESC;
```

**Inmon (core 3NF):** phải pivot trước, nhiều bảng.

```sql
WITH p AS (
  SELECT r.ts, s.compartment_id,
    MAX(IF(s.sensor_type = 'air_temp', r.value_num, NULL))     AS tair,
    MAX(IF(a.actuator_type = 't_heat_sp', a.setpoint_num, NULL)) AS sp
  FROM edw.SENSOR_READING r
  JOIN edw.SENSOR s USING (sensor_id)
  LEFT JOIN edw.ACTUATOR_STATE a ON a.ts = r.ts   -- ⚠ thiếu điều kiện compartment
  GROUP BY 1, 2
) ...
```

---

## 7. Lý do chọn Hybrid và các đánh đổi

### 7.1 Lý do phù hợp với case

1. **Nhiều nguồn, nhiều grain.** Dữ liệu có 5 phút, giờ, ngày và fact thưa, từ hai nguồn thời tiết khác nhau. Cần một tầng tích hợp trung lập (core 3NF) trước khi quyết định grain cho từng fact.
2. **Hai kiểu người dùng.** Traceability cần hub quan hệ (GROWING_CYCLE). Phân tích cần star schema để truy vấn nhanh và dễ đọc.
3. **Mục tiêu học thuật.** Hybrid cho phép hiện thực **cả** Inmon và Kimball trên cùng dataset, rồi so sánh bằng số liệu thật chứ không chỉ lý thuyết. Đây là điểm mạnh nhất của bài.
4. **Hợp với ELT hiện đại.** dbt đặt mart là SELECT từ core, có test và lineage.
5. **Mở rộng được.** Thêm site mới (ví dụ Đà Lạt) hoặc sensor mới chỉ thêm dòng ở core; mart dựng lại tự động.

### 7.2 Đánh đổi

| Đánh đổi | Mức độ trong case này | Ghi chú |
|---|---|---|
| Lưu trữ gấp đôi (core + mart) | **Không đáng kể** | Dữ liệu chỉ vài chục MB |
| Pipeline thêm một tầng, cần orchestration | Trung bình | Airflow DAG: tải → stg → edw → marts → test |
| Công nghệ nặng so với lượng dữ liệu | **Đáng nói** | 14 triệu điểm, BigQuery SQL hoặc DuckDB xử lý thoải mái mà không cần Spark. Spark hợp lý vì môn học yêu cầu, không phải vì volume. Nên chủ động nói điều này |
| Cần kỷ luật: mart chỉ đọc từ core | Trung bình | Cần enforce bằng dbt `ref()` và code review |
| Đội phải hiểu cả hai trường phái | Trung bình | Chấp nhận được trong bối cảnh môn học |
| Chỉ batch, không real-time | Thấp | Dataset lịch sử đã đóng |

### 7.3 Phản biện cần chuẩn bị

- **"Sao không dùng Kimball luôn?"** Cả 6 BR chính đều trả lời được bằng Kimball thuần có staging tốt. Lý do mạnh nhất để có core 3NF là **traceability (BR-08)** và **khả năng thêm nguồn hoặc site mới**. Hiện BR-08 chỉ nằm ở mục "mở rộng". Nên **nâng BR-08 thành BR chính** để lập luận Hybrid có chỗ dựa rõ ràng.
- **Benchmark khó cho kết quả rõ ràng.** Với FactClimate khoảng 287 nghìn dòng, chênh lệch thời gian chạy và bytes scan giữa hai mô hình sẽ rất nhỏ. Bytes billed của BigQuery còn có ngưỡng tối thiểu theo bảng. Nên đo thêm:
  - số JOIN, số dòng SQL và độ phức tạp truy vấn;
  - hoặc scale dữ liệu lên (nhân bản các vụ hoặc khoang) để thấy khác biệt thực.

### 7.4 Kết luận

Hybrid là lựa chọn hợp lý cho case này. Lý do không nằm ở quy mô dữ liệu mà ở **tính không đồng nhất của dữ liệu** (nhiều grain, nhiều nguồn, trộn measurement/setpoint/actuator) và ở **hai kiểu nhu cầu** (phân tích và truy xuất). Core 3NF lo tích hợp và traceability, mart star lo tốc độ và khả năng tự khai thác. Cái giá phải trả chủ yếu là độ phức tạp pipeline, và nhóm nên nói thẳng điều đó khi bảo vệ.

---

## 8. Checklist sửa trước khi nộp

- [ ] **BR-04:** aggregate từng fact lên grain chung trước khi join; mẫu số chỉ lấy `class_a_kg`.
- [ ] **SQL Inmon (slide 19):** thêm điều kiện compartment vào join ACTUATOR_STATE; join bảng ACTUATOR để lấy `actuator_type`; sửa "4 bảng".
- [ ] **ERD 3NF:** thêm entity ACTUATOR; ghi rõ cách nối SENSOR_READING với GROWING_CYCLE (range join).
- [ ] **FactClimate:** thêm `cycle_key` cho khớp bus matrix.
- [ ] **Slide 14:** sửa câu additivity; measure khí hậu là non-additive.
- [ ] **FactIrrigation:** chốt một grain duy nhất.
- [ ] **Slide 17:** làm rõ "gộp lên mức giờ" là một bảng aggregate riêng, không thay đổi grain của FactClimate.
- [ ] **BR-01:** dùng dải setpoint `[heating, ventilation]` thay vì chỉ `t_heat_sp`.
- [ ] **ETL:** kiểm tra giờ mùa hè (DST) ngày 29/03/2020; xử lý delta cho `Cum_irr`.
- [ ] **Business requirement:** cân nhắc nâng BR-08 (traceability) thành BR chính.
- [ ] **Benchmark:** bổ sung chỉ số độ phức tạp truy vấn, hoặc scale dữ liệu.
