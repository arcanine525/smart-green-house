# Data Engineering - Requirements
---

## 1. Mục tiêu (Objectives)

- Cung cấp kiến thức và kỹ năng theo chuẩn đầu ra của môn học
- Hỗ trợ sinh viên tự học và làm việc nhóm về các chủ đề liên quan đến kỹ thuật dữ liệu
- Phát triển ứng dụng phản ánh hiệu quả của kỹ thuật dữ liệu
- Cải thiện kỹ năng thuyết trình

---

## 2. Mô tả chi tiết

### 2.1. Lĩnh vực ứng dụng (Application domain)

Mỗi nhóm được giao một lĩnh vực ứng dụng duy nhất, ví dụ:

- Giáo dục, Y tế, Sản xuất, Marketing, Thương mại điện tử
- Du lịch, Khoa học, Giao thông vận tải, Quản lý chuỗi cung ứng
- Giải trí, v.v.

**Yêu cầu:**
- Xác định nhóm người dùng tiềm năng
- Xác định yêu cầu nghiệp vụ cần dữ liệu từ kỹ thuật dữ liệu
- Liên quan đến: phân tích dữ liệu, khoa học dữ liệu, khai phá dữ liệu, hệ thống Business Intelligence, hệ hỗ trợ ra quyết định

### 2.2. Chủ đề: Xây dựng hệ thống chứng minh giải pháp kỹ thuật dữ liệu thành công

**Ba phần chính của sản phẩm:**

1. **Dữ liệu và nghiệp vụ** trong lĩnh vực ứng dụng
2. **Kỹ thuật dữ liệu** cho yêu cầu nghiệp vụ
3. **Phát triển ứng dụng** đưa kết quả vào thực tế

#### Công nghệ lựa chọn

| Data Management | Data Processing |
|-----------------|-----------------|
| Redshift, Hive, Synapse Analytics | Flink, Spark Streaming |
| Cassandra, BigQuery (BigLake), CouchBase | Hadoop Map-Reduce, Storm |
| Databricks, Neo4J, RavenDB, Dynamo | Kafka Stream, ... |
| Oracle data lakehouse, ... | |

#### Yêu cầu số lượng

- **Tối thiểu 2×n yêu cầu nghiệp vụ** (n = số thành viên nhóm)
- Cung cấp lý do chọn công nghệ
- So sánh với ít nhất **1 giải pháp thay thế**

### 2.3. Yêu cầu cụ thể

#### i. Presentation (Bài trình bày)

- Giới thiệu bối cảnh chủ đề được giao
- Trình bày chi tiết nội dung chủ đề
- Trình bày chủ đề cho project
- Các vấn đề liên quan khác nếu cần

#### ii. Project

- Giới thiệu bối cảnh đề tài
- Giới thiệu stakeholders (đặc biệt người dùng), yêu cầu nghiệp vụ, nhu cầu sử dụng dữ liệu
- Trình bày nguồn dữ liệu và đặc điểm dữ liệu dự kiến
- Trình bày giải pháp kỹ thuật dữ liệu và ít nhất **1 giải pháp thay thế** để so sánh
- Trình bày công nghệ quản lý dữ liệu và xử lý dữ liệu
- Phát triển ứng dụng minh họa
- **Đánh giá** dựa trên:
  - Tính đúng đắn của dữ liệu sau kỹ thuật dữ liệu
  - Hiệu suất của giải pháp
  - Hiệu quả hỗ trợ khai thác dữ liệu qua ứng dụng
- Kết luận về project

---

## 3. Chính sách thay đổi

### Thay đổi nhóm
- Thành viên có thể chuyển nhóm sau khi thông báo
- Điều kiện: thỏa thuận giữa các thành viên của cả hai nhóm

### Thay đổi lịch thuyết trình
- Điều kiện: đổi slot với một nhóm khác trong tuần muốn chuyển đến

---

## 4. Đánh giá

### 4.1. Presentation

**Nộp:**
- File PDF thuyết trình (10%) - thời gian 10 phút
- Video mp4/webm (10%) - thời gian tối đa 20 phút
- Thuyết trình tại lớp (10%) - 10 phút

**Deadline:** 1 tuần trước ngày thuyết trình

#### Tiêu chí đánh giá Presentation

