# Data Warehouse Design Approaches — Research Notes

*Tổng quan các phương pháp thiết kế Data Warehouse: Kimball, Inmon, Data Vault 2.0*

---

## 1. Giới thiệu

Có ba trường phái thiết kế Data Warehouse (DWH) kinh điển, khác nhau ở chỗ: **thứ tự xây dựng** (top-down hay bottom-up), **nơi đặt độ phức tạp** (ở tầng tích hợp hay tầng báo cáo), và **mức độ chịu đựng thay đổi** của nguồn dữ liệu theo thời gian.

| Approach | Tác giả | Triết lý cốt lõi |
|---|---|---|
| **Kimball** (Dimensional Modeling) | Ralph Kimball | Bottom-up — tối ưu cho truy vấn & báo cáo nhanh |
| **Inmon** (Corporate Information Factory) | Bill Inmon | Top-down — tích hợp trước, phân phối sau ("integrate before you decorate") |
| **Data Vault 2.0** | Dan Linstedt | Hybrid, thiết kế để chịu được thay đổi nguồn liên tục |

---

## 2. Kimball — Dimensional Modeling (Bottom-up)

**Triết lý:** Xây từng data mart theo nhu cầu nghiệp vụ cụ thể trước, sau đó liên kết các mart lại bằng **conformed dimensions** (bus matrix) để tạo thành DWH toàn doanh nghiệp.

**Kiến trúc:**
- **Star schema**: một bảng **fact** (đo lường, ở một "grain" cụ thể) được bao quanh bởi các bảng **dimension** (ngữ cảnh mô tả: thời gian, khách hàng, sản phẩm...).
- **Snowflake schema**: biến thể chuẩn hóa (normalize) các dimension thành nhiều bảng con để giảm dư thừa dữ liệu.
- **Conformed dimensions**: các dimension dùng chung giữa nhiều fact table/mart, đảm bảo định nghĩa nhất quán toàn doanh nghiệp (Kimball Bus Matrix).
- Nạp dữ liệu qua ETL: Extract → Transform (làm sạch, chuẩn hoá, tính surrogate key, xử lý Slowly Changing Dimension - SCD) → Load thẳng vào schema dạng star.

**Ưu điểm:**
- Trực quan, dễ hiểu cho business user và BI analyst; truy vấn đơn giản (ít join hơn snowflake/3NF).
- Hiệu năng đọc tốt, phù hợp dashboard/BI, time-to-insight nhanh.
- Dễ triển khai từng phần (incremental), thấy giá trị sớm.

**Nhược điểm:**
- Phụ thuộc vào sự đồng thuận giữa các nhóm để dimension "conform" được với nhau — nếu không quản trị tốt dễ sinh ra các "silo" cục bộ.
- Không có một tầng "một nguồn sự thật" (single source of truth) được chuẩn hoá đầy đủ như Inmon.
- Khó đáp ứng yêu cầu audit/compliance phức tạp vì lịch sử thay đổi thường xử lý ở mức dimension (SCD Type 1/2/3), không phải ở mức nguyên bản (raw).

**Khi nào dùng:** Khách hàng chính là BI analyst/business user, cần dashboard/báo cáo nhanh, áp lực governance/tuân thủ chưa cao, đội ngũ nhỏ, muốn thấy kết quả sớm.

---

## 3. Inmon — Corporate Information Factory (Top-down)

**Triết lý:** "Integrate before you decorate" — xây một **Enterprise Data Warehouse (EDW)** tích hợp, chuẩn hoá (thường ở dạng 3NF) làm nguồn sự thật duy nhất trước, rồi mới sinh ra các **data mart** (thường ở dạng dimensional/star) phục vụ từng phòng ban từ EDW đó.

**Kiến trúc:**
- Dữ liệu từ nhiều hệ thống nguồn được tích hợp, làm sạch, chuẩn hoá theo **subject area** (khách hàng, sản phẩm, giao dịch...) vào EDW ở dạng chuẩn hoá cao (3NF), lưu lịch sử đầy đủ.
- Từ EDW, các **department data mart** được sinh ra (dependent data marts) theo nhu cầu từng bộ phận — khác với Kimball là mart được xây trước rồi mới hợp nhất.

**Ưu điểm:**
- Có một định nghĩa doanh nghiệp thống nhất (single enterprise definition) cho các thực thể cốt lõi — tránh mâu thuẫn số liệu giữa các phòng ban.
- Audit trail và lưu trữ lịch sử tốt, phù hợp yêu cầu tuân thủ/quản trị dữ liệu (data governance).
- Nhất quán xuyên suốt tổ chức (cross-domain coherence).

