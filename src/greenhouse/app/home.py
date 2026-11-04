"""Smart Greenhouse — water screen (BR-02). Production app entrypoint.

Run through the Compose `app` service (see src/greenhouse/app/README.md).
Every rerun is one action: catalog → pinned publication → days/summary/weekly
→ drilldown/exports, all reusing the same DTOs. No SQL and no formula copies
live here; unknown/NULL stays unknown and basis assumptions keep their label.
"""
from contextlib import closing
from datetime import timedelta

import psycopg
import streamlit as st

from greenhouse.app import display, exports, figures, wiring
from greenhouse.settings import Settings

st.set_page_config(page_title='Smart Greenhouse · Nước', page_icon='💧', layout='wide')

st.markdown("""<style>
.stApp{background:#f5f6f0;color:#243d31}header[data-testid="stHeader"]{background:#f5f6f0}
[data-testid="stSidebar"]{background:#173d30;color:#dbe4db}
.block-container{padding-top:3.5rem;padding-bottom:2rem;max-width:1600px}
h1{font-size:1.9rem!important;letter-spacing:-1px;font-weight:600!important}
[data-testid="stVerticalBlockBorderWrapper"]>div{border-color:#e7ebe2!important;border-radius:12px!important}
[data-testid="stVerticalBlockBorderWrapper"]{background:white;border-radius:12px}
.brand{font-size:26px;font-weight:650;letter-spacing:-1.2px;margin:0 0 2px}.brand em{color:#d7e9ab;font-style:normal}
.side-note{border:1px solid #41634f;border-radius:9px;padding:14px;margin-top:34px;font-size:12px;color:#adc5b3!important}
.subtitle{font-size:13px;color:#7c867c;margin-top:-8px;margin-bottom:14px}
.chip{display:inline-block;background:#edf3e4;color:#67814e;font-size:10px;border-radius:5px;padding:4px 8px;margin:2px 6px 2px 0}
.assumption{border:1px solid #d9b26a;background:#fdf6e4;color:#8a6420;border-radius:9px;padding:10px 14px;font-size:12.5px;margin:10px 0}
/* Sidebar: light text for labels/captions on the dark panel, but control
   values/options keep dark text on their own light surfaces. Visible date
   components are spinbutton/literal spans (the date input itself is hidden),
   so the whole date-field subtree is restored to dark text; select controls
   are buttons. Labels outside these containers stay light. */
[data-testid="stSidebar"] *{color:#dbe4db}
[data-testid="stSidebar"] input,
[data-testid="stSidebar"] button,
[data-testid="stSidebar"] [data-testid="stDateInputField"],
[data-testid="stSidebar"] [data-testid="stDateInputField"] *,
[data-testid="stSidebar"] [role="spinbutton"],
[data-testid="stSidebar"] [role="spinbutton"] *{color:#243d31!important}
.rule-note{font-size:11.5px;color:#8c9681;margin:4px 0 12px}
.foot{font-size:10px;color:#99a18d;margin-top:25px;border-top:1px solid #e4e9dc;padding-top:14px}
</style>""", unsafe_allow_html=True)


def stop_with(kind, title, detail=None, tips=None):
    getattr(st, kind)(title)
    if detail:
        st.caption(detail)
    if tips:
        st.code(tips, language='bash')
    st.stop()


