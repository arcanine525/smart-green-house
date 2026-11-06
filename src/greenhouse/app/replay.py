"""Live replay view for BR-01/03; never presents source time as wall time."""
from datetime import datetime, timezone

import plotly.graph_objects as go
import psycopg
import streamlit as st

from greenhouse.app.replay_presenter import episode_rows, export_csv, liveness, summary_rows
from greenhouse.contracts import RuleBasis
from greenhouse.db.replay_catalog import current_replays, latest_quality, source_rules
from greenhouse.sensor.replay_evaluation import evaluate_publication, map_historical_rule
from greenhouse.sensor.replay_publication import read_publication


def _plot(events, category, field, title, unit):
    points = [(event['source_observed_at'], event['payload'][category].get(field)) for event in events]
    # Keep every point for exported analytics; sample only the rendered chart.
    stride = max(1, (len(points) + 2999) // 3000)
    shown = points[::stride]
    if points and shown[-1] != points[-1]:
        shown.append(points[-1])
    figure = go.Figure(go.Scattergl(x=[p[0] for p in shown], y=[float(p[1]) if p[1] is not None else None for p in shown],
                                   mode='lines+markers', connectgaps=False, name=field))
    figure.update_layout(title=title, xaxis_title='Thời gian nguồn (naive)', yaxis_title=unit,
                         margin=dict(l=10, r=10, t=45, b=10), height=260)
    st.plotly_chart(figure, width='stretch', key='replay-chart-' + field)
    if stride > 1:
        st.caption(f'Biểu đồ lấy mẫu mỗi {stride} điểm; KPI và CSV dùng toàn bộ publication.')


def render(connection, settings):
    st.title('📡 Sensor replay · BR-01 / BR-03')
    st.caption('Dữ liệu AGC được phát lại bằng thiết bị giả lập. Thời gian nguồn là lịch sử, không phải thời điểm đo thực.')
    auto = st.checkbox('Tự làm mới mỗi 10 giây', value=False, key='replay_auto_refresh')
    st.button('Làm mới ngay', key='replay_refresh')
    if auto:
        @st.fragment(run_every='10s')
        def refresh_view():
            # A fragment reruns after the enclosing script has closed its connection.
            try:
                with psycopg.connect(**settings.connection_kwargs()) as fresh:
                    _render_pinned(fresh)
            except psycopg.Error:
                st.error('Không kết nối được PostgreSQL; dữ liệu replay chưa sẵn sàng.')
        refresh_view()
    else:
        _render_pinned(connection)


def _render_pinned(connection):
    try:
        choices = current_replays(connection)
    except psycopg.Error:
        st.error('Không đọc được catalog replay; kiểm tra migration và PostgreSQL.')
        return
    if not choices:
        st.info('Chưa có replay publication. Chạy simulator, collector và stream worker để bắt đầu.')
        return
    selected = st.selectbox('Replay · khoang', choices,
        format_func=lambda row: f'{row[0]} · {row[4]} · {row[1][:10]}', key='replay_publication')
    replay_id, publication_id, scope, cycle, compartment, observed, received, state, created = selected
    # All subsequent reads and exports are pinned to this immutable ID.
    try:
        metadata, events = read_publication(connection, publication_id)
    except (ValueError, psycopg.Error):
        st.error('Publication replay đã chọn không đọc được.')
        return
    stale_seconds = st.slider('Ngưỡng mất tín hiệu (giây)', 10, 3600, 60, step=10, key='replay_stale_threshold')
    signal, age = liveness(received, datetime.now(timezone.utc), stale_seconds)
    left, middle, right = st.columns(3)
    left.metric('Trạng thái replay', state.get('replay_status', 'unknown'))
    middle.metric('Tín hiệu collector', {'active': 'Đang nhận', 'stale': 'Stale', 'unknown': 'Không rõ'}[signal])
    right.metric('Tuổi lần nhận gần nhất', f'{age:.0f} s' if age is not None else 'Không rõ')
    st.caption(f'Publication `{publication_id}` · Scope `{scope}` · Khoang `{compartment}` · Vụ `{cycle}`')
    st.caption(f'Replay clock (source): {observed or "Chưa có"} · Nhận gần nhất (UTC): {received or "Chưa có"}')
    st.caption('Tuổi tín hiệu đo từ lần collector nhận theo đồng hồ vận hành, không lấy hiện tại trừ thời gian AGC.')
    if state.get('complete'):
        st.success('Replay đã đối soát manifest, checksum và sequence.')
    else:
        st.warning('Provisional: replay chưa đối soát đầy đủ; kết quả và episode có thể thay đổi khi dữ liệu đến trễ.')
    st.write({'Đã xác nhận': state.get('accepted_events', 0), 'Thiếu sequence': len(state.get('missing_sequences', [])),
              'Conflict': len(state.get('conflicts', [])) + len(state.get('slot_conflicts', [])),
              'Quarantine': state.get('quarantined', 0), 'Lỗi scope': len(state.get('scope_conflicts', [])),
              'Lỗi thời gian': len(state.get('time_conflicts', []))})
    if not events:
        st.info('Chưa có measurement hợp lệ trong cửa sổ replay đã phát.')
        return
    latest = max(events, key=lambda item: item['source_observed_at'])
    st.subheader('Mẫu mới nhất trong replay')
    st.write({'Nguồn': latest['source_observed_at'], 'Thiết bị giả lập': latest['device_id'],
              'Event': latest['event_id'], 'Được phát (UTC)': latest['published_at'],
              'Null / quality nguồn': latest['payload'].get('null_reasons', {})})
    with st.expander('Chất lượng mẫu mới nhất'):
        try:
            st.dataframe(latest_quality(connection, metadata, latest), hide_index=True, width='stretch')
        except (ValueError, psycopg.Error):
            st.warning('Không đọc được verdict quality của mẫu này.')
    source_snapshot = latest['source_provenance']['snapshot_id']
    basis_label = st.selectbox('Basis khí hậu', ['requested', 'realized'], key='replay_climate_basis')
    try:
        climate_options = source_rules(connection, source_snapshot, compartment, 'climate', basis_label)
        drain_options = source_rules(connection, source_snapshot, compartment, 'drain_process_computer', 'measured')
        climate_versions = ['Không chọn rule'] + [r[0] for r in climate_options]
        drain_versions = ['Không chọn rule'] + [r[0] for r in drain_options]
        climate_labels = {r[0]: f'{r[0]} · {r[1]}' + (' · demo' if r[2] else '') for r in climate_options}
        drain_labels = {r[0]: f'{r[0]} · {r[1]}' + (' · demo' if r[2] else '') for r in drain_options}
        climate_id = st.selectbox('Rule khí hậu lịch sử ánh xạ sang replay', climate_versions,
                                  format_func=lambda value: climate_labels.get(value, value), key='replay_climate_rule')
        drain_id = st.selectbox('Rule EC/pH lịch sử ánh xạ sang replay', drain_versions,
                                format_func=lambda value: drain_labels.get(value, value), key='replay_drain_rule')
        climate_rule = map_historical_rule(connection, publication_id, climate_id) if climate_id != climate_versions[0] else None
        drain_rule = map_historical_rule(connection, publication_id, drain_id) if drain_id != drain_versions[0] else None
        evaluation = evaluate_publication(connection, publication_id, climate_rule=climate_rule,
            drain_rule=drain_rule, climate_basis=RuleBasis(basis_label))
    except (ValueError, psycopg.Error) as error:
        st.warning(f'Chưa thể tính KPI replay ({type(error).__name__}); kiểm tra rule, scope và cursor.')
        return
    st.subheader('KPI đến replay clock')
    st.caption(f'Cửa sổ source: {evaluation.climate.effective_start} đến {evaluation.window_end} (không gồm điểm cuối).')
    st.dataframe(summary_rows(evaluation), hide_index=True, width='stretch')
    st.caption('Không có rule hoặc giá trị hợp lệ: out KPI để trống. Coverage là tỷ lệ thời gian đủ dữ liệu và rule '
               'để đánh giá, không phải tỷ lệ dữ liệu nguồn có mặt; chưa chọn rule thì coverage đánh giá là 0%. '
               'Các rule demo không phải ngưỡng nông học đã xác minh.')
    st.subheader('Episode')
    episodes = episode_rows(evaluation)
    if episodes:
        st.dataframe(episodes, hide_index=True, width='stretch')
    else:
        st.info('Không có episode trong publication và rule đã chọn.')
    st.download_button('Tải KPI và episode CSV', export_csv(evaluation),
                       file_name=f'replay-{publication_id[:12]}.csv', mime='text/csv', key='replay_csv')
    st.subheader('Chuỗi measurement nguồn')
    st.caption('Biểu đồ hiển thị giá trị nguồn; các verdict quality và coverage được áp dụng trong KPI ở trên.')
    for category, field, title, unit in (
        ('actual_measurements', 'Tair', 'Nhiệt độ', '°C'),
        ('actual_measurements', 'CO2air', 'CO₂', 'ppm'),
        ('drain_measurements', 'EC_drain_PC', 'EC drain', 'dS/m'),
        ('drain_measurements', 'pH_drain_PC', 'pH drain', 'pH'),
    ):
        _plot(events, category, field, title, unit)
