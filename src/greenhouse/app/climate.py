"""Climate screen (BR-01) for the production app.

Same action discipline as the other screens: catalog → pin the publication by
ID once → typed evaluations from the shared service. The declared basis
(requested/realized) is ONE evaluation per view, never coalesced; a rule
schedule revision is chosen explicitly (never latest; unknown stays unknown).

Episode list: user-controlled metric filter, user-selectable minimum duration
(default 15-minute demo option, but 0/"tất cả" is supported), and REAL paging
(offset navigation) — no hardcoded first page. The selected episode is
re-resolved by its STABLE key (metric + original span + rule version) against
the CURRENT filtered page after every rule/basis/window/filter change; a
vanished selection falls back to the current page's first row (T13 stale-
selection lesson). Unknown slot windows stay gaps, never green.

Detail/chart runs only on the SELECTED episode window (bounded reruns).
Exports reuse the same DTOs; the episode CSV labels total_after_filter and
truncation explicitly, and the episode-scoped detail download names its scope
in the file name and metadata. Lineage uses the frozen source revision
identity of the slots.
"""
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal

import psycopg
import streamlit as st

from greenhouse.app import display, exports, figures, wiring
from greenhouse.contracts import QueryContext, RuleBasis
from greenhouse.queries import episode_key, page_episodes

EPISODE_PAGE_SIZE = 40
EPISODE_EXPORT_LIMIT = 500
# Bounded detail: traces longer than this are capped in the UI (chart/table)
# and the export CSV labels exported/available counts exactly; the engine
# result and summary totals are always the FULL unbounded computation.
DETAIL_TRACE_ROWS_LIMIT = 2000
DEMO_MIN_MINUTES = Decimal('15')  # demo option (BR-01 §4.3); the user may pick 0/all
CLIMATE_UNITS = {'temperature': '°C', 'co2': 'ppm'}
MIN_OPTIONS = {
    Decimal('0'): 'Tất cả (0 phút — không lọc)',
    DEMO_MIN_MINUTES: '15 phút (demo BR-01 §4.3, mặc định)',
    Decimal('30'): '30 phút',
    Decimal('60'): '60 phút',
}


def _rule_option_label(option):
    keys = ', '.join(f'{entry[0]}/{entry[2].value}' for entry in option.keys)
    tag = 'demo_rule' if option.is_demo else option.label
    return f'{option.rule_version} · {tag} · {keys} · v{option.version}'


def _evaluation_label(evaluation, reports):
    coverage = ', '.join(f'{report.metric} {display.format_decimal(report.coverage_pct)}%'
                         for report in reports)
    joint = evaluation.result.joint_results
    joint_text = ''
    if joint:
        row = joint[0]
        joint_text = (f' · OR {display.format_decimal(row.joint_out_minutes)} phút trên '
                      f'{display.format_decimal(row.jointly_valid_minutes)} phút jointly known '
                      f'({display.format_decimal(row.joint_coverage_pct)}% coverage chung)')
    effective = evaluation.result
    return (f'basis {evaluation.basis.value} · rule {evaluation.requested_rule_version or "—"} · '
            f'cửa sổ hiệu lực [{effective.effective_start.isoformat()}, '
            f'{effective.effective_end.isoformat()}) · coverage {coverage}{joint_text}')


def _trace_fetch_window(episode, context):
    """The window the trace actually reads: the intersection of the episode's
    ORIGINAL window and the effective report view (the service clamps exactly
    the same way, so the label must match the read, never claim more)."""
    view_start = datetime.combine(context.start, time.min)
    view_end = datetime.combine(context.end, time.min)
    return max(episode.original_start, view_start), min(episode.original_end, view_end)


