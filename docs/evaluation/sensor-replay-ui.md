# Sensor replay UI — BR-01 và BR-03

Màn hình **Sensor replay (BR-01/03)** đọc current replay publication riêng và
pin một publication ID cho toàn bộ thao tác: chọn rule, KPI, episode, chart và
CSV. Historical scope không được cộng vào replay. Rule lịch sử chỉ được ánh xạ
khi snapshot và khoang khớp; lựa chọn mặc định là không có rule.

Màn hình hiển thị replay clock theo source time, mẫu gần nhất, quality từng
measure, last received theo UTC và tuổi tín hiệu theo đồng hồ vận hành. Ngưỡng
stale có thể chỉnh. Tuổi tín hiệu không phải published-to-queryable latency;
phép đo đó và fault matrix đầy đủ thuộc T36. Không có sensor vật lý, cảnh báo
ngoài hay điều khiển thiết bị.

Khi replay chưa đối soát, KPI và episode mang nhãn provisional; episode chạm
cửa sổ cuối mang nhãn open_provisional. Các missing/conflict/quarantine được
hiển thị, không thay số chưa biết bằng 0. CSV có publication, replay, scope,
quality version, basis, rule, cursor và cùng summary/episode như màn hình.
Biểu đồ tối đa khoảng 3.000 điểm mỗi measure; việc lấy mẫu chỉ áp dụng cho
chart, không thay đổi tính toán hay CSV.

Kiểm thử Streamlit AppTest chạy với PostgreSQL cô lập đã kiểm tra chọn rule
nguồn, UI không phụ thuộc historical publication, stale, pause, auto-refresh,
late conflict và nút CSV. Kiểm thử pure kiểm tra timestamp, nhãn episode và
nội dung CSV. Các bài kiểm tra này không thay cho demo sensor toàn luồng T36.
