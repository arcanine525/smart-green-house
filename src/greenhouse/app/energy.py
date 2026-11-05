"""BR04 physical energy intensity. Read-only composition, no formula copies."""
from datetime import timedelta
from decimal import Decimal
import streamlit as st
import plotly.graph_objects as go

from greenhouse.app import wiring, energy_exports as exports
from greenhouse.contracts import EnergyComponent


def number(value, places=3):
    return 'NULL' if value is None else f'{value:,.{places}f}'


def format_cell(value, places=3):
    if value is None:
        return 'NULL'
    if isinstance(value, Decimal):
        return f'{value:,.{places}f}'
    return value


def presentation_rows(rows, places=3):
    return [{k: format_cell(v, places=places) for k, v in row.items()} for row in rows]


def render(connection, settings, publications):
    catalog = wiring.catalog_service(connection)
    service = wiring.intensity_service(connection, wiring.raw_snapshot_mapping(settings.raw_dir))
    usable = [p for p in publications if p.energy_usable]
    if not usable:
        st.error('Publication hiện hành chưa có energy-mart-v1 / energy-quantity-v1 và harvest mart. '
                 'Chạy workflow mới; màn hình không tự migrate hoặc publish.')
        st.stop()
    with st.sidebar:
        st.caption('NĂNG LƯỢNG · BR-04')
        selected = st.selectbox('Publication năng lượng', usable,
            format_func=lambda p: f'{p.scope_id} · {p.publication_id}', key='energy_publication')
    publication = service.energy.adapter.publication(selected.publication_id)
    cycles = catalog.cycles(publication)
    if not cycles:
        st.info('Không có vụ trong publication đã chọn.'); st.stop()
    if st.session_state.get('energy_pub_key') != publication.publication_id:
        st.session_state['energy_pub_key'] = publication.publication_id
        for key in ('energy_cycle', 'energy_window', 'energy_revision'):
            st.session_state.pop(key, None)
    with st.sidebar:
        cycle = st.selectbox('Khoang · vụ năng lượng', cycles,
            format_func=lambda c: f'{c.compartment_name} — {c.cycle_id}', key='energy_cycle')
        identity = (publication.publication_id, cycle.cycle_id)
        if st.session_state.get('energy_cycle_key') != identity:
            st.session_state['energy_cycle_key'] = identity
            for key in ('energy_window', 'energy_revision'):
                st.session_state.pop(key, None)
        window = st.date_input('Khoảng năng lượng (cả hai đầu)',
            value=(cycle.start, cycle.end-timedelta(days=1)), min_value=cycle.start,
            max_value=cycle.end-timedelta(days=1), format='DD/MM/YYYY', key='energy_window')
        if not isinstance(window, (tuple, list)) or len(window) != 2:
            st.info('Chọn đủ hai đầu khoảng ngày.'); st.stop()
    context = wiring.build_context(publication, cycle, *window)
    result = service.get_intensity(context)
    quantities = service.energy.get_quantities(context)
    contexts = tuple(wiring.build_context(publication, c, c.start, c.end-timedelta(days=1)) for c in cycles)
    population = service.get_portfolio(contexts)
    st.title('Năng lượng trên sản lượng loại A')
    st.caption(f'Publication {publication.publication_id} · snapshot {publication.snapshot_id} · '
               f'{cycle.compartment_name} · [{result.energy.window_start}, {result.energy.window_end}) · ngày nguồn naive')
    st.caption(f'{result.metric_version} · {result.energy.quantity_version} · '
               f'quality {publication.versions.quality_policy_version} · pipeline {publication.versions.code_version}')
    if result.assumptions:
        st.warning(f'{result.basis_gate.label} · {", ".join(result.assumptions)}. '
                   'Resources/Production primary62.5m² là giả định phân tích; không phải xác minh nguồn. '
                   'Sensitivity riêng: Resources96m², Production giữ62.5m², hệ số96/62.5=1.536.')
    elif result.basis_gate.label:
        st.info(f'Basis: {result.basis_gate.label}')
    cols = st.columns(4)
    for col, title, value in zip(cols, ('Năng lượng (kWh)', 'Sản lượng A (kg)', 'kWh/kg A', 'Coverage năng lượng (%)'),
            (result.energy.official_total_kwh, result.official_a_kg, result.intensity_kwh_kg_a, result.energy.complete_coverage_pct)):
        col.metric(title, number(value))
    st.caption('Mẫu số CHỈ ProdA; ProdB vẫn xem được nhưng không cộng vào mẫu số. '
               'Tổng năng lượng gồm mọi ngày trong kỳ, kể cả trước thu hoạch đầu; không ngoại suy phần thiếu.')
    st.write('Nhiệt kWh/kg A:', number(result.heat_kwh_kg_a),
             '· Điện chiếu sáng kWh/kg A:', number(result.light_kwh_kg_a))
    st.write('Sensitivity Resources96 / Production62.5 (kWh/kg A):', number(result.sensitivity_kwh_kg_a))
    st.write('Tổng quan sát (kWh):', number(result.energy.observed_total_kwh),
             '· A quan sát (kg):', number(result.observed_a_kg))
    if result.energy.official_total_kwh_m2 is not None and result.energy.official_total_kwh is None:
        st.write('Tổng năng lượng trên diện tích (kWh/m²):', number(result.energy.official_total_kwh_m2))
    if result.reason_codes:
        st.info('Lý do / điều kiện: ' + ' · '.join(result.reason_codes))
    if not result.comparison_eligible:
        st.warning('Không đủ điều kiện xếp hạng toàn vụ. Khoảng con chỉ mô tả lượng ghi nhận trong kỳ; '
                   'không suy luận năng lượng kỳ này tạo ra quả kỳ này.')
    st.subheader('Thành phần và độ đầy đủ')
    component_rows = []
    for c in result.energy.components:
        unit = 'MJ/m²' if c.component == EnergyComponent.HEAT else 'kWh/m²'
        component_rows.append(dict(
            component=c.component.value,
            source_unit=unit,
            observed_source_per_m2=c.observed_source_per_m2,
            observed_kwh_m2=c.observed_kwh_m2,
            observed_kwh=c.observed_absolute_kwh,
            official_kwh=c.official_absolute_kwh,
            accepted_days=c.accepted_days,
            expected_days=c.expected_days,
            coverage_pct=c.coverage_pct,
            reasons=';'.join(c.reason_codes),
        ))
    st.dataframe(presentation_rows(component_rows), width='stretch', hide_index=True)
    st.write('Ngày thiếu:', ', '.join(str(d) for d in result.energy.incomplete_dates) or 'Không có')
    st.write('Harvest snapshot complete:', result.harvest_state.snapshot_complete,
             '· unresolved:', result.harvest_state.unresolved_records,
             '· paired:', result.harvest_state.paired_records)
    if result.harvest_state.unresolved_items:
        from greenhouse.contracts import to_dict
        st.dataframe(presentation_rows([to_dict(i) for i in result.harvest_state.unresolved_items]), width='stretch')
    st.subheader('So sánh toàn vụ · tập đủ điều kiện')
    st.caption('Một dòng mỗi khoang/vụ; chỉ xếp hạng tập đủ điều kiện cùng cửa sổ, quality và assumption version. '
               'Số hạng là trên tập được bao gồm, không phải toàn bộ sáu khoang.')
    if population.reason_codes:
        st.info('Lý do portfolio: ' + ' · '.join(population.reason_codes))
    portfolio_rows = []
    for m in population.members:
        row = dict(exports.summary_row(m))
        row['included_in_portfolio'] = m.cycle.cycle_id in population.included_cycle_ids
        row['portfolio_reasons'] = ';'.join(population.reason_codes) if m.cycle.cycle_id in population.excluded_cycle_ids else ''
        portfolio_rows.append(row)
    st.dataframe(presentation_rows(portfolio_rows), width='stretch', hide_index=True)
    st.metric('Portfolio kWh/kg A · ΣE / ΣA', number(population.intensity_kwh_kg_a))
    st.write('Bao gồm:', ', '.join(population.included_cycle_ids) or 'Không có')
    st.write('Loại:', ', '.join(population.excluded_cycle_ids) or 'Không có')
    eligible = sorted((m for m in population.members if m.cycle.cycle_id in population.included_cycle_ids),
                      key=lambda m: m.intensity_kwh_kg_a)
    if eligible:
        fig = go.Figure(go.Bar(x=[float(m.intensity_kwh_kg_a) for m in eligible],
            y=[f'{i+1}/{len(eligible)} · {m.cycle.compartment_id}' for i,m in enumerate(eligible)], orientation='h'))
        fig.update_layout(xaxis_title='kWh/kg A · giả định được ghi trong bảng', yaxis_title='Tập đủ điều kiện')
        st.plotly_chart(fig, width='stretch')
    with st.expander('Công thức, basis và đóng góp', expanded=False):
        st.code('E = Σ(Heat_cons × A_heat / 3.6) + Σ(ElecHigh × A_high) + Σ(ElecLow × A_low)\n'
                'Y_A = Σ(ProdA × A_production)\nkWh/kg A = E / Y_A\n'
                'Portfolio = Σ(E_eligible) / Σ(Y_A_eligible)')
        st.caption('50 significant digits half-even tại chia MJ/3.6 và tỷ số; giá trị export exact. '
                   'Basis hiệu lực theo từng ngày; không lấy metadata mới nhất cho lịch sử.')
        st.dataframe(presentation_rows(exports.quantity_rows(quantities)), width='stretch', hide_index=True)
        st.dataframe(presentation_rows(exports.harvest_rows(result)), width='stretch', hide_index=True)
    st.subheader('Truy nguồn đóng góp')
    revisions = tuple(dict.fromkeys([q.revision_id for q in quantities if q.revision_id] +
                                   [c.record.revision_id for c in result.harvest_a]))
    if revisions:
        revision = st.selectbox('Revision nguồn energy / harvest A', revisions, key='energy_revision')
        if st.button('Xác minh lineage raw', key='energy_lineage'):
            try:
                lineage = service.energy.get_lineage(publication, revision_id=revision)
                st.write(f'File {lineage.relative_path} · dòng {lineage.line_number}–{lineage.line_end}')
                st.write('Raw hash verified:', lineage.raw_hash_verified)
                st.json(__import__('greenhouse.contracts', fromlist=['to_dict']).to_dict(lineage))
            except (OSError, ValueError) as error:
                st.warning(f'Không xác minh được raw: {str(error)[:160]}')
    st.subheader('Xuất đúng kết quả đã pin')
    stem = f'energy-{cycle.compartment_name}-{context.start}-{context.end}'
    for label, data, suffix, mime in (
        ('CSV summary năng lượng', exports.summary_csv(result), 'summary.csv', 'text/csv'),
        ('CSV quantities năng lượng', exports.quantities_csv(result, quantities), 'quantities.csv', 'text/csv'),
        ('CSV harvest A contributions', exports.harvest_csv(result), 'harvest.csv', 'text/csv'),
        ('CSV portfolio (gồm excluded)', exports.portfolio_csv(population), 'portfolio.csv', 'text/csv'),
        ('JSON intensity (typed)', exports.result_json(result), 'result.json', 'application/json'),
        ('JSON portfolio (typed)', exports.portfolio_json(population), 'portfolio.json', 'application/json')):
        st.download_button(label, data, file_name=f'{stem}-{suffix}', mime=mime)
