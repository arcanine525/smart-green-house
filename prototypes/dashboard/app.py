"""Run: streamlit run prototypes/dashboard/app.py --server.address 127.0.0.1"""
import streamlit as st
from data import DAYS, HOUSES, METRICS, fixture, climate_figure, cost_figure, csv_text

st.set_page_config(page_title="Verdant · Smart Greenhouse", page_icon="🌱", layout="wide")

st.markdown("""<style>
.stApp{background:#f5f6f0;color:#243d31}header[data-testid="stHeader"]{background:#f5f6f0}
[data-testid="stSidebar"]{background:#173d30;color:#dbe4db}[data-testid="stSidebar"] *{color:#dbe4db}
.block-container{padding-top:4.5rem;padding-bottom:2rem;max-width:1600px}
h1{font-size:2rem!important;letter-spacing:-1px;font-weight:600!important}h3{font-size:1.05rem!important;letter-spacing:-.2px}
[data-testid="stVerticalBlockBorderWrapper"]>div{border-color:#e7ebe2!important;border-radius:12px!important}
[data-testid="stVerticalBlockBorderWrapper"]{background:white;border-radius:12px}
.brand{font-size:31px;font-weight:650;letter-spacing:-1.5px;margin:0 0 2px}.brand em{color:#d7e9ab;font-style:normal}
.brand-sub{font-size:9px;letter-spacing:2px;color:#a6bdac!important;margin-bottom:38px}
.sidenav{display:block;padding:13px 14px;margin:6px 0;border-radius:8px;font-size:13px;text-decoration:none!important;color:#dbe4db!important}
.sidenav:first-of-type{background:#d7e9ab;color:#173d30!important}.side-note{border:1px solid #41634f;border-radius:9px;padding:16px;margin-top:70px;font-size:12px;color:#adc5b3!important}
.eyebrow{font-size:10px;letter-spacing:2px;color:#89967e;margin-bottom:18px}.subtitle{font-size:13px;color:#7c867c;margin-top:-8px;margin-bottom:20px}
.demo{padding:12px 16px;background:#eaf0dd;border:1px solid #e1e9d1;border-radius:8px;font-size:12px;color:#65784e;margin:6px 0 22px}.demo b{font-size:10px;letter-spacing:1px;margin-right:12px}
.metriccard{background:white;border:1px solid #e7ebe2;border-radius:12px;padding:18px 19px;margin:6px 0 20px;min-height:132px}.metriccap{font-size:12px;color:#7d8b73}.metricvalue{font-size:32px;font-weight:550;letter-spacing:-1px;margin:10px 0 3px}.metricvalue small{font-size:13px;color:#8e9984;margin-left:6px;font-weight:400}.metricnote{font-size:10px;color:#8b9582}.warm .metricvalue{color:#ad783f}.warm{background:#fffdf8}
.legend{font-size:11px;color:#87917f;margin:0 0 8px}.legend span{margin-right:17px}.chip{display:inline-block;background:#edf3e4;color:#67814e;font-size:10px;border-radius:5px;padding:4px 8px;margin-bottom:8px}
.issue{border-bottom:1px solid #edf0e9;padding:13px 0}.issue b{font-size:13px;font-weight:550}.issue p{font-size:12px;color:#84917b;line-height:1.7;margin:6px 0}.note{font-size:11px;color:#8c9681;line-height:1.7}.costnumber{font-size:30px;color:#23664e;letter-spacing:-1px;font-weight:550}.costnumber small{font-size:12px;letter-spacing:0;color:#8b9980}
.resource{padding:10px 0;font-size:12px;color:#7c8b70}.resource strong{float:right;color:#3d5837;font-weight:550}.mini-bar{height:7px;background:#edf2e4;border-radius:5px;margin:10px 0 4px;overflow:hidden}.mini-bar div{height:100%;background:#91ad76}.foot{font-size:10px;color:#99a18d;margin-top:25px;border-top:1px solid #e4e9dc;padding-top:16px}
.st-key-climate_panel,.st-key-insights_panel,.st-key-resources_panel,.st-key-episodes_panel,.st-key-quality_panel,.st-key-cost_panel{background:white;border-radius:12px;border:1px solid #e7ebe2}
</style>""", unsafe_allow_html=True)


def number(value, digits=1):
    return f"{value:.{digits}f}".replace(".", ",")


