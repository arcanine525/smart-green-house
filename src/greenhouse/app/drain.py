"""BR-03: explicit immutable profiles, bounded traces and daily water context."""
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from functools import partial

import psycopg
import streamlit as st

from greenhouse.app import display, exports, figures, wiring
from greenhouse.app import drain_actions as actions, drain_exports as downloads
from greenhouse.contracts import RuleBasis
from greenhouse.db.drain_profiles import save_drain_profile
from greenhouse.queries import episode_key, page_episodes

EPISODE_PAGE_SIZE = 40
EPISODE_EXPORT_LIMIT = 500
MIN_OPTIONS = (Decimal('0'), Decimal('15'), Decimal('60'), Decimal('1440'))


def _number(value):
    return 'NULL' if value is None else display.format_decimal(value)


def _reset(*keys):
    for key in keys:
        st.session_state.pop('drain_' + key, None)


def _move(key, offset):
    st.session_state['drain_' + key] = max(0, offset)


def _episode_label(episode):
    return (f'{episode.metric} · {episode.original_minutes} phút gốc · '
            f'{episode.original_start.isoformat()} → {episode.original_end.isoformat()} · '
            f'trong kỳ {episode.clipped_minutes} phút · peak {episode.peak_deviation} '
            f'@ {episode.peak_time.isoformat()} · '
            + '/'.join(direction.value for direction in episode.directions))


def _profile_form(connection, publication, cycle):
    with st.expander('Tạo version profile mới (kỹ sư, chưa xác minh nông học)'):
        st.caption('Lưu bất biến cho cả vụ theo giờ nguồn; giữ nguyên version cũ. '
                   'Các cận gợi ý là minh họa, chưa được hiệu chuẩn nông học. '
                   'Đổi profile không tạo publication hay sửa phép đo.')
        with st.form('drain_profile_form'):
            name = st.text_input('Tên profile', value='engineer', key='drain_profile_name')
            version = st.number_input('Version mới', min_value=1, value=1, step=1,
                                      key='drain_profile_version')
            ec_min = st.text_input('EC cận dưới (dS/m)', value='2', key='drain_ec_lower')
            ec_max = st.text_input('EC cận trên (dS/m)', value='5', key='drain_ec_upper')
            ph_min = st.text_input('pH cận dưới', value='5.5', key='drain_ph_lower')
            ph_max = st.text_input('pH cận trên', value='6.5', key='drain_ph_upper')
            provenance = st.text_input('Nguồn / lý do cho version này', key='drain_profile_evidence')
            submitted = st.form_submit_button('Lưu version bất biến', key='drain_save_profile')
        if submitted:
            try:
                revision = save_drain_profile(
                    connection, publication, cycle, name=name, version=int(version),
                    ec_lower=ec_min, ec_upper=ec_max, ph_lower=ph_min, ph_upper=ph_max,
                    provenance=provenance)
                connection.commit()  # Seal before the rerun/connection close.
            except (ValueError, psycopg.Error) as error:
                connection.rollback()
                st.error('Không lưu profile: ' + str(error)[:220])
            else:
                st.session_state['drain_pending_rule'] = revision.rule_version
                st.session_state['drain_profile_saved'] = revision.rule_version
                st.rerun()