def _episode_label(index, episode):
    direction = ', '.join(item.value for item in episode.directions) or '—'
    clipped = (f'trong báo cáo {display.format_decimal(episode.clipped_minutes)}p'
               if episode.clipped_minutes is not None else '—')
    return (f'#{index + 1} · {episode.metric} · gốc {display.format_decimal(episode.original_minutes)}p '
            f'({episode.original_start.isoformat()} → {episode.original_end.isoformat()}) '
            f'· {direction} · peak {display.format_decimal(episode.peak_deviation)} '
            f'@ {episode.peak_time.isoformat()} · {clipped}')


SOURCE_INSPECT_ROWS = 12  # bounded source-inspection page (main T15 preflight item)


def _quality_note(measure_quality):
    """Compact frozen quality triplets per measure (status/applicability/
    reason). These are the ORIGINAL verdicts — evaluation-input masking in
    the summary/detail view never rewrites this frozen content."""
    parts = []
    for code, entry in (measure_quality or {}).items():
        parts.append(f"{code} {entry.get('status')}/{entry.get('applicability')}"
                     + (f" ({entry.get('reason')})" if entry.get('reason') else ''))
    return '; '.join(parts)[:240]


def _episode_page_keys(page):
    """Selectable options are the STABLE KEYS of the current filtered page,
    so a selection survives reordering (T13 lesson)."""
    return [episode_key(episode) for episode in page]