with st.sidebar:
    st.markdown('<div class="brand">verdant<em>.</em></div><div class="brand-sub">SMART GREENHOUSE</div>', unsafe_allow_html=True)
    st.caption("KHÔNG GIAN LÀM VIỆC")
    st.markdown('''<a class="sidenav" href="#overview">▦ &nbsp; Tổng quan</a>
    <a class="sidenav" href="#climate">∿ &nbsp; Điều kiện trồng</a>
    <a class="sidenav" href="#resources">◌ &nbsp; Nước & thu hoạch</a>
    <a class="sidenav" href="#cost">ϟ &nbsp; Năng lượng & giá</a>
    <a class="sidenav" href="#quality">◇ &nbsp; Chất lượng dữ liệu</a>
    <div class="side-note">Trồng tốt hơn, từ dữ liệu.<br/><br/>GROUP 29<br/>Bản review giao diện · Local</div>''', unsafe_allow_html=True)

st.markdown('<div id="overview" class="eyebrow">WORKSPACE / SMART GREENHOUSE</div>', unsafe_allow_html=True)
st.title("Tổng quan nhà kính")
st.markdown('<p class="subtitle">Hiểu điều kiện trồng. Theo dõi từng nguồn lực.</p><div class="demo"><b>DỮ LIỆU MINH HỌA</b> Fixture tổng hợp để review UI. Chưa kết nối sensor hoặc database; không phải kết quả AGC.</div>', unsafe_allow_html=True)

filter1, filter2, spacer, export = st.columns([1.25,1.25,1.5,1.2], vertical_alignment="bottom")
house = filter1.selectbox("Khoang", list(HOUSES), index=1, format_func=HOUSES.get, key="house")
day = filter2.selectbox("Ngày nguồn", DAYS, format_func=lambda d: "/".join(reversed(d.split("-"))), key="day")
item = fixture(house, day)
metric = st.session_state.get("metric") or "temperature"
spec, summary = METRICS[metric], item["summaries"][metric]
export.download_button("↓ Xuất dữ liệu CSV", csv_text(item,metric).encode("utf-8-sig"), file_name=f"demo-{house}-{day}-{metric}.csv", mime="text/csv", width="stretch")
r = item["resources"]
cards = [
    (spec["label"]+" cuối ngày", number(summary["latest"],0 if metric=="co2" else 1), spec["unit"], "Mẫu nguồn lúc 23:55", ""),
    ("Ngoài dải demo", str(summary["out_minutes"]), "phút", f'{len(summary["episodes"])} đợt · {number(summary["out_pct"])}% thời gian hợp lệ', "warm"),
    ("Tỷ lệ drain", number(r["drain"]/r["irrigation"]*100), "%", "Drain / tưới · cùng ngày hợp lệ", ""),
    ("Sản lượng loại A", number(r["a"],2), "kg/m²", f'{number(100*r["a"]/(r["a"]+r["b"]),2)}% sản lượng A + B', ""),
]
for col,(label,value,unit,note,style) in zip(st.columns(4),cards):
    col.markdown(f'<div class="metriccard {style}"><div class="metriccap">{label}</div><div class="metricvalue">{value}<small>{unit}</small></div><div class="metricnote">{note}</div></div>', unsafe_allow_html=True)

left,right = st.columns([1.9,1],gap="medium")
with left:
    with st.container(border=True,key="climate_panel"):
        st.subheader("Diễn biến điều kiện trồng",anchor="climate")
        st.caption("Quan sát theo thời gian nguồn · mỗi mẫu 5 phút")
        st.segmented_control("Chỉ số", list(METRICS), default="temperature", format_func=lambda key: METRICS[key]["label"], key="metric", label_visibility="collapsed")
        st.markdown(f'<span class="chip">Độ phủ {number(summary["coverage"])}%</span>', unsafe_allow_html=True)
        st.plotly_chart(climate_figure(item,metric), theme=None, width="stretch", config={"displayModeBar":False}, key="climate_chart")
        st.markdown(f'<div class="legend"><span>━ Giá trị quan sát</span><span>▧ Dải demo {spec["low"]}–{spec["high"]} {spec["unit"]}</span><span>▥ Khoảng thiếu dữ liệu</span></div>', unsafe_allow_html=True)
with right:
    with st.container(border=True,key="insights_panel"):
        st.subheader("Những điểm cần xem lại")
        st.caption("Giữ ngữ cảnh trước khi đưa ra nhận định")
        episode = summary["episodes"][0] if summary["episodes"] else None
        text = f'{episode["start"][11:16]}–{episode["end"][11:16]} · {episode["minutes"]} phút. Đợt dài nhất của ngày trong fixture.' if episode else "Không có đợt ngoài dải trên mẫu hợp lệ."
        st.markdown(f'<div class="issue"><b>↗ {spec["label"]} ngoài dải demo</b><p>{text}</p></div><div class="issue"><b>··· Khoảng dữ liệu chưa biết</b><p>08:40–09:20 thiếu 8 mẫu. Tổng thời gian chưa dùng được: {summary["unknown_minutes"]} phút.</p></div><div class="issue"><b>≈ Giữ rõ giả định diện tích</b><p>Chi phí demo giả định cùng basis tài nguyên. Chưa công bố KPI kWh/kg A.</p></div>', unsafe_allow_html=True)
        st.caption("Ngưỡng minh họa, chưa thẩm định nông học.")