**Nhược điểm:**
- Giai đoạn thiết kế ban đầu dài hơn, chi phí đầu tư trước (upfront cost) cao — lâu mới thấy giá trị.
- Đòi hỏi nguồn dữ liệu tương đối ổn định; nếu schema nguồn thay đổi thường xuyên, mô hình 3NF trung tâm dễ phải thiết kế lại.
- Truy vấn trực tiếp trên EDW (3NF) phức tạp hơn (nhiều join) so với star schema — thường phải qua data mart mới thân thiện cho BI.

**Khi nào dùng:** Có áp lực quy định/tuân thủ (regulatory), nhiều phòng ban cần thống nhất định nghĩa dữ liệu, nguồn dữ liệu tương đối ổn định, tổ chức đủ lớn để đầu tư dài hạn.

---

## 4. Data Vault 2.0

**Triết lý:** Thiết kế để **chịu được thay đổi liên tục** của hệ thống nguồn mà không cần redesign lại mô hình — tách biệt hoàn toàn giữa "cấu trúc" và "nội dung mô tả", chỉ **append** (thêm), gần như không bao giờ **update/delete**.

**Kiến trúc — 3 thành phần chính:**
- **Hub**: lưu **business key** duy nhất của một thực thể nghiệp vụ (khách hàng, sản phẩm, đơn hàng...) — giống như một "nhà ga trung tâm", chỉ chứa khóa định danh, không chứa mô tả.
- **Link**: lưu **mối quan hệ** giữa các Hub (ví dụ: khách hàng nào mua sản phẩm nào) — giống các tuyến kết nối giữa các nhà ga.
- **Satellite**: lưu các **thuộc tính mô tả** và **lịch sử thay đổi** theo thời gian (tên, địa chỉ, giá...) gắn với Hub hoặc Link, mỗi thay đổi được ghi thành một bản ghi mới kèm timestamp.
- **Raw Vault**: lớp lưu dữ liệu nguyên bản, bất biến (immutable), đúng như nguồn sinh ra.
- **Business Vault**: lớp tính toán thêm business rule, tổng hợp (PIT tables, bridge tables...) để phục vụ truy vấn thực tế; từ đây mới sinh ra các data mart (thường theo kiểu Kimball star schema) cho người dùng cuối.

**Ưu điểm:**
- Chịu được thay đổi nguồn (thêm entity/attribute/relationship mới) mà **không phá vỡ** cấu trúc đã có — chỉ cần thêm bảng/cột mới.
- Audit trail đầy đủ theo mặc định — lưu lại toàn bộ lịch sử, phù hợp yêu cầu compliance nghiêm ngặt.
- Có thể nạp dữ liệu song song (parallel load) tốt vì Hub/Link/Satellite độc lập nhau — phù hợp khối lượng dữ liệu lớn.

**Nhược điểm:**
- Mô hình phức tạp, đường cong học tập dốc; số lượng bảng tăng nhanh (nhiều Hub/Link/Satellite hơn hẳn số bảng fact/dimension tương ứng).
- Hiệu năng truy vấn trực tiếp trên Raw Vault kém (nhiều join) — bắt buộc phải có tầng Business Vault/data mart phía sau mới dùng được cho BI.
- Chi phí hạ tầng và vận hành cao hơn.

**Khi nào dùng:** Nguồn dữ liệu thay đổi thường xuyên (schema drift), yêu cầu audit/truy vết lịch sử rất cao, cần mô hình "sống" được nhiều năm mà không phải redesign, tổ chức lớn/nhiều nguồn.

---

## 5. Bảng so sánh tổng hợp

| Tiêu chí | Kimball | Inmon | Data Vault 2.0 |
|---|---|---|---|
| Hướng tiếp cận | Bottom-up (mart → warehouse) | Top-down (warehouse → mart) | Hybrid, hướng lưu trữ bất biến |
| Cấu trúc trung tâm | Star/snowflake schema | 3NF (chuẩn hoá cao) | Hub – Link – Satellite |
| Tốc độ triển khai ban đầu | Nhanh | Chậm | Trung bình–chậm (cần thêm tầng business vault) |
| Hiệu năng truy vấn (cho end-user) | Tốt (ít join) | Trung bình (qua mart mới tốt) | Kém ở raw vault, cần mart phía sau |
| Khả năng chịu thay đổi nguồn | Trung bình | Kém (khi nguồn thay đổi nhiều) | Rất tốt |
| Audit/lịch sử đầy đủ | Hạn chế (qua SCD) | Tốt | Rất tốt (mặc định) |
| Độ phức tạp mô hình | Thấp | Trung bình | Cao |
| Phù hợp | BI nhanh, team nhỏ | Doanh nghiệp cần 1 nguồn sự thật, compliance | Nguồn biến động, audit nghiêm ngặt, quy mô lớn |

---

## 6. Xu hướng hiện đại