def _summary(evaluation):
    st.title('EC/pH dung dịch drain')
    st.caption('drain_process_computer · measured · thời gian nguồn chưa xác minh timezone. '
               'Ngưỡng kinh doanh tách khỏi quality policy; không suy ra chẩn đoán dinh dưỡng.')
    st.caption(f'Publication {evaluation.publication.publication_id} · '
               f'quality {evaluation.publication.versions.quality_policy_version} · '
               f'profile {evaluation.requested_rule_version or "rule_not_configured"}')
    if evaluation.requested_rule_version is None:
        st.info('rule_not_configured — chưa chọn profile; KPI ngoài dải là NULL. '
                'Vẫn có thể xem phép đo, quality và lineage bên dưới.')
    else:
        label = 'Ngưỡng minh họa của đồ án' if evaluation.rule_is_demo else 'Profile do kỹ sư nhập'
        st.warning(label + ' — chưa xác minh/hiệu chuẩn nông học cho giống cây hoặc giai đoạn này.')
        st.caption(f'Provenance: {evaluation.rule_evidence_reference}')
    for report in evaluation.result.metric_results:
        columns = st.columns(4)
        columns[0].metric(f'{report.metric.upper()} ngoài dải (phút)',
                          display.format_decimal(report.out_minutes)
                          if report.out_percent is not None else 'NULL')
        columns[1].metric(f'{report.metric.upper()} ngoài dải %',
                          _number(report.out_percent))
        columns[2].metric(f'{report.metric.upper()} coverage %',
                          display.format_decimal(report.coverage_pct))
        columns[3].metric(f'{report.metric.upper()} suspect (phút)',
                          display.format_decimal(report.suspect_minutes))
        st.caption(f'{report.metric}: valid {report.valid_minutes} phút · '
                   f'unknown {report.unknown_minutes} · low {report.out_low_minutes} · '
                   f'high {report.out_high_minutes} · '
                   + '; '.join(f'{share.reason_code}={share.minutes}' for share in report.unknown_reasons))
    joint = evaluation.result.joint_results[0]
    cols = st.columns(3)
    cols[0].metric('OR ngoài dải (phút, hợp lệ chung)',
                   display.format_decimal(joint.joint_out_minutes)
                   if joint.joint_out_percent is not None else 'NULL')
    cols[1].metric('OR ngoài dải %', _number(joint.joint_out_percent))
    cols[2].metric('Coverage giao chung %', display.format_decimal(joint.joint_coverage_pct))
    st.caption(f'OR trên giao EC/pH hợp lệ: {joint.jointly_valid_minutes} phút; '
               'không cộng hai thời lượng riêng. Bộ lọc đợt không đổi các tổng này.')


def _episode_list(evaluation, metric, minimum):
    selection_scope = (evaluation.publication.publication_id, evaluation.context,
                       evaluation.requested_rule_version, metric, minimum)
    if st.session_state.get('drain_list_scope') != selection_scope:
        st.session_state['drain_list_scope'] = selection_scope
        _reset('page_offset', 'episode_choice')
    page_offset = st.session_state.get('drain_page_offset', 0)
    page, total = page_episodes(evaluation.result, metric=metric, min_minutes=minimum,
                                limit=EPISODE_PAGE_SIZE, offset=page_offset,
                                metric_order=('ec', 'ph'))
    st.subheader('Đợt ngoài dải — dài nhất trước')
    st.caption('Giữ thời điểm/toàn bộ thời lượng GỐC và phần giao CLIPPED với kỳ báo cáo. '
               'Metric và thời lượng tối thiểu chỉ lọc danh sách, không thay tổng KPI.')
    nav = st.columns(3)
    nav[0].button('◀ Đợt trước', key='drain_prev_page', disabled=page_offset == 0,
                  on_click=_move, args=('page_offset', page_offset - EPISODE_PAGE_SIZE))
    nav[1].button('Đợt sau ▶', key='drain_next_page',
                  disabled=page_offset + EPISODE_PAGE_SIZE >= total,
                  on_click=_move, args=('page_offset', page_offset + EPISODE_PAGE_SIZE))
    st.caption(f'Đợt: {len(page)}/{total} sau lọc · offset {page_offset}.')
    if not page:
        _reset('selected_key', 'episode_choice')
        st.info('Không có đợt sau bộ lọc. Tra cứu nguồn và nước theo kỳ vẫn dùng được.')
        return None, total
    keys = [episode_key(episode) for episode in page]
    by_key = dict(zip(keys, page))
    if st.session_state.get('drain_episode_choice') not in keys:
        remembered = st.session_state.get('drain_selected_key')
        st.session_state['drain_episode_choice'] = remembered if remembered in keys else keys[0]
    chosen = st.selectbox('Chọn đợt để xem chi tiết', keys,
                          format_func=lambda key: _episode_label(by_key[key]), key='drain_episode_choice')
    st.session_state['drain_selected_key'] = chosen
    return by_key[chosen], total


