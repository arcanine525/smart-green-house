"""Harvest screen (BR-05) for the production app.

Same action discipline as the water screen: catalog → pin the publication by
ID once → record/day/week/summary DTOs from the shared service → drilldown,
lineage, authorized unresolved-source audit and exports reuse the same DTOs.
No SQL and no formula copies live here; NULL stays a proven gap, verified
no-event days stay known zero with record_present=False, and a quarantined
value is never re-invented from raw.
"""
from collections import Counter
from datetime import timedelta

import psycopg
import streamlit as st

from greenhouse.app import display, exports, figures, wiring
from greenhouse.contracts import HarvestUnresolvedIssue, RuleStatus
from greenhouse.queries.harvest import HarvestService

HARVEST_ERRORS = {
    'lacks': 'harvest mart bundle',
}

DEMO_FAMILY_PREFIX = 'harvest-demo-b10:'


def latest_demo_versions(rules):
    """Highest stored version of each explicit demo family (display only)."""
    latest = {}
    for option in rules:
        if option.is_demo and option.rule_id.startswith(DEMO_FAMILY_PREFIX):
            latest[option.rule_id] = max(
                latest.get(option.rule_id, option.version), option.version)
    return latest


def harvest_rule_grade_label(option, latest_demo_version):
    """Explicit grade/label option text; supersession notes are display-only.

    A legacy demo version (same immutable family, lower stored version) keeps
    being selectable for historical lookups while the label marks it
    superseded; the replacement points at the accurate synthetic-example
    provenance. Arbitrary non-demo profiles are never labelled."""
    if option is None:
        return '(không chọn — not_configured)'
    tag = 'demo_rule' if option.is_demo else option.label
    note = ''
    if option.is_demo and option.rule_id.startswith(DEMO_FAMILY_PREFIX):
        latest = latest_demo_version.get(option.rule_id, option.version)
        if latest > option.version:
            note = (f' · legacy v{option.version} (superseded: accurate '
                    f'provenance lives in v{latest})')
        elif option.version > 1:
            note = (f' · replacement of legacy v{option.version - 1} '
                    f'(synthetic demo provenance per BR-05 §4.2/AC-08)')
    return (f'{option.rule_version} · v{option.version} · '
            f'B > {display.format_decimal(option.max_class_b_share_pct)} % · {tag}{note}')


def _stop_record():
    st.stop()