- Nhiều kiến trúc thực tế hiện nay **kết hợp cả ba**: dùng **Data Vault hoặc Inmon-style EDW** ở tầng lõi (core/integration layer) để lưu trữ bền vững, chịu thay đổi và đảm bảo audit; sau đó sinh ra các **data mart kiểu Kimball (star schema)** ở tầng phục vụ (serving/consumption layer) để BI/dashboard truy vấn nhanh.
- Với nền tảng cloud (BigQuery, Snowflake, Redshift, Databricks/Lakehouse...), ranh giới ETL vs **ELT** mờ dần: dữ liệu thô được load trước (Load) rồi transform ngay trong warehouse bằng SQL/dbt, tận dụng khả năng compute co giãn của cloud thay vì transform trước khi load như ETL truyền thống.
- **Data Lakehouse** (Databricks, BigLake, Oracle Data Lakehouse...) là xu hướng hợp nhất data lake (lưu raw, chi phí thấp, hỗ trợ bán/phi cấu trúc) và data warehouse (transaction, schema, hiệu năng truy vấn) — cho phép áp dụng cả 3 approach thiết kế ở trên nhưng trên nền lưu trữ thống nhất (vd: Delta Lake, Apache Iceberg).

---

## 7. Gợi ý áp dụng cho project môn Data Engineering

Theo `DE_Requirements.md`, project cần: xác định nghiệp vụ/nguồn dữ liệu cho một lĩnh vực ứng dụng, chọn công nghệ Data Management + Data Processing, và **so sánh với ít nhất 1 giải pháp thay thế**. Gợi ý áp dụng phần "Data Warehouse design approach" vào báo cáo:

1. **Chọn 1 approach chính** làm giải pháp đề xuất (thường Kimball star schema là dễ trình bày và demo ứng dụng nhất trong thời gian ngắn của môn học).
2. **Chọn Inmon hoặc Data Vault làm giải pháp thay thế** để so sánh (đúng yêu cầu "so sánh với ít nhất 1 giải pháp thay thế") — nêu rõ lý do vì sao approach chính phù hợp hơn với đặc điểm nguồn dữ liệu và nhu cầu người dùng của lĩnh vực ứng dụng đã chọn.
3. Trình bày rõ: grain của fact table, danh sách dimension, loại SCD dùng cho dimension nào (nếu chọn Kimball); hoặc Hub/Link/Satellite chính (nếu chọn Data Vault).
4. Liên hệ với công nghệ Data Management đã chọn trong bảng công nghệ (Redshift, BigQuery, Synapse, Databricks...) — mỗi công nghệ hỗ trợ tốt các approach khác nhau (ví dụ: BigQuery/Redshift rất hợp Kimball star schema; Databricks/Lakehouse hợp cả Data Vault lẫn lakehouse pattern).

---

## 8. Nguồn tham khảo

- [Kimball vs Inmon vs Data Vault 2.0: Data Warehouse Architecture Guide — TalkingSchema](https://talkingschema.ai/blog/compare-inmon-kimball-datavault)
- [Data Vault Warehouse Explained, Vault vs Star Schema — AltexSoft](https://www.altexsoft.com/blog/data-vault-architecture/)
- [Exploring Data Warehouse Modeling Methodologies: Kimball, Inmon, and Data Vault — Medium](https://medium.com/@lgsoliveira/exploring-data-warehouse-modeling-methodologies-kimball-inmon-and-data-vault-c46f0a253190)
- [Comparative study of data warehouses modeling approaches: Inmon, Kimball and Data Vault — ResearchGate](https://www.researchgate.net/publication/312486486_Comparative_study_of_data_warehouses_modeling_approaches_Inmon_Kimball_and_Data_Vault)
- [Data Modeling for Modern DWH: Data Vault 2.0 vs Kimball, Inmon, Anchor & Mixed Approach — AnalyticsCreator](https://www.analyticscreator.com/blog/how-to-choose-the-right-data-modeling-techniques-for-your-data-warehouse)
- [Kimball vs Inmon: Data Warehouse Design Strategies — idatamax](https://idatamax.com/blog/kimball-vs-inmon-data-warehouse-design)
- [Data Warehousing Architectures – Kimball vs Inmon vs Data Vault — JobPrep Arena Academy](https://www.jobpreparena.com/blog/data-warehousing-architectures-kimball-vs-inmon-vs-data-vault)
- [Data Vault 2.0: Hubs, Links, Satellites Explained — Scalefree](https://www.scalefree.com/consulting/data-vault-2-0/)
- [Data Vault Modeling: Hubs, Links, and Satellites — DEV Community](https://dev.to/alexmercedcoder/data-vault-modeling-hubs-links-and-satellites-1e1h)

*Ghi chú: tài liệu này là research tổng quan, phục vụ tham khảo cho phần thiết kế Data Warehouse của project môn Data Engineering.*