def render(connection):
    settings = Settings.from_env()
    catalog = wiring.catalog_service(connection)
    raw_snapshots = wiring.raw_snapshot_mapping(settings.raw_dir)
    service = wiring.irrigation_service(connection, raw_snapshots)

    try:
        publications = catalog.publications()
    except psycopg.Error:
        stop_with('error', 'Không đọc được catalog publication từ PostgreSQL.',
                  'Kiểm tra DB Compose rồi thử lại; chi tiết lỗi không hiển thị để tránh lộ cấu hình.')

    if not publications:
        stop_with('info', 'Chưa có publication nào được publish — ứng dụng không có dữ liệu fixture thay thế.',
                  'Chạy pipeline để tạo publication có water mart (lệnh đầy đủ trong docs/implementation/local-environment.md):',
                  'docker compose -p smart-greenhouse run --rm job python -m greenhouse migrate\n'
                  'docker compose -p smart-greenhouse run --rm job python -m greenhouse run-pipeline --workflow-id water-first')

    usable_rows = [row for row in publications if row.usable]
    if not usable_rows:
        stop_with('error', 'Không có publication nào đủ water mart để xem.',
                  'Publication hiện hành của scope hiện tại thiếu bundle irrigation-mart-v1 '
                  '(publication cũ trước mart). Chạy workflow pipeline mới; ứng dụng không tự publish/backfill.',
                  'docker compose -p smart-greenhouse run --rm job python -m greenhouse run-pipeline --workflow-id water-rerun')

    with st.sidebar:
        st.markdown('<div class="brand">smart<em> greenhouse</em></div>', unsafe_allow_html=True)
        st.caption('MÀN HÌNH NƯỚC · BR-02')
        selected = st.selectbox('Publication (scope đã publish)', usable_rows,
                                format_func=wiring.publication_label, key='pub_choice')
        st.markdown('<div class="side-note">Bộ lọc lấy từ published catalog — không nhúng ID thủ công. '
                    'Một tác vụ pin đúng một publication; đổi scope/vụ sẽ reset ngày và rule.</div>',
                    unsafe_allow_html=True)

    try:
        publication = wiring.resolve_publication(service, selected.publication_id)
    except ValueError as error:
        stop_with('error', 'Publication đã chọn không đọc được qua adapter mặc định.',
                  f'{type(error).__name__}: {error} · Không tự publish lại; chạy workflow pipeline mới.')

    cycles = catalog.cycles(publication)
    if not cycles:
        stop_with('error', 'Publication này chưa có cycle/khoang nào trong water mart.',
                  'Chọn publication khác hoặc chạy lại pipeline với mart bundle.')

    if st.session_state.get('pub_key') != publication.publication_id:
        st.session_state['pub_key'] = publication.publication_id
        for widget in ('cycle_choice', 'window', 'rule_choice', 'day_choice'):
            st.session_state.pop(widget, None)

    with st.sidebar:
        cycle = st.selectbox('Khoang · vụ', cycles, format_func=lambda c: f'{c.compartment_name} — {c.cycle_id}',
                             key='cycle_choice')
        # Cycle/compartment changes invalidate every dependent choice, exactly
        # like a publication change: dates, scoped rule and day detail reset.
        active_key = f'{publication.publication_id}|{cycle.cycle_id}|{cycle.compartment_id}'
        if st.session_state.get('active_key') != active_key:
            st.session_state['active_key'] = active_key
            for widget in ('window', 'rule_choice', 'day_choice'):
                st.session_state.pop(widget, None)
        cycle_last_inclusive = cycle.end - timedelta(days=1)
        window = st.date_input('Khoảng ngày (cả hai đầu, theo ngày nguồn)', key='window',
                               min_value=cycle.start, max_value=cycle_last_inclusive,
                               value=(cycle.start, cycle_last_inclusive), format='DD/MM/YYYY')
        if isinstance(window, (tuple, list)) and len(window) == 2:
            start_inclusive, end_inclusive = window
        elif isinstance(window, (tuple, list)) or window is None:
            # Transient mid-edit state (zero or one picked date): neutral prompt,
            # no query and no export for an incomplete window.
            st.info('Khoảng ngày chưa đủ hai đầu — chọn lại ngày bắt đầu và ngày kết thúc để xem dữ liệu.')
            st.stop()
        else:
            start_inclusive = end_inclusive = window
        rules = catalog.rules(publication, cycle.compartment_id)

        def rule_label(rule_option):
            if rule_option is None:
                return '(không chọn — not_configured)'
            tag = 'demo_rule' if rule_option.is_demo else rule_option.label
            return f'{rule_option.rule_version} · v{rule_option.version} · {tag}'

        rule = st.selectbox('Rule kiểm tra (tùy chọn, version tường minh)', [None, *rules],
                            format_func=rule_label, key='rule_choice')
        if rule is not None:
            st.caption(f'Ngưỡng: drain {display.format_decimal(rule.min_drain_pct)}–'
                       f'{display.format_decimal(rule.max_drain_pct)} % · '
                       f'irrigation {display.format_decimal(rule.min_irrigation_l_m2)}–'
                       f'{display.format_decimal(rule.max_irrigation_l_m2)} L/m² · '
                       f'evidence: {rule.evidence_reference}')

    if end_inclusive < cycle.start or start_inclusive > cycle_last_inclusive:
        stop_with('warning', 'Cửa sổ ngày không giao với vụ đã chọn.',
                  f'Vụ {cycle.cycle_id} kéo dài {cycle.start.isoformat()} → {cycle_last_inclusive.isoformat()}.')

    window_key = f'{start_inclusive.isoformat()}|{end_inclusive.isoformat()}'
    if st.session_state.get('window_key') != window_key:
        st.session_state['window_key'] = window_key
        st.session_state.pop('day_choice', None)

    try:
        context = wiring.build_context(publication, cycle, start_inclusive, end_inclusive,
                                       rule.rule_version if rule else None)
        action = wiring.water_action(service, publication, context)
    except ValueError as error:
        stop_with('error', 'Truy vấn nước không thực hiện được với bộ lọc hiện tại.',
                  f'{type(error).__name__}: {error}')
    except psycopg.Error:
        stop_with('error', 'Lỗi PostgreSQL khi đọc nước (không hiển thị chi tiết cấu hình).')

    summary, days, weekly = action.summary, action.days, action.weekly

    st.title('💧 Nước · tưới & drain')
    st.markdown('<p class="subtitle">Tổng lượng ngày (L/m²), tỷ lệ drain theo kỳ đã chọn · '
                'nguồn Resources theo ngày, thời gian nguồn chưa gắn timezone.</p>', unsafe_allow_html=True)
    st.markdown(
        f'<span class="chip">scope: {publication.scope_id}</span>'
        f'<span class="chip">snapshot: {publication.snapshot_id[:24]}…</span>'
        f'<span class="chip">publication: {publication.publication_id[:16]}…</span>'
        f'<span class="chip">sealed {publication.committed_at.astimezone().strftime("%d/%m/%Y %H:%M")}</span>'
        f'<span class="chip">code {publication.versions.code_version}</span>'
        f'<span class="chip">metric {publication.versions.metric_version}</span>',
        unsafe_allow_html=True)

    if summary.basis_status.value == 'assumed_unverified':
        assumption = display.join(summary.assumption_ids, ', ')
        version = display.join(summary.assumption_versions, ', v')
        st.markdown(
            f'<div class="assumption">⚠️ <b>Basis diện tích: {summary.basis_label}</b> — giả định '
            f'<code>{assumption}</code> v{version}; tỷ lệ drain và tổng phụ thuộc giả định này. '
            f'Basis version: {display.join(summary.basis_version_ids)}.</div>', unsafe_allow_html=True)
    elif summary.basis_status.value in ('unknown', 'incompatible'):
        st.markdown(f'<div class="assumption">⚠️ <b>Basis Irr/Drain chưa dùng được cho tỷ lệ</b> — '
                    f'trạng thái <code>{summary.basis_status.value}</code>; tổng observed từng measure vẫn hiển thị, '
                    f'tỷ lệ drain là NULL.</div>', unsafe_allow_html=True)

    if summary.rule_status.value == 'not_configured':
        st.markdown('<p class="rule-note">Kiểm tra rule: <b>not_configured</b> — chưa chọn profile; '
                    'không có kết luận đạt/không đạt theo ngưỡng nào.</p>', unsafe_allow_html=True)
    else:
        profile = summary.rule_profile
        st.markdown(f'<p class="rule-note">Rule <code>{profile.rule_version}</code> '
                    f'({"demo_rule" if profile.is_demo else profile.label}) · ngoài dải '
                    f'{summary.outside_rule_days} ngày · không xác định {summary.rule_unknown_days} ngày · '
                    f'evidence: {profile.evidence_reference}</p>', unsafe_allow_html=True)

    kpis = st.columns(4)
    kpis[0].metric('Irrigation observed', f'{display.format_decimal(summary.irrigation_observed_l_m2)} L/m²')
    kpis[1].metric('Drain observed', f'{display.format_decimal(summary.drain_observed_l_m2)} L/m²')
    kpis[2].metric('Tỷ lệ drain (paired)', f'{display.format_decimal(summary.drain_fraction_pct)} %')
    kpis[3].metric('Độ phủ paired', f'{display.format_decimal(summary.paired_coverage_pct)} %')
    st.markdown(
        f'<span class="chip">trạng thái: {display.STATUS_GLOSS[summary.status.value]}</span>'
        f'<span class="chip">cửa sổ hiệu lực: {display.window_label(summary)}</span>'
        f'<span class="chip">ngày kỳ vọng: {summary.expected_days}</span>'
        f'<span class="chip">Irr accepted: {summary.irrigation_accepted_days}</span>'
        f'<span class="chip">Drain accepted: {summary.drain_accepted_days}</span>'
        f'<span class="chip">paired: {summary.paired_days}</span>'
        f'<span class="chip">unknown: {summary.unknown_days}</span>'
        f'<span class="chip">suspect: {summary.suspect_days}</span>'
        f'<span class="chip">drain&gt;irr: {summary.drain_exceeds_irrigation_days}</span>',
        unsafe_allow_html=True)
    st.markdown(
        f'<span class="chip">Độ phủ Irr: {display.format_decimal(summary.irrigation_coverage_pct)} %</span>'
        f'<span class="chip">Độ phủ Drain: {display.format_decimal(summary.drain_coverage_pct)} %</span>'
        f'<span class="chip">Độ phủ paired: {display.format_decimal(summary.paired_coverage_pct)} %</span>'
        f'<span class="chip">Paired drain/tưới (tử/mẫu): '
        f'{display.format_decimal(summary.paired_drain_l_m2)} / '
        f'{display.format_decimal(summary.paired_irrigation_l_m2)} L/m²</span>',
        unsafe_allow_html=True)
    for reason in display.gloss_reasons(summary.reason_codes):
        st.caption(f'• {reason}')

    with st.container(border=True):
        st.subheader('Diễn biến ngày')
        st.caption('Đường lượng tưới/drain (L/m²) và tỷ lệ drain (%) · ngày thiếu dữ liệu để trống, không vẽ về 0.')
        st.plotly_chart(figures.daily_figure(days), width='stretch', config={'displayModeBar': False})

    with st.container(border=True):
        st.subheader('Tổng hợp tuần (ISO)')
        st.caption('Tỷ lệ tuần là tổng paired Drain / tổng paired Irr (ratio of sums), không phải trung bình tỷ lệ ngày.')
        st.dataframe([display.weekly_row(item) for item in weekly], width='stretch', hide_index=True)

    with st.container(border=True):
        st.subheader('Chi tiết ngày & lineage')
        day = st.selectbox('Chọn ngày', days, format_func=lambda item: item.resource_date.isoformat(),
                           key='day_choice')
        left, right = st.columns(2, gap='medium')
        with left:
            st.dataframe([display.day_row(day)], width='stretch', hide_index=True)
            st.caption(f'Basis Irr: {day.irrigation.basis_status.value}'
                       + (f' · {day.irrigation.basis_label}' if day.irrigation.basis_label else ''))
            st.caption(f'Basis Drain: {day.drain.basis_status.value}'
                       + (f' · {day.drain.basis_label}' if day.drain.basis_label else ''))
            if day.rule_status.value != 'not_configured':
                st.caption(f'Rule ngày: {day.rule_status.value} · {day.rule_version}')
        with right:
            if day.revision_id is None:
                st.info('Ngày không có bản ghi nguồn (missing) — không hiển thị nguồn ảo.')
            else:
                with st.expander('Lineage về raw', expanded=False):
                    try:
                        lineage = wiring.cycle_lineage(service, publication, day.revision_id)
                        st.write(f"File: `{lineage.relative_path}` · dòng {lineage.line_number}–{lineage.line_end}")
                        st.write(f"Serial nguồn: {lineage.source_time_serial} · timestamp nguồn: "
                                 f"{lineage.source_timestamp.isoformat()} (naive)")
                        st.write(f"Logical key: `{lineage.logical_record_key}` · "
                                 f"revision: `{lineage.revision_id}` (#{lineage.revision_number})")
                        st.write(f"Correction: {lineage.correction_id or '—'}"
                                 + (f' v{lineage.correction_version}' if lineage.correction_version else ''))
                        if lineage.raw_hash_verified:
                            st.success('Raw hash xác minh khớp với file đã mount.')
                        else:
                            st.warning('Raw hash chưa được xác minh.')
                    except FileNotFoundError:
                        st.warning('File raw không có trên mount — không xác minh được lineage '
                                   '(khác với “đã xác minh”).')
                    except (PermissionError, IsADirectoryError):
                        st.warning('Raw mount không đọc được — không xác minh được lineage.')
                    except OSError:
                        st.warning('Raw mount gặp lỗi đọc — không xác minh được lineage.')
                    except ValueError as error:
                        message = str(error)
                        if 'mount' in message:
                            st.warning('Raw snapshot mount chưa khả dụng — không xác minh được lineage '
                                       '(khác với “đã xác minh”). Cần mount raw đọc-cho của đúng snapshot.')
                        elif 'checksum' in message:
                            st.error('File raw trên mount không khớp checksum đã lưu — lineage không xác minh.')
                        else:
                            st.error(f'Lineage không xác minh được: {message}')

    with st.container(border=True):
        st.subheader('Xuất dữ liệu')
        st.caption('CSV dùng đúng DTO đang hiển thị: Decimal exact, NULL để trống, 0 giữ nguyên; '
                   'metadata ghi scope/snapshot/publication, cửa sổ, đơn vị, basis/giả định và rule.')
        stem = f'water-{cycle.compartment_id}-{context.start.isoformat()}_{(context.end - timedelta(days=1)).isoformat()}'
        buttons = st.columns(3)
        buttons[0].download_button('⬇ CSV ngày', exports.day_csv(action),
                                   file_name=f'{stem}-days.csv', mime='text/csv')
        buttons[1].download_button('⬇ CSV tuần', exports.weekly_csv(action),
                                   file_name=f'{stem}-weekly.csv', mime='text/csv')
        buttons[2].download_button('⬇ JSON summary (v2)', exports.summary_json(action),
                                   file_name=f'{stem}-summary.json', mime='application/json')

    st.markdown('<div class="foot">Smart Greenhouse · màn hình nước tối thiểu — UI không tính lại công thức, '
                'không tự publish/migrate và không cộng L/m² giữa các khoang. '
                'Prototype dashboard (prototypes/dashboard) chỉ là demo fixture, tách riêng ứng dụng này.</div>',
                unsafe_allow_html=True)


try:
    settings = Settings.from_env()
except ValueError as error:
    stop_with('error', 'Cấu hình chưa đủ (PGHOST/PGDATABASE/PGUSER/PGPASSWORD).', str(error),
              'cp .env.example .env  # rồi chỉnh giá trị local')
else:
    try:
        connection = psycopg.connect(**settings.connection_kwargs())
    except psycopg.Error:
        stop_with('error', 'Không kết nối được PostgreSQL qua Compose.',
                  'Chi tiết kết nối không hiển thị; kiểm tra service db và .env (không log credential).',
                  'docker compose -p smart-greenhouse up -d --wait db')
    with closing(connection):
        render(connection)