def render(connection, settings, publications):
    """Render the harvest screen for one action."""
    catalog = wiring.catalog_service(connection)
    raw_snapshots = wiring.raw_snapshot_mapping(settings.raw_dir)
    service = wiring.harvest_service(connection, raw_snapshots)

    harvest_rows = [row for row in publications if row.harvest_usable]
    with st.sidebar:
        st.markdown('<div class="brand">smart<em> greenhouse</em></div>', unsafe_allow_html=True)
        st.caption('MÀN HÌNH THU HOẠCH · BR-05')
        if not harvest_rows:
            st.error('Không có publication nào đủ harvest mart để xem.')
            st.caption('Publication hiện hành của scope thiếu bundle harvest-mart-v3 '
                       '(hoặc giữ bundle cũ). Chạy workflow pipeline mới; ứng dụng không '
                       'tự publish/backfill. Publication chỉ có water vẫn xem được ở màn hình Nước.')
            st.code('docker compose -p smart-greenhouse run --rm job '
                    'python -m greenhouse run-pipeline --workflow-id harvest-screen',
                    language='bash')
            st.stop()
        selected = st.selectbox('Publication (scope đã publish)', harvest_rows,
                                format_func=wiring.harvest_publication_label,
                                key='harv_pub_choice')
        st.markdown('<div class="side-note">Cùng theo dõi vụ một cycle + một khoang; lũy kế liệt kê '
                    'origin đầu vụ riêng, kể cả khi cửa sổ zoom con.</div>', unsafe_allow_html=True)

    try:
        publication = service.adapter.publication(selected.publication_id)
    except ValueError as error:
        st.error('Publication đã chọn không đọc được qua harvest adapter mặc định.')
        st.caption(f'{type(error).__name__}: {error} · Không tự publish lại.')
        _stop_record()

    cycles = catalog.cycles(selected)
    if not cycles:
        st.error('Publication này chưa có cycle/khoang nào trong harvest mart.')
        st.stop()

    if st.session_state.get('harv_pub_key') != publication.publication_id:
        st.session_state['harv_pub_key'] = publication.publication_id
        st.session_state.pop('harv_cycle_choice', None)

    with st.sidebar:
        cycle = st.selectbox('Khoang · vụ', cycles, format_func=lambda c: f'{c.compartment_name} — {c.cycle_id}',
                             key='harv_cycle_choice')
        active_key = f'{publication.publication_id}|{cycle.cycle_id}|{cycle.compartment_id}'
        if st.session_state.get('harv_active_key') != active_key:
            st.session_state['harv_active_key'] = active_key
            for widget in ('harv_window', 'harv_rule_choice', 'harv_record_choice', 'harv_day_choice',
                           'harv_rule_grain'):
                st.session_state.pop(widget, None)
        cycle_last_inclusive = cycle.end - timedelta(days=1)
        window = st.date_input('Khoảng ngày (cả hai đầu, theo ngày nguồn)', key='harv_window',
                               min_value=cycle.start, max_value=cycle_last_inclusive,
                               value=(cycle.start, cycle_last_inclusive), format='DD/MM/YYYY')
        if isinstance(window, (tuple, list)) and len(window) == 2:
            start_inclusive, end_inclusive = window
        elif isinstance(window, (tuple, list)) or window is None:
            st.info('Khoảng ngày chưa đủ hai đầu — chọn lại ngày bắt đầu và ngày kết thúc.')
            _stop_record()
        else:
            start_inclusive = end_inclusive = window
        rules = catalog.harvest_rules(selected, cycle.compartment_id)

        rule = st.selectbox('Rule kiểm tra (tùy chọn, version tường minh)', [None, *rules],
                            format_func=lambda option: harvest_rule_grade_label(
                                option, latest_demo_versions(rules)),
                            key='harv_rule_choice')
        if rule is not None:
            st.caption(f"Ngưỡng: share B > {display.format_decimal(rule.max_class_b_share_pct)} % "
                       f"(bằng ngưỡng là trong dải) · evidence: {rule.evidence_reference}")
            if rule.is_demo:
                st.markdown('<div class="assumption">Demo rule: fixture/thí nghiệm, không phải '
                            'ngưỡng chất lượng chuẩn của AGC.</div>', unsafe_allow_html=True)
            if rule.is_demo and rule.rule_id.startswith(DEMO_FAMILY_PREFIX):
                latest_version = latest_demo_versions(rules).get(rule.rule_id, rule.version)
                if latest_version > rule.version:
                    st.warning(f'Đã chọn demo legacy v{rule.version}: đã được thay thế bởi '
                               f'v{latest_version} có dẫn nguồn chuẩn; lựa chọn chỉ giữ cho '
                               'tra cứu lịch sử/đối chiếu, không tự động áp dụng.')
                elif rule.version > 1:
                    st.info(f'Demo v{rule.version} là bản thay thế của legacy '
                            f'v{rule.version - 1}; dẫn nguồn là ví dụ tổng hợp BR-05 '
                            '§4.2/AC-08, không phải ngưỡng chất lượng AGC.')
            grain = st.radio('Grain kết luận rule', ('record', 'day', 'week'),
                             index=0, format_func=lambda g: {'record': 'lần thu', 'day': 'ngày',
                                                             'week': 'tuần (ISO)'}[g],
                             help='Kết luận rule đọc đúng trạng thái của grain đã chọn trên '
                                  'service DTO (record/day/week/summary); không tính lại share, '
                                  'không suy từ grain này sang grain khác.', horizontal=True,
                             key='harv_rule_grain')
        else:
            grain = 'record'

    if end_inclusive < cycle.start or start_inclusive > cycle_last_inclusive:
        st.warning('Cửa sổ ngày không giao với vụ đã chọn.')
        st.caption(f'Vụ {cycle.cycle_id} kéo dài {cycle.start.isoformat()} → '
                   f'{cycle_last_inclusive.isoformat()}.')
        _stop_record()

    window_key = f'{start_inclusive.isoformat()}|{end_inclusive.isoformat()}'
    if st.session_state.get('harv_window_key') != window_key:
        st.session_state['harv_window_key'] = window_key
        st.session_state.pop('harv_record_choice', None)

    try:
        context = wiring.build_context(publication, cycle, start_inclusive, end_inclusive,
                                       rule.rule_version if rule else None)
        action = wiring.harvest_action(service, publication, context)
    except ValueError as error:
        st.error('Truy vấn thu hoạch không thực hiện được với bộ lọc hiện tại.')
        st.caption(f'{type(error).__name__}: {error}')
        _stop_record()
    except psycopg.Error:
        st.error('Lỗi PostgreSQL khi đọc thu hoạch (không hiển thị chi tiết cấu hình).')
        _stop_record()

    summary, days, weekly, records = action.summary, action.days, action.weekly, action.records
    state = summary.completeness

    st.title('🍅 Thu hoạch · sản lượng & cơ cấu A/B')
    st.markdown('<p class="subtitle">Kg/m² theo lần thu/ngày/tuần/vụ lớp chất lượng A/B · '
                'tỷ lệ là ratio of masses trên tập paired, nguồn Production theo ngày '
                '(thời gian nguồn chưa gắn timezone).</p>', unsafe_allow_html=True)
    st.markdown(
        f'<span class="chip">scope: {publication.scope_id}</span>'
        f'<span class="chip">snapshot: {publication.snapshot_id[:24]}…</span>'
        f'<span class="chip">publication: {publication.publication_id[:16]}…</span>'
        f'<span class="chip">sealed {publication.committed_at.astimezone().strftime("%d/%m/%Y %H:%M")}</span>'
        f'<span class="chip">code {publication.versions.code_version}</span>'
        f'<span class="chip">bundle {selected.harvest_bundle_version}</span>',
        unsafe_allow_html=True)

    if not state.snapshot_complete:
        st.markdown('<div class="assumption">⚠️ <b>_snapshot chưa hoàn chỉnh</b> — kết quả trên phần '
                    'dữ liệu chấp nhận được, KHOANG NÀY KHÔNG THAM GIA xếp hạng và tổng toàn vụ không '
                    'được gọi là đầy đủ.</div>', unsafe_allow_html=True)

    if summary.rule_status.value == 'not_configured':
        st.markdown('<p class="rule-note">Rule kiểm tra: <b>not_configured</b> — chưa chọn profile; '
                    'không có kết luận đạt/không đạt theo ngưỡng nào (không ngầm dùng ngưỡng mặc định).</p>',
                    unsafe_allow_html=True)
    else:
        profile = summary.rule_profile
        if summary.rule_status == RuleStatus.OUTSIDE_RANGE:
            window_verdict = 'ngoài ngưỡng'
        elif summary.rule_status == RuleStatus.WITHIN_RANGE:
            window_verdict = 'trong dải'
        else:
            window_verdict = 'không xác định'
        unit = {'record': 'lần thu', 'day': 'ngày', 'week': 'tuần'}[grain]
        rows = {'record': records, 'day': days, 'week': weekly}[grain]
        verdicts = Counter(row.rule_status.value for row in rows)
        st.markdown(f'<p class="rule-note">Rule <code>{profile.rule_version}</code> '
                    f'({"demo_rule" if profile.is_demo else profile.label}) · ngưỡng share B &gt; '
                    f"{display.format_decimal(profile.max_class_b_share_pct)} % (bằng ngưỡng là trong "
                    'dải) · kết luận cửa sổ: <b>' + window_verdict + '</b> · kết luận grain '
                    f'{unit}: {verdicts.get("outside_range", 0)} {unit} ngoài ngưỡng · '
                    f'{verdicts.get("unknown", 0)} {unit} không xác định · '
                    f'{verdicts.get("within_range", 0)} {unit} trong dải '
                    f'(trên {len(rows)} {unit} của cửa sổ) · evidence: '
                    f'{profile.evidence_reference}</p>', unsafe_allow_html=True)
        st.caption('Mỗi phần tử ở grain đã chọn được so share B của chính nó với ngưỡng '
                   '(bằng ngưỡng là trong dải; không có mẫu số → không xác định) — không suy '
                   'ra kết luận của grain khác; bộ đếm record chỉ là số đếm thuần túy.')

    kpis = st.columns(4)
    kpis[0].metric('A observed', f'{display.format_decimal(summary.prod_a_observed)} kg/m²',
                   help='Σ ProdA trên record A accepted trong cửa sổ')
    kpis[1].metric('B observed', f'{display.format_decimal(summary.prod_b_observed)} kg/m²',
                   help='Σ ProdB trên record B accepted trong cửa sổ')
    kpis[2].metric('Tổng paired A+B', f'{display.format_decimal(summary.paired_total)} kg/m²',
                   help='Chỉ trên các record đủ cả hai measure')
    kpis[3].metric('Share B (paired)', f'{display.format_decimal(summary.class_b_share_pct)} %',
                   help='100×ΣB/Σ(A+B) trên CÙNG tập paired, không phải trung bình share từng record')
    st.markdown(
        f'<span class="chip">trạng thái: {display.STATUS_GLOSS[summary.status.value]}</span>'
        f'<span class="chip">cửa sổ hiệu lực: {display.window_label(summary)}</span>'
        f'<span class="chip">records cửa sổ: {summary.accepted_records} '
        f'(paired {summary.paired_records})</span>'
        f'<span class="chip">snapshot: {"đầy đủ" if state.snapshot_complete else "chưa hoàn chỉnh"}</span>'
        f'<span class="chip">lũy kế từ đầu vụ: origin '
        f'{summary.cumulative_origin.isoformat() if summary.cumulative_origin else "—"} · '
        f'A {display.format_decimal(summary.cumulative_a)} · '
        f'total {display.format_decimal(summary.cumulative_total)} kg/m²</span>',
        unsafe_allow_html=True)
    extra = st.columns(3)
    extra[0].metric('Paired A (kg/m²)', display.format_decimal(summary.paired_prod_a),
                    help='Σ ProdA trên cùng tập paired như Share B — phần A của mẫu số')
    extra[1].metric('Paired B (kg/m²)', display.format_decimal(summary.paired_prod_b),
                    help='Σ ProdB trên cùng tập paired — mẫu số chung của Share A/B')
    extra[2].metric('Share A (paired)', f'{display.format_decimal(summary.class_a_share_pct)} %',
                    help='100×ΣA/Σ(A+B) trên cùng tập paired')
    if summary.record_quality is not None:
        q = summary.record_quality
        st.caption(f'Cơ sở population: {q.population_records} lần thu trong cửa sổ hiệu lực '
                   f'[{summary.window_start.isoformat()}, {summary.window_end.isoformat()}) '
                   f'— measure A: accepted {q.prod_a_accepted}, missing {q.prod_a_missing}, '
                   f'invalid {q.prod_a_invalid}, suspect {q.prod_a_suspect}; '
                   f'measure B: accepted {q.prod_b_accepted}, missing {q.prod_b_missing}, '
                   f'invalid {q.prod_b_invalid}, suspect {q.prod_b_suspect}. Số đếm chất lượng '
                   'do service trả từ DTO của chính cửa sổ; tách biệt với completeness toàn '
                   'vụ và với nguồn chưa phân giải đã frozen. Ngày không có event KHÔNG '
                   'được tính là coverage.')
    st.markdown(
        f'<span class="chip">record nguồn: read {state.records_read} · '
        f'accepted {state.records_accepted} · quarantine {state.records_quarantine} · '
        f'duplicate {state.records_duplicate} · conflict {state.records_conflict}</span>'
        f'<span class="chip">usable: A {state.prod_a_usable_records} / B '
        f'{state.prod_b_usable_records} / paired {state.paired_records}</span>'
        f'<span class="chip">ngày có event: A {state.event_a_dates} / B {state.event_b_dates}</span>'
        f'<span class="chip">chưa phân giải: quar {state.unresolved_records} / conflict '
        f'{state.conflicted_records}</span>',
        unsafe_allow_html=True)
    for reason in display.gloss_harvest_reasons(summary.reason_codes):
        st.caption(f'• {reason}')

    with st.container(border=True):
        st.subheader('Diễn biến ngày')
        st.caption('Stacked A/B theo ngày nguồn (kg/m²); lũy kế từ origin đầu vụ vẽ trên trục '
                   'riêng; ngày thiếu record để trống, ngày không thu đã xác minh hiển thị 0 '
                   'tường minh.')
        st.plotly_chart(figures.harvest_day_figure(days, summary), width='stretch',
                        config={'displayModeBar': False})
        st.caption(f"Lũy kế chạy từ origin {summary.cumulative_origin.isoformat() if summary.cumulative_origin
                   else '—'} (đầu vụ) đến hết ngày ở cột hiện tại; record SAU cửa sổ không góp vào lũy kế "
                   'chỉnh cột đang xem.')
        if summary.cumulative_basis_label:
            st.caption(f'Cơ sở lũy kế (basis, từ record góp): {summary.cumulative_basis_label}')

    with st.container(border=True):
        st.subheader('Tổng hợp tuần (ISO)')
        st.caption('Tổng observed và share tính riêng trên các record của chính tuần đó '
                   '(cột Rule là kết luận của tuần); cột lũy kế chạy từ origin đầu vụ và '
                   'không reset giữa các tuần (độc lập với share tuần); tuần mà cặp thông '
                   'tin chưa đủ để trống (NULL) chứ không đổi về 0.')
        st.dataframe([display.harvest_weekly_row(item) for item in weekly], width='stretch',
                     hide_index=True)
        st.plotly_chart(figures.harvest_week_figure(weekly), width='stretch',
                        config={'displayModeBar': False})

    with st.container(border=True):
        st.subheader('Chi tiết lần thu & lineage')
        if not records:
            st.info('Không có record thu hoạch nào trong cửa sổ đã chọn.')
        else:
            chosen = st.selectbox('Chọn record', list(records),
                                  format_func=lambda r: (f'{r.harvest_date.isoformat()} · '
                                                         f'A {display.format_decimal(r.prod_a.value)} / '
                                                         f'B {display.format_decimal(r.prod_b.value)} · '
                                                         f'share B {display.format_decimal(r.class_b_share_pct)}%'),
                                  key='harv_record_choice')
            # Detail/lineage luôn dùng DTO HIỆN HÀNH của action: một selection
            # có thể được giữ qua các lần render cũ (widget key), nên re-resolve
            # theo identity bền vững (revision + logical key) trước khi hiển thị
            # bất kỳ kết luận dòng nào; nếu identity hết hiệu lực thì hiển thị
            # thông tin trống, không dùng DTO cũ.
            record = None
            if chosen is not None:
                record = next((item for item in records
                               if item.revision_id == chosen.revision_id
                               and item.logical_record_key == chosen.logical_record_key), None)
            if record is None:
                st.info('Record đã chọn không còn trong cửa sổ hiện tại — chọn lại.')
            left, right = st.columns(2, gap='medium')
            if record is not None:
                with left:
                    st.dataframe([display.harvest_record_row(record)], width='stretch', hide_index=True)
                    st.caption(f'Chất lượng A: {record.prod_a.quality.value}'
                               f'{f" ({record.prod_a.reason_code})" if record.prod_a.reason_code else ""} · '
                               f'Chất lượng B: {record.prod_b.quality.value}'
                               f'{f" ({record.prod_b.reason_code})" if record.prod_b.reason_code else ""}')
                    st.caption(f'Rule: {record.rule_status.value}'
                               + (f' · {record.rule_version}' if record.rule_version else ''))
                with right:
                    st.write(f'Source record/revision: `{record.revision_id}` · key: '
                             f'`{record.logical_record_key}`')
                    st.write(f"Correction: {record.correction_id or '—'}"
                             + (f' v{record.correction_version}' if record.correction_version else ''))
                    expander = st.expander('Lineage về raw (record accepted)', expanded=False)
                    with expander:
                        try:
                            lineage = service.get_lineage(publication,
                                                          revision_id=record.revision_id)
                            st.write(f"File: `{lineage.relative_path}` · dòng "
                                     f"{lineage.line_number}–{lineage.line_end}")
                            st.write(f"Serial nguồn: {lineage.source_time_serial} · timestamp: "
                                     f"{lineage.source_timestamp.isoformat()} (naive)")
                            if lineage.raw_hash_verified:
                                st.success('Raw hash xác minh khớp với file đã mount.')
                            else:
                                st.warning('Raw hash chưa được xác minh.')
                        except FileNotFoundError:
                            st.warning('File raw không có trên mount — không xác minh được lineage.')
                        except OSError:
                            st.warning('Raw mount không đọc được — không xác minh được lineage.')
                        except ValueError as error:
                            st.error(f'Lineage không xác minh được: {str(error)[:160]}')

    with st.container(border=True):
        st.subheader('Records chưa phân giải (excluded) & audit raw')
        st.caption('Provenance đúng theo identity được frozen trong mart của publication; số raw quarantined '
                   'CHỈ hiển thị sau khi file+record được xác minh từ mount read-only và vẫn không bao giờ '
                   'được dùng tính lại KPI.')
        items = state.unresolved_items
        if not items:
            st.success('Không có record quarantine/conflict chưa phân giải trong snapshot này.')
        else:
            st.dataframe([display.unresolved_row(item) for item in items], width='stretch',
                         hide_index=True)
            for index, item in enumerate(items):
                with st.expander(f"Audit nguồn chưa phân giải · {item.source_record_id[:16]}… · "
                                 f"dòng {item.line_number}", expanded=False):
                    issue = HarvestUnresolvedIssue(
                        source_record_id=item.source_record_id, disposition=item.disposition,
                        reason_code=item.reason_code, issue_column=item.issue_column,
                        relative_path=item.relative_path, line_number=item.line_number,
                        source_time_serial=item.source_time_serial)
                    if st.button('Đọc & xác minh từ raw (read-only)',
                                 key=f'harv-audit-{index}'):
                        try:
                            audit = service.get_unresolved_source(context, issue,
                                                                  rule_grain=grain)
                        except ValueError as error:
                            st.error(f'Truy cập nguồn bị từ chối: {str(error)[:160]}')
                        except OSError:
                            st.warning('Raw mount không đọc được — không xác minh được.')
                        else:
                            if audit.record_verified:
                                st.success('Raw record xác minh khớp identity/hash (đọc và để nguyên raw).')
                            elif audit.note:
                                st.warning(display.AUDIT_NOTE_GLOSS.get(
                                    audit.note, f'Không xác minh được: {audit.note}'))
                            st.dataframe(display.source_audit_rows(audit), width='stretch', hide_index=True)
                            if audit.record_verified:
                                st.download_button('⬇ CSV audit (raw đã xác minh)',
                                                   exports.unresolved_source_audit_csv(audit),
                                                   file_name=(f'harvest-unresolved-'
                                                              f'{issue.source_record_id[:12]}.csv'),
                                                   mime='text/csv')
                            else:
                                st.download_button('⬇ CSV audit (chưa xác minh — không có giá trị raw)',
                                                   exports.unresolved_source_audit_csv(audit),
                                                   file_name=(f'harvest-unresolved-'
                                                              f'{issue.source_record_id[:12]}.csv'),
                                                   mime='text/csv')

    with st.container(border=True):
        st.subheader('Xuất dữ liệu')
        st.caption('CSV dùng đúng DTO đang hiển thị: Decimal exact, NULL để trống, 0 giữ nguyên; '
                   'metadata ghi scope/snapshot/publication, cửa sổ, đơn vị, quality, completeness và rule.')
        stem = (f'harvest-{cycle.compartment_id[:24]}-{context.start.isoformat()}_'
                f'{(context.end - timedelta(days=1)).isoformat()}')
        buttons = st.columns(4)
        buttons[0].download_button('⬇ CSV lần thu', exports.harvest_record_csv(action),
                                   file_name=f'{stem}-records.csv', mime='text/csv')
        buttons[1].download_button('⬇ CSV ngày', exports.harvest_day_csv(action),
                                   file_name=f'{stem}-days.csv', mime='text/csv')
        buttons[2].download_button('⬇ CSV tuần', exports.harvest_week_csv(action),
                                   file_name=f'{stem}-weekly.csv', mime='text/csv')
        buttons[3].download_button('⬇ JSON summary (window_summary)',
                                   exports.harvest_summary_json(action),
                                   file_name=f'{stem}-summary.json', mime='application/json')
        st.download_button('⬇ CSV nguồn chưa phân giải (provenance)', exports.excluded_source_csv(action),
                           file_name=f'{stem}-excluded.csv', mime='text/csv')

    st.markdown('<div class="foot">Smart Greenhouse · màn hình thu hoạch BR-05 — UI không tính lại '
                'công thức, không cộng kg/m² giữa các khoang, không xếp hạng khoang chưa đủ điều kiện, '
                'không tự publish/migrate; giá trị quarantined không bao giờ được dán lại vào kết quả.</div>',
                unsafe_allow_html=True)