def _trace(service, evaluation, episode):
    if episode is None:
        _reset('trace_identity', 'trace_offset')
        return None
    start, end = actions.clipped_span(evaluation.context, episode.original_start, episode.original_end)
    identity = (evaluation.context, evaluation.requested_rule_version, episode_key(episode))
    if st.session_state.get('drain_trace_identity') != identity:
        st.session_state['drain_trace_identity'] = identity
        _reset('trace_offset')
    trace, offset, total = actions.trace_page(service, evaluation.context,
        evaluation.requested_rule_version, start, end, st.session_state.get('drain_trace_offset', 0))
    st.subheader('Chi tiết đợt — trang hữu hạn trước khi đọc dữ liệu')
    st.caption(f'Gốc [{episode.original_start.isoformat()},{episode.original_end.isoformat()}) '
               f'= {episode.original_minutes} phút; trong kỳ {episode.clipped_minutes} phút. '
               f'Trang [{trace.start.isoformat()},{trace.end.isoformat()}) · '
               f'{trace.window_slots} mốc / {total} · offset {offset}; tối đa '
               f'{actions.TRACE_PAGE_SLOTS} mốc 5 phút mỗi lượt, không cắt tổng KPI.')
    nav = st.columns(2)
    nav[0].button('◀ Trace trước', key='drain_trace_prev', disabled=offset == 0,
                  on_click=_move, args=('trace_offset', offset - actions.TRACE_PAGE_SLOTS))
    nav[1].button('Trace sau ▶', key='drain_trace_next',
                  disabled=offset + actions.TRACE_PAGE_SLOTS >= total,
                  on_click=_move, args=('trace_offset', offset + actions.TRACE_PAGE_SLOTS))
    facts = service.slot_facts(evaluation.context, window_start=trace.start,
                               window_end=trace.end, max_slots=actions.TRACE_PAGE_SLOTS)
    for metric in ('ec', 'ph'):
        st.plotly_chart(figures.drain_figure(trace.reviews, metric, facts.rows),
                        width='stretch', config={'displayModeBar': False}, key='drain_chart_' + metric)
    st.caption('Dải tô là cận được áp dụng; unknown tạo khoảng trống, dấu x xám '
               'giữ giá trị nguồn suspect/invalid/chưa có rule, không tô thành đạt.')
    scoped = replace(evaluation, slot_reviews=trace.reviews)
    st.dataframe(exports.climate_detail_rows(scoped), width='stretch', hide_index=True)
    return trace


def _source(service, evaluation):
    st.subheader('Tra cứu nguồn độc lập với danh sách đợt')
    context = evaluation.context
    left, right = st.columns(2)
    day = left.date_input('Ngày nguồn', min_value=context.start,
        max_value=context.end - timedelta(days=1), value=context.start, key='drain_source_day',
        on_change=_move, args=('source_offset', 0))
    clock = right.time_input('Giờ nguồn (5 phút)', value=time.min, step=300,
        key='drain_source_time', on_change=_move, args=('source_offset', 0))
    if day is None or clock is None:
        st.info('Chọn đủ ngày và giờ nguồn để tra cứu.')
        st.stop()
    start = datetime.combine(day, clock)
    report_end = datetime.combine(context.end, time.min)
    offset = st.session_state.get('drain_source_offset', 0)
    start += offset * actions.CADENCE
    facts = service.slot_facts(context, window_start=start, window_end=report_end,
                               max_slots=actions.SOURCE_PAGE_SLOTS)
    st.caption(f'Nguồn [{facts.start.isoformat()},{facts.end.isoformat()}) · '
               f'{len(facts.rows)} hàng gốc; giữ EC âm/pH=0 và quality riêng; không dùng lab/giá thể.')
    nav = st.columns(2)
    nav[0].button('◀ Nguồn trước', key='drain_source_prev', disabled=offset == 0,
                  on_click=_move, args=('source_offset', offset - actions.SOURCE_PAGE_SLOTS))
    nav[1].button('Nguồn sau ▶', key='drain_source_next', disabled=not facts.truncated,
                  on_click=_move, args=('source_offset', offset + actions.SOURCE_PAGE_SLOTS))
    st.dataframe(downloads.source_rows(facts), width='stretch', hide_index=True)
    # A small raw chart is present even with no rule and no OUT episode.
    trace = service.get_slot_review(context, rule_version=evaluation.requested_rule_version,
        window_start=facts.start, window_end=facts.end, max_slots=actions.SOURCE_PAGE_SLOTS)
    for metric in ('ec', 'ph'):
        st.plotly_chart(figures.drain_figure(trace.reviews, metric, facts.rows),
                        width='stretch', key='drain_raw_chart_' + metric)
    if facts.rows:
        by_id = {row.revision_id: row for row in facts.rows}
        if st.session_state.get('drain_source_revision') not in by_id:
            _reset('source_revision')
        selected = st.selectbox('Dòng nguồn để truy vết', list(by_id),
            format_func=lambda key: by_id[key].slot_start.isoformat(), key='drain_source_revision')
        if st.button('Xác minh lineage về raw', key='drain_verify_lineage'):
            try:
                lineage = service.get_lineage(evaluation.publication, revision_id=selected)
                st.session_state['drain_lineage'] = (evaluation.publication.publication_id, selected, lineage)
            except (ValueError, OSError, psycopg.Error) as error:
                st.warning('Chưa xác minh được raw: ' + str(error)[:160])
        saved = st.session_state.get('drain_lineage')
        if saved and saved[:2] == (evaluation.publication.publication_id, selected):
            lineage = saved[2]
            st.write(f'File: {lineage.relative_path} · dòng {lineage.line_number}–{lineage.line_end} · '
                     f'serial {lineage.source_time_serial} · timestamp nguồn {lineage.source_timestamp}')
            st.caption(f'Revision {lineage.revision_id} · correction {lineage.correction_id or "—"}')
            if lineage.raw_hash_verified:
                st.success('Raw hash xác minh khớp; số đo gốc và quality được truy vết độc lập.')
            else:
                st.warning('Raw hash chưa xác minh trên mount này; không coi là đã kiểm chứng.')
    return facts