def render(connection, settings, publications):
    catalog = wiring.catalog_service(connection)
    raw_snapshots = wiring.raw_snapshot_mapping(settings.raw_dir)
    service = wiring.climate_service(connection, raw_snapshots)

    climate_rows = [row for row in publications if row.climate_usable]
    with st.sidebar:
        st.markdown('<div class="brand">smart<em> greenhouse</em></div>', unsafe_allow_html=True)
        st.caption('MÀN HÌNH KHÍ HẬU · BR-01')
        if not climate_rows:
            st.error('Không có publication nào đủ climate mart (climate-mart-v1) để xem.')
            st.caption('Publication hiện hành của scope thiếu climate bundle. Chạy một '
                       'workflow pipeline mới (v4); ứng dụng không tự publish/backfill.')
            st.code('docker compose -p smart-greenhouse run --rm job '
                    'python -m greenhouse run-pipeline --workflow-id climate-br01',
                    language='bash')
            st.stop()
        selected = st.selectbox('Publication (scope đã publish)', climate_rows,
                                format_func=wiring.climate_publication_label,
                                key='clim_pub_choice')
        st.markdown('<div class="side-note">Requested/realized là HAI lần đánh giá riêng '
                    '(không fallback ngầm). Chưa chọn revision rule thì mọi metric là '
                    'unknown; một revision có thể chỉ phủ một phần metric.</div>',
                    unsafe_allow_html=True)

    try:
        publication = service.adapter.publication(selected.publication_id)
    except ValueError as error:
        st.error('Publication đã chọn không đọc được qua climate adapter mặc định.')
        st.caption(f'{type(error).__name__}: {error} · Không tự publish lại.')
        st.stop()

    cycles = catalog.cycles(selected)
    if not cycles:
        st.error('Publication này chưa có cycle/khoang nào trong climate mart.')
        st.stop()

    if st.session_state.get('clim_pub_key') != publication.publication_id:
        st.session_state['clim_pub_key'] = publication.publication_id
        st.session_state.pop('clim_cycle_choice', None)

    with st.sidebar:
        cycle = st.selectbox('Khoang · vụ', cycles,
                             format_func=lambda c: f'{c.compartment_name} — {c.cycle_id}',
                             key='clim_cycle_choice')
        active_key = f'{publication.publication_id}|{cycle.cycle_id}|{cycle.compartment_id}'
        if st.session_state.get('clim_active_key') != active_key:
            st.session_state['clim_active_key'] = active_key
            for widget in ('clim_window', 'clim_basis', 'clim_rule_choice',
                           'clim_selected_key', 'clim_episode_choice',
                           'clim_trace_offset', 'clim_metric_filter',
                           'clim_min_filter', 'clim_page_offset'):
                st.session_state.pop(widget, None)
        cycle_last_inclusive = cycle.end - timedelta(days=1)
        window = st.date_input('Khoảng ngày (cả hai đầu, theo ngày nguồn)', key='clim_window',
                               min_value=cycle.start, max_value=cycle_last_inclusive,
                               value=(cycle.start, cycle_last_inclusive), format='DD/MM/YYYY')
        if isinstance(window, (tuple, list)) and len(window) == 2:
            start_inclusive, end_inclusive = window
        else:
            st.info('Khoảng ngày chưa đủ hai đầu — chọn lại ngày bắt đầu và ngày kết thúc.')
            st.stop()
        basis = st.radio('Basis điều kiện', (RuleBasis.REQUESTED, RuleBasis.REALIZED), index=0,
                         format_func=lambda b: ('Requested (t_heat_sp/t_vent_sp/co2_sp)'
                                                if b == RuleBasis.REQUESTED
                                                else 'Realized (VIP: t_heat_vip/t_ventlee_vip/co2_vip)'),
                         key='clim_basis')
        options = catalog.schedule_rules(selected, cycle.compartment_id)
        labels = {None: '(không chọn — rule_not_configured)'}
        for option in options:
            labels[option.rule_version] = _rule_option_label(option)
        rule_label = st.selectbox('Revision rule tường minh (tối đa một revision cho mỗi metric)',
                                  [None, *(option.rule_version for option in options)],
                                  format_func=lambda value: labels.get(value, value),
                                  key='clim_rule_choice')
        option_row = next((option for option in options if option.rule_version == rule_label),
                          None) if rule_label else None
        if option_row is None:
            st.markdown('<p class="rule-note">Rule kiểm tra: <b>rule_not_configured</b> — '
                        'chưa chọn revision; mọi slot là unknown, không có kết luận theo '
                        'ngưỡng nào (không ngầm dùng ngưỡng mặc định).</p>',
                        unsafe_allow_html=True)
        else:
            if option_row.is_demo:
                st.markdown('<div class="assumption">Demo rule: cấu hình tổng hợp cho đồ án, '
                            'không phải ngưỡng nông học đã phê duyệt của AGC '
                            '(BR-01 §4.1).</div>', unsafe_allow_html=True)
            st.caption(f"Evidence: {option_row.evidence_reference}")
        metric_filter = st.radio('Metric lọc danh sách đợt', ('Cả hai', 'temperature', 'co2'),
                                 format_func=lambda value: ('Cả hai (mặc định)'
                                                            if value == 'Cả hai' else value),
                                 key='clim_metric_filter')
        min_choice = st.select_slider('Thời lượng đợt tối thiểu trong danh sách',
                                      options=list(MIN_OPTIONS),
                                      format_func=MIN_OPTIONS.__getitem__,
                                      value=DEMO_MIN_MINUTES, key='clim_min_filter')
        st.caption('Bộ lọc metric/thời lượng CHỈ tác động danh sách/ex đợt này; tổng giờ '
                   'out và coverage phía trên/lịch tính từ cùng kết quả, chưa qua bộ lọc.')

    if end_inclusive < cycle.start or start_inclusive > cycle_last_inclusive:
        st.warning('Cửa sổ ngày không giao với vụ đã chọn.')
        st.caption(f'Vụ {cycle.cycle_id} kéo dài {cycle.start.isoformat()} → '
                   f'{cycle_last_inclusive.isoformat()}.')
        st.stop()

    start_date, end_date = wiring.context_window(start_inclusive, end_inclusive)
    context = QueryContext(publication.scope_id, publication.publication_id,
                           publication.snapshot_id, cycle.cycle_id,
                           (cycle.compartment_id,), start_date, end_date)

    try:
        evaluation = service.get_evaluation(context, basis=basis, rule_version=rule_label)
    except ValueError as error:
        st.error('Đánh giá climate không thực hiện được với bộ lọc hiện tại.')
        st.caption(f'{type(error).__name__}: {error}')
        st.stop()
    except psycopg.Error:
        st.error('Lỗi PostgreSQL khi đọc climate mart (không hiển thị chi tiết cấu hình).')
        st.stop()

    result = evaluation.result
    reports = result.metric_results
    metric_value = None if metric_filter in ('Cả hai', None) else metric_filter
    min_value = None if min_choice == 0 else min_choice
    filtered_total = sum(1 for episode in result.episodes
                         if (metric_value is None or episode.metric == metric_value)
                         and (min_value is None or episode.original_minutes >= min_value))
    page_offset = st.session_state.get('clim_page_offset', 0)
    if page_offset and page_offset >= max(filtered_total, 1):
        page_offset = 0  # the filter shrank the list; restart pagination honestly
    page, _ = page_episodes(result, metric=metric_value, min_minutes=min_value,
                            limit=EPISODE_PAGE_SIZE, offset=page_offset)

    st.title('🌡️ Khí hậu · các đợt lệch điều kiện')
    st.markdown('<p class="subtitle">Actual so với điều kiện đã chọn (requested hoặc realized '
                'riêng); điều kiện theo từng metric; độ lệch/thời lượng là ước lượng theo '
                'cadence 5 phút trên nguồn chưa xác minh timezone.</p>', unsafe_allow_html=True)
    st.markdown(
        f'<span class="chip">scope: {publication.scope_id}</span>'
        f'<span class="chip">snapshot: {publication.snapshot_id[:24]}…</span>'
        f'<span class="chip">publication: {publication.publication_id[:16]}…</span>'
        f'<span class="chip">sealed {publication.committed_at.astimezone().strftime("%d/%m/%Y %H:%M")}'
        f'</span><span class="chip">bundle climate-mart-v1</span>'
        f'<span class="chip">basis {evaluation.basis.value}</span>'
        f'<span class="chip">code {publication.versions.code_version}</span>',
        unsafe_allow_html=True)
    st.markdown(f'<p class="rule-note">{_evaluation_label(evaluation, reports)}</p>',
                unsafe_allow_html=True)
    for report in reports:
        if report.out_hours is None:
            st.markdown(f'<p class="rule-note">⚠️ <b>{report.metric}</b>: chưa có slot hợp lệ '
                        '(valid 0) — out-hours/percent công bố NULL, KHÔNG hiển thị "0 giờ, '
                        'đạt".</p>', unsafe_allow_html=True)

    temperature, co2 = reports
    kpis = st.columns(4)
    kpis[0].metric('Nhiệt độ out (giờ quan sát được)',
                   display.format_decimal(temperature.out_hours)
                   if temperature.out_hours is not None else 'NULL')
    kpis[1].metric('Nhiệt độ coverage', f'{display.format_decimal(temperature.coverage_pct)} %')
    kpis[2].metric('CO₂ out (giờ quan sát được)',
                   display.format_decimal(co2.out_hours)
                   if co2.out_hours is not None else 'NULL')
    kpis[3].metric('CO₂ coverage', f'{display.format_decimal(co2.coverage_pct)} %')
    chips = [f'{report.metric}: out {display.format_decimal(report.out_minutes)}p '
             f'(low {display.format_decimal(report.out_low_minutes)} / '
             f'high {display.format_decimal(report.out_high_minutes)}) · valid '
             f'{display.format_decimal(report.valid_minutes)}p · unknown '
             f'{display.format_decimal(report.unknown_minutes)}p'
             for report in reports]
    chips += [f'{share.reason_code}: {display.format_decimal(share.minutes)}p ({report.metric})'
              for report in reports for share in report.unknown_reasons]
    st.markdown(''.join(f'<span class="chip">{chip}</span>' for chip in chips),
                unsafe_allow_html=True)

    joint_values = result.joint_results
    if joint_values:
        row = joint_values[0]
        joint_columns = st.columns(3)
        joint_columns[0].metric('OR lệch (giờ, hợp lệ chung)',
                                display.format_decimal(row.joint_out_minutes / 60)
                                if row.joint_out_percent is not None else 'NULL',
                                help='Hợp các slot vi phạm trên giao joint-known; không cộng riêng từng metric')
        joint_columns[1].metric('OR lệch %',
                                display.format_decimal(row.joint_out_percent)
                                if row.joint_out_percent is not None else 'NULL')
        joint_columns[2].metric('Coverage giao chung', f'{display.format_decimal(row.joint_coverage_pct)} %')

    st.markdown(
        '<span class="chip">metric áp dụng: '
        + '; '.join(f'{applied.metric}→{applied.rule_version or "rule_not_configured"}'
                    for applied in result.applications) + '</span>',
        unsafe_allow_html=True)

    with st.container(border=True):
        st.subheader('Danh sách đợt lệch (dài nhất trước, phân trang)')

        if filtered_total == 0:
            st.info('Không có đợt nào sau bộ lọc metric/thời lượng hiện chọn — tổng giờ và '
                    'coverage phía trên vẫn tính đủ mọi đợt, không qua hai bộ lọc này.')
        else:
            st.caption('Đợt dựng trên TOÀN VỤ trước khi lọc: mỗi hàng giữ nguyên thời điểm '
                       'gốc và kèm số phút phần giao với cửa sổ khi có cắt. Bộ lọc '
                       'metric/thời lượng chỉ tác động danh sách; tổng giờ out ở trên '
                       'đã tính đủ mọi đợt.')
            if filtered_total > len(page):
                st.caption(f'Trang {page_offset // EPISODE_PAGE_SIZE + 1}: hiển thị '
                           f'{len(page)}/{filtered_total} đợt sau bộ lọc (mỗi trang '
                           f'{EPISODE_PAGE_SIZE} hàng).')

        offset = page_offset
        def _goto(offset_value):
            def callback():
                st.session_state['clim_page_offset'] = max(0, offset_value)
            return callback

        nav = st.columns([1, 2, 2, 5])
        nav[0].button('◀ Trang trước', key='clim_prev_page',
                      on_click=_goto(offset - EPISODE_PAGE_SIZE), disabled=offset <= 0)
        nav[1].button('Trang sau ▶', key='clim_next_page',
                      on_click=_goto(offset + EPISODE_PAGE_SIZE),
                      disabled=offset + EPISODE_PAGE_SIZE >= filtered_total)
        nav[2].button('Về trang đầu', key='clim_head_page', on_click=_goto(0),
                      disabled=offset == 0)
        nav[3].caption(f'offset {offset}/{filtered_total} đợt sau bộ lọc '
                       f'({'tất cả' if min_value is None else f'min {min_value}p'}'
                       f', metric {metric_filter}).')

        keys = _episode_page_keys(page)
        chosen_episode = None
        if keys:
            # Honor what the user just submitted; replaceONLY an absent/invalid
            # widget value — first with the remembered stable key (T13), then
            # with the current page's first row. Nothing ever overwrites a
            # valid submission back to a lagged state.
            submitted = st.session_state.get('clim_episode_choice')
            if submitted is None or submitted not in keys:
                remembered = st.session_state.get('clim_selected_key')
                st.session_state['clim_episode_choice'] = (
                    remembered if remembered in keys else keys[0])
            key_labels = {key: index for index, key in enumerate(keys)}
            chosen = st.selectbox(
                'Chọn đợt để xem chi tiết', keys,
                format_func=lambda episode_key_value: _episode_label(
                    key_labels[episode_key_value], page[key_labels[episode_key_value]]),
                key='clim_episode_choice')
            chosen_index = keys.index(chosen)
            chosen_episode = page[chosen_index]
            st.session_state['clim_selected_key'] = chosen

    with st.container(border=True):
        st.subheader('Biểu đồ & diễn biến của đợt đã chọn')
        if chosen_episode is None:
            st.info('Không có đợt để hiển thị diễn biến.')
        else:
            fetch_start, fetch_end = _trace_fetch_window(chosen_episode, context)
            st.caption(f'Đang xem #{chosen_index + 1}: '
                       f'{_episode_label(chosen_index, chosen_episode)}')
            try:
                reviews = service.get_slot_review(
                    context, basis=basis, rule_version=rule_label,
                    window_start=fetch_start, window_end=fetch_end)
            except (ValueError, psycopg.Error) as error:
                st.error('Trace theo đợt không đọc được: ' + str(error)[:160])
                reviews = ()
            trace_total = len(reviews)
            capped = reviews[:DETAIL_TRACE_ROWS_LIMIT]
            st.plotly_chart(figures.climate_figure(capped, chosen_episode.metric,
                                                   CLIMATE_UNITS[chosen_episode.metric]),
                            width='stretch', config={'displayModeBar': False})
            st.caption('Biểu đồ đọc đúng luồng phân loại đã dùng cho KPI (không tính '
                       'lại); giờ không có số là khoảng trống, không tô xanh. Phần '
                       'đọc là giao của cửa sổ gốc của đợt với cửa sổ đang xem: '
                       f'[{fetch_start.isoformat()}, {fetch_end.isoformat()}) — đợt gốc '
                       f'{chosen_episode.original_start.isoformat()} → '
                       f'{chosen_episode.original_end.isoformat()}; thời điểm gốc và '
                       'phần cắt vẫn nằm đầy đủ trong bản export CSV.'
                       + (f' Hiển thị tối đa {DETAIL_TRACE_ROWS_LIMIT} hàng '
                          f'({len(capped)}/{trace_total}); bản export CSV ghi rõ số hàng.'
                          if trace_total > len(capped) else ''))
            if (chosen_episode.clipped_minutes is not None
                    and chosen_episode.clipped_start != chosen_episode.original_start):
                st.warning(f'Đợt bắt đầu TRƯỚC cửa sổ đang xem '
                           f'({chosen_episode.original_start.isoformat()}): phần được '
                           f'tính cho cửa sổ hiện tại là '
                           f'{display.format_decimal(chosen_episode.clipped_minutes)} phút '
                           f'từ {chosen_episode.clipped_start.isoformat()}; thời điểm '
                           'gốc vẫn được giữ nguyên trong bản export CSV.')
            metric_reviews = [item for item in capped
                              if item.metric == chosen_episode.metric]
            st.dataframe(
                [{'start': item.start.isoformat(),
                  'actual': display.format_decimal(item.actual) if item.actual is not None else '',
                  'lower': display.format_decimal(item.lower) if item.lower is not None else '',
                  'upper': display.format_decimal(item.upper) if item.upper is not None else '',
                  'class': item.class_code,
                  'deviation': display.format_decimal(item.deviation),
                  'reason': item.unknown_reason or ''} for item in metric_reviews],
                width='stretch', hide_index=True)
            out_rows = [item for item in metric_reviews if item.class_code.startswith('out')]
            if out_rows:
                peak = max(out_rows, key=lambda row: row.deviation)
                facts = service.slot_facts(context, window_start=chosen_episode.original_start,
                                           window_end=chosen_episode.original_end)
                source = next((fact for fact in facts if fact.slot_start == peak.start), None)
                if source is not None:
                    expander = st.expander('Lineage về raw (đỉnh của đợt)', expanded=False)
                    with expander:
                        try:
                            lineage = service.get_lineage(publication,
                                                          revision_id=source.revision_id)
                            st.write(f"Revision: `{lineage.revision_id}` · key "
                                     f"`{lineage.logical_record_key}` · correction "
                                     f"{lineage.correction_id or '—'}")
                            st.write(f"File: `{lineage.relative_path}` · dòng "
                                     f"{lineage.line_number}–{lineage.line_end}")
                            st.write(f"Serial nguồn: {lineage.source_time_serial} · timestamp "
                                     f"{lineage.source_timestamp.isoformat()} (naive)")
                            if lineage.raw_hash_verified:
                                st.success('Raw hash xác minh khớp với file đã mount.')
                            else:
                                st.warning('Raw hash chưa xác minh được trên mount này.')
                        except (FileNotFoundError, OSError):
                            st.warning('Raw mount không đọc được — không xác minh được lineage.')
                        except ValueError as error:
                            st.error(f'Lineage không xác minh được: {str(error)[:160]}')
            episode_scope = (f'episode {chosen_episode.original_start.isoformat()} → '
                             f'{chosen_episode.original_end.isoformat()} '
                             f'({chosen_episode.metric}); read '
                             f'[{fetch_start.isoformat()}, {fetch_end.isoformat()})')
        st.caption('Tổng giờ out và coverage phía trên KHÔNG qua bộ lọc metric/thời lượng; '
                   'danh sách đợt theo bộ lọc, export CSV là list đã lọc và có nhãn '
                   'total/truncated.')

    with st.container(border=True):
        st.subheader('Xuất dữ liệu')
        st.caption('CSV giữ Decimal exact + metadata phiên bản; CSV đợt ghi rõ thời điểm GỐC '
                   'và phần clipped, kèm nhãn total_after_filter/truncated; CSV detail GIỚI '
                   'HẠN trong đợt đã chọn (nhãn scope: episode). JSON là kết quả typed của '
                   'engine (codec v2).')
        stem = (f'climate-{cycle.compartment_id[:24]}-{context.start.isoformat()}_'
                f'{(context.end - timedelta(days=1)).isoformat()}-{basis.value}')
        exported_episodes, _ = page_episodes(result, metric=metric_value, min_minutes=min_value,
                                             limit=EPISODE_EXPORT_LIMIT, offset=0)
        line = st.columns(3)
        line[0].download_button('⬇ CSV summary', exports.climate_summary_csv(evaluation),
                                file_name=f'{stem}-summary.csv', mime='text/csv')
        line[1].download_button('⬇ CSV joint OR', exports.climate_joint_csv(evaluation),
                                file_name=f'{stem}-joint.csv', mime='text/csv')
        line[2].download_button('⬇ CSV đợt (đã lọc)',
                                exports.climate_episodes_csv(evaluation, exported_episodes,
                                                             total_after_filter=filtered_total,
                                                             filter_note=('metric=' + (metric_filter or 'all')
                                                                          + '; min_minutes='
                                                                          + ('all' if min_value is None
                                                                             else str(min_value)))),
                                file_name=f'{stem}-episodes.csv', mime='text/csv')
        second = st.columns(2)
        if chosen_episode is None:
            second[0].info('Không có đợt để xuất CSV detail; chọn một đợt trong danh sách.')
        else:
            try:
                scoped_reviews = service.get_slot_review(
                    context, basis=basis, rule_version=rule_label,
                    window_start=chosen_episode.original_start,
                    window_end=chosen_episode.original_end)
            except (ValueError, psycopg.Error):
                scoped_reviews = ()
            scoped = replace(evaluation, slot_reviews=tuple(scoped_reviews))
            second[0].download_button(
                '⬇ CSV detail (đợt đã chọn)',
                exports.climate_detail_csv(scoped, limit=DETAIL_TRACE_ROWS_LIMIT,
                                           scope_note=episode_scope
                                           + f'; cap {DETAIL_TRACE_ROWS_LIMIT} hàng'),
                file_name=f'{stem}-detail-{chosen_episode.metric}.csv', mime='text/csv')
        # The typed engine result stays downloadable even when nothing is
        # selected (no rule / no episode): the JSON never hides behind the
        # episode detail flow.
        second[1].download_button('⬇ JSON result (typed)',
                                  exports.climate_result_json(evaluation),
                                  file_name=f'{stem}-result.json', mime='application/json')

    with st.container(border=True):
        st.subheader('Tra cứu dữ liệu nguồn trong cửa sổ báo cáo')
        st.caption('Giá trị gốc từng measure và trạng thái chất lượng của từng measure '
                   'trong nguồn, không qua bộ lọc hay ngưỡng nào. Mỗi lượt hiện tối đa '
                   f'{SOURCE_INSPECT_ROWS} hàng 5 phút; đổi lượt bằng hai nút; cửa sổ '
                   'luôn nằm trong khoảng ngày đã chọn. Cục này dùng để kiểm tra các '
                   'giờ không có kết luận (thiếu số/chưa chọn rule) mà không cần nạp '
                   'cả vụ.')
        inspect_offset = st.session_state.get('clim_trace_offset', 0)
        view_start = datetime.combine(context.start, time.min)
        view_end = datetime.combine(context.end, time.min)
        view_slots = max(0, int((view_end - view_start) / timedelta(minutes=5)))
        max_offset = max(0, view_slots - SOURCE_INSPECT_ROWS)
        inspect_offset = max(0, min(inspect_offset, max_offset))

        def _inspect_page(next_offset):
            def callback():
                st.session_state['clim_trace_offset'] = next_offset
            return callback

        nav2 = st.columns([1, 2, 6])
        nav2[0].button('◀ Nguồn trước', key='clim_src_prev',
                       on_click=_inspect_page(inspect_offset - SOURCE_INSPECT_ROWS),
                       disabled=inspect_offset <= 0)
        nav2[1].button('Nguồn sau ▶', key='clim_src_next',
                       on_click=_inspect_page(inspect_offset + SOURCE_INSPECT_ROWS),
                       disabled=inspect_offset >= max_offset)
        nav2[2].caption(f'Cửa sổ nguồn: offset {inspect_offset}/{view_slots} slot '
                        f'(5 phút/slot, tối đa {SOURCE_INSPECT_ROWS} hàng/lượt).')
        page_start = view_start + timedelta(minutes=5 * inspect_offset)
        page_end = view_end if max_offset == 0 else min(
            page_start + timedelta(minutes=5 * SOURCE_INSPECT_ROWS), view_end)
        try:
            source_rows = service.slot_facts(context, window_start=page_start,
                                             window_end=page_end)
        except (ValueError, psycopg.Error) as error:
            st.error('Tra cứu nguồn không đọc được: ' + str(error)[:160])
            source_rows = ()
        st.caption(f'{len(source_rows)} hàng trong cửa sổ '
                   f'[{page_start.isoformat()}, {page_end.isoformat()}) — giá trị '
                   'gốc và trạng thái chất lượng từng measure; dùng để kiểm tra '
                   'các giờ không có kết luận mà không cần nạp cả vụ.')
        st.dataframe([{
            'slot_start': row.slot_start.isoformat(),
            'Tair': display.format_decimal(row.tair) if row.tair is not None else '',
            'CO2air': display.format_decimal(row.co2air) if row.co2air is not None else '',
            't_heat_sp': display.format_decimal(row.t_heat_sp) if row.t_heat_sp is not None else '',
            't_vent_sp': display.format_decimal(row.t_vent_sp) if row.t_vent_sp is not None else '',
            'co2_sp': display.format_decimal(row.co2_sp) if row.co2_sp is not None else '',
            't_heat_vip': display.format_decimal(row.t_heat_vip) if row.t_heat_vip is not None else '',
            't_ventlee_vip': display.format_decimal(row.t_ventlee_vip) if row.t_ventlee_vip is not None else '',
            'co2_vip': display.format_decimal(row.co2_vip) if row.co2_vip is not None else '',
            'quality (status/applicability/reason)': _quality_note(row.measure_quality),
        } for row in source_rows], width='stretch', hide_index=True)

    st.markdown('<div class="foot">Smart Greenhouse · màn hình khí hậu BR-01 — UI không tính lại '
                'công thức, không gộp requested/realized, không cộng CO₂ hoặc nhiệt độ, không tự '
                'publish/migrate; slot thiếu là unknown, không bao giờ xanh giả. Chi tiết đợt '
                'được đọc theo cửa sổ đợt đã chọn; tổng/danh sách dùng cùng kết quả đã pin.',
                unsafe_allow_html=True)