| Tiêu chí | 0-4 điểm | 4-6.5 điểm | 6.5-8.5 điểm | 8.5-10 điểm |
|----------|----------|------------|--------------|-------------|
| **Nội dung video** (5đ) | Không khớp với nội dung được giao | Khớp nội dung; nhất quán 40-65% slides | Khớp nội dung; nhất quán 65-85% slides | Khớp nội dung; nhất quán 85-100% slides |
| **Phong cách trình bày video** (4đ) | Không tự tin, không mạch lạc, không phối hợp | Tự tin, mạch lạc, có phối hợp nhưng không tương tác | Tự tin, mạch lạc, có tương tác hạn chế | Tự tin, mạch lạc, có tương tác phù hợp |
| **Thời lượng video** (1đ) | Chênh >10 phút | Chênh 5-10 phút | Chênh 2-5 phút | Đúng thời lượng |
| **Cấu trúc slide** (2đ) | Không rõ ràng | Tổ chức hợp lý nhưng >7 phần hoặc <4 phần | Cấu trúc chi tiết | Tất cả phần đều rõ ràng, logic |
| **Nội dung slide** (6đ) | Chi tiết bề mặt, không có ví dụ minh họa | Chi tiết vừa đủ, có ví dụ minh họa | Chi tiết phù hợp, có thảo luận và ví dụ | Chi tiết sâu, thảo luận tốt, ví dụ phù hợp |
| **Định dạng slide** (2đ) | >10 lỗi | 5-10 lỗi | 2-5 lỗi | Gần như không lỗi |
| **Phong cách trình bày tại lớp** (8đ) | Không tự tin, không tương tác, phân chia không đều | Tự tin, mạch lạc nhưng không tương tác | Tự tin, mạch lạc, tương tác hạn chế | Tự tin, mạch lạc, tương tác tốt |
| **Quản lý thời gian** (2đ) | Trễ >5 phút | Trễ 3-5 phút | Trễ 2-3 phút | Đúng giờ |

### 4.2. Project

**Nộp:**
- Báo cáo kỹ thuật PDF (10%)
- File thuyết trình PDF (5%)
- Video mp4/webm 20-30 phút (5%)
- Sản phẩm ứng dụng chạy được (15%)

**Deadline:** Cuối khóa

#### Tiêu chí đánh giá Project

| Tiêu chí | 0-4 điểm | 4-6.5 điểm | 6.5-8.5 điểm | 8.5-10 điểm |
|----------|----------|------------|--------------|-------------|
| **Cấu trúc báo cáo** (1đ) | Không rõ ràng | Cấu trúc hợp lý nhưng >7 hoặc <4 phần | Cấu trúc tốt | Tất cả phần rõ ràng, logic |
| **Nội dung công nghệ** (4đ) | Gần như không trình bày | Trình bày sơ lược | Trình bày khá đầy đủ với 1-3 ví dụ | Trình bày đầy đủ với >3 ví dụ |
| **Nội dung ứng dụng** (4đ) | Thiếu >75% | Thiếu 40-75% | Thiếu 10-40% | Thiếu <10% |
| **Đánh giá công nghệ** (1đ) | Không đánh giá | Đánh giá bề mặt, tiêu chí không rõ | Đánh giá có hệ thống, <85% tiêu chí rõ | Đánh giá có hệ thống, >85% tiêu chí rõ |
| **Nội dung video** (5đ) | Khớp <40% slides | Khớp 40-65% | Khớp 65-85% | Khớp 85-100% |
| **Phong cách video** (4đ) | Không tự tin, không mạch lạc | Tự tin nhưng không tương tác | Tự tin, tương tác hạn chế | Tự tin, tương tác tốt |
| **Thời lượng video** (1đ) | Chênh >10 phút | Chênh 5-10 phút | Chênh 2-5 phút | Đúng thời lượng |
| **Cấu trúc slide** (2đ) | Không rõ ràng | >7 hoặc <4 phần | Chi tiết phù hợp | Tất cả phần rõ ràng |
| **Nội dung slide** (6đ) | Bề mặt, không ví dụ | Có ví dụ nhưng chưa chi tiết | Chi tiết, thảo luận tốt | Chi tiết sâu, thảo luận và ví dụ phù hợp |
| **Định dạng slide** (2đ) | >10 lỗi | 5-10 lỗi | 2-5 lỗi | Gần như không lỗi |
| **Thiết kế UI** (2đ) | Không phù hợp | Phù hợp nhưng không hài hòa | Phù hợp, hài hòa vừa đủ | Phù hợp, hài hòa, tương tác tốt |
| **Triển khai ứng dụng** (8đ) | Mỗi demo thành công = 4/n/10 điểm (n = số thành viên) |

---

## 5. Điểm thưởng (Bonus)

Điểm thưởng cộng trực tiếp vào điểm Presentation và/hoặc Project.

### Cá nhân
- Thảo luận và hỏi đáp xuất sắc cho bất kỳ bài thuyết trình nào

### Nhóm
- Nhiều giải pháp thay thế với đánh giá xuất sắc
- Yêu cầu nghiệp vụ thú vị với nhiều nhóm người dùng
- Thảo luận với kết quả nghiên cứu

---

## Tóm tắt điểm số

| Thành phần | Tỷ lệ |
|------------|-------|
| Presentation (PDF + Video) | 20% |
| Thuyết trình tại lớp (Presentation) | 10% |
| Báo cáo kỹ thuật (Project) | 10% |
| Thuyết trình PDF (Project) | 5% |
| Video demo (Project) | 5% |
| Sản phẩm ứng dụng (Project) | 15% |
| **Tổng** | **65%** |