def _water(service, evaluation, episode):
    st.subheader('Bối cảnh tưới/drain theo ngày — BR-02')
    water = actions.daily_water_context(service, evaluation,
        episode.original_start if episode else None, episode.original_end if episode else None)
    summary = water.action.summary
    st.caption(f'Cùng publication/cycle; giao [{water.overlap_start.isoformat()},'
               f'{water.overlap_end.isoformat()}). Một hàng/ngày nguồn; ngày chỉ giao một phần '
               'vẫn hiển thị TOÀN lượng ngày, không prorate, không nhân theo slot/đợt. '
               'Đối chiếu để điều tra, chưa kết luận nguyên nhân.')
    cols = st.columns(3)
    cols[0].metric('Tưới nguồn (L/m², ngày tính một lần)',
                   _number(summary.irrigation_observed_l_m2))
    cols[1].metric('Drain nguồn (L/m², ngày tính một lần)',
                   _number(summary.drain_observed_l_m2))
    cols[2].metric('Coverage nước ghép ngày %', display.format_decimal(summary.paired_coverage_pct))
    if summary.basis_status.value != 'verified':
        st.warning(f'Basis nước: {summary.basis_status.value} · {summary.basis_label or "chưa xác minh"}')
    rows = exports.day_rows(water.action.days)
    partial_dates = {day.isoformat() for day in water.partial_dates}
    for row in rows:
        row['partial_overlap_full_day'] = row['resource_date'] in partial_dates
    st.dataframe(rows, width='stretch', hide_index=True)
    return water