left,right = st.columns([1.9,1],gap="medium")
with left:
    with st.container(border=True,key="resources_panel"):
        st.subheader("Nước & thu hoạch",anchor="resources")
        st.caption("Tổng ngày nguồn · dữ liệu minh họa cùng bộ lọc")
        water,harvest = st.columns(2)
        water.markdown(f'<div class="resource">Lượng tưới <strong>{number(r["irrigation"],2)} L/m²</strong></div><div class="resource">Lượng drain <strong>{number(r["drain"],2)} L/m²</strong></div><div class="mini-bar"><div style="width:35%"></div></div><div class="note">Drain bằng 35% lượng tưới</div>', unsafe_allow_html=True)
        harvest.markdown(f'<div class="resource">Loại A <strong>{number(r["a"],2)} kg/m²</strong></div><div class="resource">Loại B <strong>{number(r["b"],2)} kg/m²</strong></div><div class="mini-bar"><div style="width:93.75%"></div></div><div class="note">A / (A + B) = 93,75%</div>', unsafe_allow_html=True)
        st.caption("Basis chung là giả định fixture · Không suy ra lượng nước cây hấp thụ.")
    with st.container(border=True,key="episodes_panel"):
        st.subheader("Các đợt ngoài dải")
        st.caption(f'{spec["label"]} · {HOUSES[house]} · {day}')
        for i,e in enumerate(summary["episodes"]):
            with st.expander(f'{e["start"][11:16]}–{e["end"][11:16]} · {e["minutes"]} phút · cao nhất {number(e["peak"],2)} {spec["unit"]}'):
                subset = [{"Thời gian nguồn": row["time"], spec["label"]:row[metric], "Quality":row[f"{metric}_quality"]} for row in item["rows"] if e["start"]<=row["time"]<e["end"]]
                st.dataframe(subset,hide_index=True,width="stretch")
                st.caption(f'Publication: {item["publication"]} · Event: {e["event_id"]}')
        if not summary["episodes"]:
            st.caption("Không có đợt ngoài dải trong fixture đang chọn.")
    with st.container(border=True,key="quality_panel"):
        st.subheader("Chất lượng trước kết luận",anchor="quality")
        st.caption("Thiếu dữ liệu không có nghĩa là giá trị bằng 0")
        st.progress(summary["coverage"]/100, text=f'{summary["valid_minutes"]//5} / 288 mẫu accepted · {number(summary["coverage"])}%')
        st.write(f'8 mẫu thiếu · {summary["suspect_count"]} mẫu nghi lỗi, loại khỏi baseline')
        with st.expander("Xem nguồn và phiên bản"):
            st.code(f'Scope: {item["scope"]}\nPublication: {item["publication"]}\nSource: synthetic-ui-fixture-v1\nRule: fixed-demo-band-v1\nTime basis: source-naive, chưa gắn timezone')
with right:
    with st.container(border=True,key="cost_panel"):
        st.subheader("Nếu giá điện thay đổi?",anchor="cost")
        st.caption("Giữ nguyên mức tiêu thụ, chỉ thay giá")
        pct = st.slider("Thay đổi cả hai giá điện (%)",-30,100,20,5,key="price_change")
        c = item["costs"][str(pct)]
        st.markdown(f'<div class="costnumber">{number(c["scenario"],2)} <small>EUR/m²</small></div><div class="note">Chênh lệch {c["delta"]:+.2f} EUR/m² · {c["delta_pct"]:+.2f}%</div>', unsafe_allow_html=True)
        st.plotly_chart(cost_figure(item,pct),theme=None,width="stretch",config={"displayModeBar":False},key="cost_chart")
        st.caption(f'Baseline: {number(c["baseline"],2)} EUR/m²')
        st.markdown('<p class="note">Nhiệt: 0,0083 EUR/MJ · điện: 0,08 / 0,04 EUR/kWh.<br/>Giá tham chiếu lịch sử; quantities và basis là fixture giả định. Kịch bản chưa lưu vào database.</p>', unsafe_allow_html=True)

st.markdown('<div class="foot">VERDANT / SMART GREENHOUSE · GROUP 29 &nbsp; — &nbsp; UI CONCEPT 01 · DỮ LIỆU MINH HỌA · CHẠY LOCAL</div>', unsafe_allow_html=True)
