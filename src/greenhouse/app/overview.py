"""Overview and quality inspection share the same pinned historical filters."""
import csv
from datetime import timedelta
from io import StringIO

import streamlit as st

from greenhouse.app import shared_context, wiring
from greenhouse.app import quality_reads as quality_overview


def render(connection, settings, publications, *, quality=False):
    catalog = wiring.catalog_service(connection)
    service = wiring.irrigation_service(connection, wiring.raw_snapshot_mapping(settings.raw_dir))
    usable = [p for p in publications if p.usable]
    if not usable:
        st.info('Chưa có publication với water mart để chọn vụ.'); st.stop()
    prefix = 'quality' if quality else 'overview'
    with st.sidebar:
        shared_context.adopt_publication(st.session_state, prefix + '_pub', usable, st.warning)
        selected = st.selectbox('Publication lịch sử', usable, format_func=wiring.publication_label,
                                key=prefix + '_pub')
    publication = wiring.resolve_publication(service, selected.publication_id)
    cycles = catalog.cycles(publication)
    if not cycles:
        st.info('Publication chưa có vụ.'); st.stop()
    if st.session_state.get(prefix + '_identity') != publication.publication_id:
        st.session_state[prefix + '_identity'] = publication.publication_id
        for name in ('_cycle', '_window'):
            st.session_state.pop(prefix + name, None)
    with st.sidebar:
        shared_context.adopt_cycle(st.session_state, prefix + '_cycle', cycles, st.warning)
        cycle = st.selectbox('Khoang · vụ', cycles, format_func=lambda c: f'{c.compartment_name} — {c.cycle_id}',
                             key=prefix + '_cycle')
        identity = (publication.publication_id, cycle.cycle_id, cycle.compartment_id)
        if st.session_state.get(prefix + '_cycle_identity') != identity:
            st.session_state[prefix + '_cycle_identity'] = identity
            st.session_state.pop(prefix + '_window', None)
        shared_context.adopt_window(st.session_state, prefix + '_window', cycle, st.warning)
        window = st.date_input('Khoảng ngày (cả hai đầu)', value=(cycle.start, cycle.end - timedelta(days=1)),
                               min_value=cycle.start, max_value=cycle.end - timedelta(days=1),
                               key=prefix + '_window')
        if not isinstance(window, (tuple, list)) or len(window) != 2:
            st.info('Chọn đủ hai đầu khoảng ngày.'); st.stop()
    shared_context.remember(st.session_state, publication, cycle, window)
    st.title('🛡️ Chất lượng dữ liệu' if quality else '🏠 Tổng quan vụ')
    st.caption(f'Lịch sử · scope {publication.scope_id} · snapshot {publication.snapshot_id} · '
               f'publication {publication.publication_id} · {cycle.compartment_name} · '
               f'{window[0]} → {window[1]} · quality {publication.versions.quality_policy_version}')
    if not quality:
        st.write('Chọn màn hình trong thanh bên để xem điều kiện vận hành, tài nguyên và sản xuất. '
                 'Các màn hình giữ publication, vụ và khoảng ngày khi cùng khả dụng.')
        st.dataframe([
            {'Nghiệp vụ': 'Khí hậu BR-01', 'Sẵn sàng': selected.climate_usable},
            {'Nghiệp vụ': 'Nước BR-02', 'Sẵn sàng': selected.usable},
            {'Nghiệp vụ': 'EC/pH BR-03', 'Sẵn sàng': selected.drain_usable},
            {'Nghiệp vụ': 'Năng lượng BR-04', 'Sẵn sàng': selected.energy_usable},
            {'Nghiệp vụ': 'Thu hoạch BR-05', 'Sẵn sàng': selected.harvest_usable},
            {'Nghiệp vụ': 'Chi phí BR-06', 'Sẵn sàng': selected.energy_usable},
        ], hide_index=True, width='stretch')
        st.info('Sẵn sàng chỉ phản ánh mart, không đảm bảo đủ dữ liệu hoặc rule. '
                'Sensor replay là scope riêng; kịch bản và ngưỡng demo có nhãn tại màn hình tương ứng.')
        return
    rows = quality_overview.measures(connection, publication.publication_id, cycle.cycle_id, *window)
    st.caption('Đếm quyết định chất lượng trên các revision đã publish; không phải độ phủ so với số quan sát kỳ vọng. '
               'Bản ghi quarantine chưa thuộc publication không nằm trong bảng này.')
    st.dataframe(rows, hide_index=True, width='stretch')
    with st.expander('Chi tiết measure bị loại hoặc chưa chắc chắn'):
        if not st.checkbox('Tải chi tiết measure và CSV', key='quality_load_details'):
            st.caption('Chọn để tải; tập kết quả có thể lớn trên toàn bộ vụ.')
        else:
            render_details(connection, publication, cycle, window)

    with st.expander('Quarantine, conflict và duplicate của lần nạp publication'):
        if not st.checkbox('Tải bản ghi loại và CSV', key='quality_load_quarantine'):
            st.caption('Chọn để tải các bản ghi loại của lần nạp publication.')
        else:
            render_quarantined(connection, publication, cycle, window)


def render_details(connection, publication, cycle, window):
    details = quality_overview.excluded(connection, publication.publication_id, cycle.cycle_id, *window)
    st.dataframe(details, hide_index=True, width='stretch')
    if details:
        stream = StringIO(newline='')
        writer = csv.DictWriter(stream, fieldnames=['publication_id', 'scope_id', 'snapshot_id',
                                                    'cycle_id', 'window_start', 'window_end', 'quality_policy_version', *details[0].keys()])
        writer.writeheader()
        for row in details:
            writer.writerow(dict(publication_id=publication.publication_id, scope_id=publication.scope_id,
                                 snapshot_id=publication.snapshot_id, cycle_id=cycle.cycle_id,
                                 window_start=window[0], window_end=window[1],
                                 quality_policy_version=publication.versions.quality_policy_version, **row))
        st.download_button('⬇ CSV chi tiết chất lượng', stream.getvalue(),
                           file_name='quality-details.csv', mime='text/csv')
    else:
        st.info('Không có quyết định measure bị loại hoặc chưa chắc chắn trong cửa sổ này.')


def render_quarantined(connection, publication, cycle, window):
    rejected = quality_overview.quarantined(connection, publication.publication_id, cycle.cycle_id, *window)
    st.caption('Chỉ gồm lần nạp gắn với publication; dòng thiếu thời gian được hiển thị dù không thể xác định thuộc khoảng ngày hay không.')
    st.dataframe(rejected, hide_index=True, width='stretch')
    if rejected:
        stream = StringIO(newline='')
        writer = csv.DictWriter(stream, fieldnames=['publication_id', 'scope_id', 'snapshot_id',
                                                    'cycle_id', 'window_start', 'window_end', *rejected[0].keys()])
        writer.writeheader()
        for row in rejected:
            writer.writerow(dict(publication_id=publication.publication_id, scope_id=publication.scope_id,
                                 snapshot_id=publication.snapshot_id, cycle_id=cycle.cycle_id,
                                 window_start=window[0], window_end=window[1], **row))
        st.download_button('⬇ CSV bản ghi loại', stream.getvalue(),
                           file_name='quality-quarantine.csv', mime='text/csv')