def render(connection, settings, publications):
    catalog = wiring.catalog_service(connection)
    usable = catalog.drain_usable(publications)
    if not usable:
        st.info('Publication hiện tại chưa có drain-mart-v1. Chạy workflow mới có drain bundle; '
                'màn hình không tự publish/backfill hoặc dùng core thay thế.')
        return
    service = wiring.drain_service(connection, wiring.raw_snapshot_mapping(settings.raw_dir))
    with st.sidebar:
        st.caption('MÀN HÌNH EC/pH DRAIN · BR-03')
        selected = st.selectbox('Publication drain', usable,
            format_func=lambda row: f'{row.scope_id} · {row.publication_id[:12]} · drain-mart-v1',
            key='drain_pub_choice')
        publication = service.adapter.publication(selected.publication_id)
        cycles = catalog.cycles(publication)
        if st.session_state.get('drain_pub_key') != publication.publication_id:
            st.session_state['drain_pub_key'] = publication.publication_id
            _reset('cycle_choice')
        cycle = st.selectbox('Khoang · vụ drain', cycles,
            format_func=lambda row: f'{row.compartment_name} — {row.cycle_id}', key='drain_cycle_choice')
        active = (publication.publication_id, cycle.cycle_id)
        if st.session_state.get('drain_active') != active:
            st.session_state['drain_active'] = active
            _reset('window', 'rule_choice', 'pending_rule', 'selected_key', 'source_revision')
        window = st.date_input('Khoảng ngày drain (cả hai đầu)',
            min_value=cycle.start, max_value=cycle.end - timedelta(days=1),
            value=(cycle.start, cycle.end - timedelta(days=1)), key='drain_window', format='DD/MM/YYYY')
        if not isinstance(window, (tuple, list)) or len(window) != 2:
            st.info('Chọn đủ hai đầu của khoảng ngày.')
            return
        options = catalog.schedule_rules(selected, cycle.compartment_id,
            measurement_context='drain_process_computer', metrics=('ec', 'ph'), bases=(RuleBasis.MEASURED,))
        labels = {None: '(không chọn — rule_not_configured)'}
        labels.update({option.rule_version: f'{option.rule_version} · {option.label} · v{option.version}'
                       for option in options})
        pending = st.session_state.pop('drain_pending_rule', None)
        if pending in labels and pending is not None:
            st.session_state['drain_rule_choice'] = pending
        if st.session_state.get('drain_rule_choice') not in labels:
            _reset('rule_choice')
        rule = st.selectbox('Profile EC/pH (version tường minh)', list(labels),
                            format_func=labels.__getitem__, key='drain_rule_choice')
        _profile_form(connection, publication, cycle)
        metric = st.radio('Metric lọc đợt drain', ('Cả hai', 'ec', 'ph'), key='drain_metric_filter')
        minimum = st.select_slider('Thời lượng tối thiểu (chỉ danh sách)',
            options=MIN_OPTIONS, value=Decimal('0'), key='drain_min_filter')
    context = wiring.build_context(publication, cycle, window[0], window[1], rule)
    view = (context, rule)
    if st.session_state.get('drain_view') != view:
        st.session_state['drain_view'] = view
        _reset('source_day', 'source_time', 'source_offset', 'source_revision', 'lineage',
               'selected_key', 'trace_identity', 'trace_offset')
    try:
        evaluation = service.get_evaluation(context, rule_version=rule)
        _summary(evaluation)
        episode, total = _episode_list(evaluation, None if metric == 'Cả hai' else metric, minimum)
        trace = _trace(service, evaluation, episode)
        facts = _source(service, evaluation)
        water = _water(wiring.irrigation_service(connection), evaluation, episode)
    except (ValueError, psycopg.Error) as error:
        st.error('Không đọc được view đã pin: ' + str(error)[:220])
        return
    st.subheader('Xuất kết quả đúng view đã pin')
    stem = f'drain-{cycle.compartment_name}-{context.start.isoformat()}-{context.end.isoformat()}'
    shown, _ = page_episodes(evaluation.result, metric=None if metric == 'Cả hai' else metric,
        min_minutes=minimum, limit=EPISODE_EXPORT_LIMIT, metric_order=('ec', 'ph'))
    for label, data, suffix, mime in (
        ('CSV summary drain', partial(downloads.summary_csv, evaluation), 'summary.csv', 'text/csv'),
        ('CSV joint OR drain', partial(downloads.joint_csv, evaluation), 'joint.csv', 'text/csv'),
        ('CSV đợt drain (đã lọc)', partial(downloads.episodes_csv, evaluation, shown,
            total=total, metric=None if metric == 'Cả hai' else metric, min_minutes=minimum), 'episodes.csv', 'text/csv'),
        ('JSON result drain (typed)', partial(downloads.result_json, evaluation), 'result.json', 'application/json'),
        ('CSV nước theo ngày', partial(downloads.water_csv, evaluation, water), 'water.csv', 'text/csv'),
        ('CSV nguồn drain (trang)', partial(downloads.source_csv, evaluation, facts), 'source.csv', 'text/csv'),
    ):
        # Streamlit's installed deferred-data API: generate from these immutable
        # DTOs on click, without retaining large JSON/CSV buffers on every rerun.
        st.download_button(label, data, file_name=f'{stem}-{suffix}', mime=mime, on_click='ignore')
    if trace is not None:
        st.download_button('CSV detail drain (trang)', partial(downloads.detail_csv, evaluation, trace),
                            file_name=f'{stem}-detail.csv', mime='text/csv', on_click='ignore')
