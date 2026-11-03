# Verdant — UI demo Smart Greenhouse

Prototype để review giao diện, chưa phải ứng dụng đã tích hợp PostgreSQL, MQTT hoặc pipeline. Tên Verdant là tên giao diện thử nghiệm. Không có task nghiệp vụ nào trong TODO được tự đánh dấu hoàn tất từ prototype này.

## Mở nhanh bản HTML

Mở [preview.html](preview.html) trong browser. File chứa sẵn Plotly.js, figure JSON, CSS và dữ liệu; không cần server, CDN, font online hoặc Internet. Có thể đổi khoang/ngày/chỉ số, zoom và hover biểu đồ, mở chi tiết episode, kéo giá điện và xuất CSV.

HTML là bản preview được dựng riêng từ cùng fixtures và Plotly figures của app Python; không phải bản export nguyên ứng dụng Streamlit. Layout được thiết kế cùng phong cách nhưng widget và bố trí có thể khác giữa hai bản.

## Chạy ứng dụng Streamlit + Plotly

Từ root repository, dùng Python 3.12:

```bash
python3 -m venv .venv-ui
.venv-ui/bin/python -m pip install -r prototypes/dashboard/requirements.txt
.venv-ui/bin/python -m streamlit run prototypes/dashboard/app.py --server.address 127.0.0.1 --server.port 8501 --browser.gatherUsageStats false
```

Mở `http://127.0.0.1:8501`. Streamlit cần Python server; sau khi cài dependencies app không cần cloud. Môi trường `.venv-ui` được Git ignore. Nếu tạo venv bằng `uv`, dùng `uv pip install --python .venv-ui/bin/python -r prototypes/dashboard/requirements.txt`.

Chạy lệnh từ root để Streamlit đọc theme sáng/xanh trong `.streamlit/config.toml`. File này cũng tắt usage telemetry và mặc định bind server vào loopback.

## Dữ liệu và phạm vi demo

- Ba khoang giả lập, hai ngày nguồn 12–13/04/2020; 288 slot 5 phút mỗi ngày. Đây là fixture tổng hợp deterministic, không phải dữ liệu AGC đã nhập.
- Mỗi ngày thiếu tám mẫu tại 08:40–09:20; pH thêm hai mẫu raw=0 được đánh dấu suspect, baseline là NULL. Chart không nối qua gap.
- KPI ngoài dải, coverage và episode được tính từ đúng fixture/bộ lọc. Dải cố định chỉ để review UI; chưa hiện thực logic requested/realized setpoint đầy đủ của BR-01.
- Nước/thu hoạch/năng lượng là fixture theo ngày. Giả định cùng basis tài nguyên chỉ áp dụng trong demo; không xác minh diện tích dataset thật và không tính kWh/kg A.
- Scenario thay cả hai giá điện từ -30% đến +100%, bước 5%; giữ heat và quantities. Fixture mặc định: 100 MJ/m², 10 + 5 kWh/m², baseline 1.83 EUR/m²; điện +20% cho 2.03 EUR/m². Không có lưu scenario vào DB.
- CSV giữ source time, event ID, publication, metric, raw/accepted value, quality, unit và rule; NULL xuất ô trống, khác số 0.
- Chưa có live replay, auto-refresh hoặc giao tiếp MQTT. Không hiển thị đèn trạng thái kết nối giả.

## Sửa và build lại HTML

```bash
.venv-ui/bin/python prototypes/dashboard/build_preview.py
```

| File | Vai trò |
|---|---|
| `app.py` | UI Streamlit với widget native và Plotly |
| `data.py` | Fixture, tính KPI, scenario bằng Decimal, Plotly figures và CSV dùng chung |
| `template.html` | Layout HTML và điều khiển preview; chưa build không mở để review |
| `build_preview.py` | Nhúng fixtures, figure JSON và Plotly.js vào HTML |
| `preview.html` | Artifact hoàn chỉnh để mở offline |

Các phép tính scenario cho HTML được Python tính trước theo từng nấc slider, tránh công thức chi phí riêng trong JavaScript. Preview chỉ đọc kết quả và cập nhật chart.

## Kiểm tra đã thực hiện

- Kiểm chứng baseline 1.83 → 2.03 EUR/m² khi giá điện tăng 20%; phân biệt pH raw=0/suspect với accepted=NULL trong CSV.
- Streamlit AppTest: khởi động, đổi khoang/ngày và slider; không có exception. Browser đã mở được ứng dụng và hai biểu đồ Plotly.
- HTML browser: chặn mọi HTTP(S) request vẫn render và tương tác được; đổi chỉ số/khoang/ngày, slider, drill-down và tải CSV 288 mẫu thành công.
- Kiểm tra HTML ở viewport desktop 1440px và mobile 390px; sửa overflow ngang và kiểm tra menu mobile.

Các kiểm tra này chỉ xác nhận prototype giao diện, không thay nghiệm thu sáu BR hoặc pipeline.

Tham khảo chính thức: [Streamlit Plotly chart](https://docs.streamlit.io/develop/api-reference/charts/st.plotly_chart), [Plotly interactive HTML](https://plotly.com/python/interactive-html-export/).
