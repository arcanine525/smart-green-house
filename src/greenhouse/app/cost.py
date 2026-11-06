"""BR-06 energy cost and tariff scenarios UI screen.

Provides baseline reference evaluation, user scenario creation, versioned revisions,
side-by-side delta comparisons, sensitivity disclosures, and exact exports.
All calculations delegate to EnergyCostService; zero formula copies or direct SQL.
"""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from uuid import uuid4

import psycopg
import streamlit as st
from greenhouse.app import shared_context
from greenhouse.app.runtime import is_clickhouse

from greenhouse.app import cost_exports as exports, wiring
from greenhouse.contracts import (
    BasisStatus, ComponentCostDelta, CostScenario, EnergyComponent, EnergyCostSummary, PublicationRef, QueryContext,
    ScenarioChange, ScenarioCommand, ScenarioComparison, exact_product,
)
from greenhouse.queries.cost import compare_summaries, compare_weekly, share_pct


def format_money(value: Decimal | None, places: int = 2, unit: str = 'EUR') -> str:
    """Format monetary cost amount to fixed decimal places with thousands separator."""
    if value is None:
        return 'NULL'
    return f'{value:,.{places}f} {unit}'.strip()


def format_rate(rate: Decimal | None, price_unit: str = '') -> str:
    """Format unit rate preserving exact precision without blanket 2dp truncation."""
    if rate is None:
        return 'NULL'
    s = f'{rate}'
    return f'{s} {price_unit}'.strip()


def heat_equivalent_display(rate: Decimal, price_unit: str) -> str:
    """Compute exact equivalent rate without ambient Decimal precision28 truncation."""
    if price_unit == 'EUR/MJ':
        equiv_val = exact_product(rate, Decimal('3.6'))
        return f' (tương đương {format_rate(equiv_val)} EUR/kWh)'
    elif price_unit == 'EUR/kWh':
        with localcontext() as ctx:
            ctx.prec = max(50, len(rate.as_tuple().digits) + 20)
            equiv_val = rate / Decimal('3.6')
        return f' (tương đương {format_rate(equiv_val)} EUR/MJ)'
    return ''


def format_cell(value, is_rate: bool = False, places: int = 2) -> str:
    """Convert any value to a browser-safe string for st.dataframe cells."""
    if value is None:
        return 'NULL'
    if isinstance(value, Decimal):
        if is_rate:
            return f'{value}'
        return f'{value:,.{places}f}'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def presentation_rows(rows: list[dict], rate_keys: set[str] | None = None, places: int = 2) -> list[dict]:
    rate_keys = rate_keys or set()
    out = []
    for row in rows:
        formatted = {}
        for k, v in row.items():
            formatted[k] = format_cell(v, is_rate=(k in rate_keys), places=places)
        out.append(formatted)
    return out


def parse_decimal_input(text: str, field_name: str, min_val: Decimal | None = None) -> Decimal:
    clean = text.strip()
    if not clean:
        raise ValueError(f'Vui lòng nhập giá trị cho {field_name}')
    try:
        val = Decimal(clean)
    except InvalidOperation:
        raise ValueError(f'{field_name}: "{clean}" không phải là số thập phân hợp lệ')
    if not val.is_finite():
        raise ValueError(f'{field_name}: giá trị không hữu hạn')
    if min_val is not None and val < min_val:
        raise ValueError(f'{field_name}: giá trị phải lớn hơn hoặc bằng {min_val}')
    return val


def render(connection, settings, publications):
    catalog = wiring.catalog_service(connection)
    cost_svc = wiring.cost_service(connection, wiring.raw_snapshot_mapping(settings.raw_dir))

    usable = [p for p in publications if p.energy_usable]
    if not usable:
        st.error('Publication hiện hành chưa có energy mart bundle. '
                 'Chạy workflow mới; màn hình không tự migrate hoặc publish.')
        st.stop()

    with st.sidebar:
        st.caption('CHI PHÍ & KỊCH BẢN GIÁ · BR-06')
        shared_context.adopt_publication(st.session_state, 'cost_publication', usable, st.warning)
        selected_pub = st.selectbox(
            'Publication năng lượng', usable,
            format_func=lambda p: f'{p.scope_id} · {p.publication_id}',
            key='cost_publication'
        )

    publication = cost_svc.energy.adapter.publication(selected_pub.publication_id)
    cycles = catalog.cycles(publication)
    if not cycles:
        st.info('Không có vụ trong publication đã chọn.')
        st.stop()

    if st.session_state.get('cost_pub_key') != publication.publication_id:
        st.session_state['cost_pub_key'] = publication.publication_id
        for key in ('cost_cycle', 'cost_window', 'cost_scenario_select'):
            st.session_state.pop(key, None)

    with st.sidebar:
        shared_context.adopt_cycle(st.session_state, 'cost_cycle', cycles, st.warning)
        cycle = st.selectbox(
            'Khoang · vụ', cycles,
            format_func=lambda c: f'{c.compartment_name} — {c.cycle_id}',
            key='cost_cycle'
        )
        identity = (publication.publication_id, cycle.cycle_id)
        if st.session_state.get('cost_cycle_key') != identity:
            st.session_state['cost_cycle_key'] = identity
            for key in ('cost_window', 'cost_scenario_select'):
                st.session_state.pop(key, None)

        shared_context.adopt_window(st.session_state, 'cost_window', cycle, st.warning)
        window = st.date_input(
            'Khoảng ngày (cả hai đầu)',
            value=(cycle.start, cycle.end - timedelta(days=1)),
            min_value=cycle.start,
            max_value=cycle.end - timedelta(days=1),
            format='DD/MM/YYYY',
            key='cost_window'
        )
        if not isinstance(window, (tuple, list)) or len(window) != 2:
            st.info('Chọn đủ hai đầu khoảng ngày.')
            st.stop()

    shared_context.remember(st.session_state, publication, cycle, window)
    context = wiring.build_context(publication, cycle, *window)

    # 1. Baseline Tariff Lookup - NO implicit write from render()
    baseline_tariff = cost_svc.get_baseline_tariff(publication.scope_id, cycle.compartment_id)
    if baseline_tariff is None:
        baseline_tariff = cost_svc.get_baseline_tariff(publication.scope_id, None)
    if baseline_tariff is None:
        st.warning('Chưa có biểu giá tham chiếu (baseline tariff) cho scope này. '
                   'Với AGC lịch sử, chạy: docker compose run --rm job python -m greenhouse '
                   'register-agc-tariff --apply. Scope khác cần biểu giá cấu hình riêng.')
        st.stop()

    # 2. Existing Scenarios for this Scope
    existing_scenarios = cost_svc.list_scenarios(publication.scope_id)
    scenario_options = ['(Baseline tham chiếu - không dùng kịch bản)'] + [
        f'{s.name} ({s.scenario_version})' for s in existing_scenarios
    ]

    if 'pending_scenario_select' in st.session_state:
        st.session_state['cost_scenario_select'] = st.session_state.pop('pending_scenario_select')

    with st.sidebar:
        st.caption(f'Kịch bản đã lưu ({len(existing_scenarios)})')
        selected_scenario_label = st.selectbox(
            'Chọn kịch bản giá',
            scenario_options,
            key='cost_scenario_select',
            help='Chọn kịch bản đã lưu để so sánh hoặc mở lại, hoặc chọn baseline để đánh giá ban đầu.'
        )

    # Resolve chosen scenario (if any)
    active_scenario: CostScenario | None = None
    if selected_scenario_label != scenario_options[0]:
        for s in existing_scenarios:
            if f'{s.name} ({s.scenario_version})' == selected_scenario_label:
                active_scenario = s
                break

    # If scenario was created with an analysis_context, restore/display it
    eval_context = context
    if active_scenario is not None and active_scenario.analysis_context is not None:
        eval_context = active_scenario.analysis_context

    # Reopen must load the saved baseline version even after newer tariff exists
    target_baseline_version = (
        active_scenario.baseline_tariff_version
        if active_scenario is not None
        else baseline_tariff.tariff_version
    )
    effective_baseline_profile = cost_svc.adapter.get_tariff(target_baseline_version) or baseline_tariff

    eval_comp_id = eval_context.compartment_ids[0]
    eval_compartment_name = next(
        (c.compartment_name for c in cycles if c.compartment_id == eval_comp_id),
        eval_comp_id
    )

    st.title('Chi phí năng lượng & Kịch bản biểu giá')
    st.markdown(
        '> **Phân tích chi phí theo kịch bản** · Giữ nguyên mức tiêu thụ năng lượng vật lý · '
        'Tiền tệ EUR lịch sử của thí nghiệm (`Economics.pdf`). '
        'Không phải hóa đơn thanh toán thực tế, giá thị trường hiện tại hoặc lợi nhuận trang trại.'
    )

    # Calculate baseline cost with target_baseline_version
    baseline_cost = cost_svc.get_cost(eval_context, tariff_version=target_baseline_version)

    # If scenario selected, calculate scenario cost & comparison
    comparison: ScenarioComparison | None = None
    scenario_cost: EnergyCostSummary | None = None
    if active_scenario is not None:
        comparison = cost_svc.compare_scenarios(
            eval_context,
            scenario_version=active_scenario.scenario_version,
            baseline_tariff_version=target_baseline_version,
        )
        scenario_cost = comparison.scenario

    # Pinned context notice
    if active_scenario is not None and active_scenario.analysis_context is not None:
        ac = active_scenario.analysis_context
        if (ac.publication_id != context.publication_id or ac.cycle_id != context.cycle_id or
            ac.start != context.start or ac.end != context.end):
            st.warning(
                f'📌 **Kịch bản được pin với ngữ cảnh phân tích gốc**: Publication `{ac.publication_id}`, '
                f'Vụ `{ac.cycle_id}`, Khoảng `[{ac.start}, {ac.end})`. '
                'Bộ lọc thanh bên đang hiển thị khác, kết quả hiển thị được neo theo đúng ngữ cảnh của kịch bản đã lưu.'
            )

    st.caption(
        f'Publication `{eval_context.publication_id}` · Scope `{eval_context.scope_id}` · '
        f'Vụ `{eval_context.cycle_id}` · Khoang `{eval_compartment_name}` · '
        f'Khoảng `[{eval_context.start}, {eval_context.end})` ({baseline_cost.expected_days} ngày)'
    )

    # Top Metrics
    st.subheader('Tổng quan chi phí' if active_scenario is None else f'So sánh kịch bản: {active_scenario.name}')
    cols = st.columns(4)

    if comparison is None:
        cols[0].metric('Tổng chi phí (EUR)', format_money(baseline_cost.official_total_cost_eur))
        cols[1].metric('Chi phí trên diện tích (EUR/m²)', format_money(baseline_cost.official_total_cost_per_m2, unit='EUR/m²'))
        cols[2].metric(
            'Coverage định giá (%)',
            f'{baseline_cost.pricing_coverage_pct:.1f}%' if baseline_cost.pricing_coverage_pct is not None else 'NULL',
            delta=f'{baseline_cost.priced_days}/{baseline_cost.expected_days} ngày'
        )
        cols[3].metric(
            'Coverage lượng (%)',
            f'{baseline_cost.quantity_coverage_pct:.1f}%' if baseline_cost.quantity_coverage_pct is not None else 'NULL',
            delta=f'{baseline_cost.quantity_complete_days}/{baseline_cost.expected_days} ngày'
        )
    else:
        # Baseline vs Scenario vs Delta
        b_eur = comparison.baseline.official_total_cost_eur
        s_eur = comparison.scenario.official_total_cost_eur
        d_eur = comparison.delta_total_cost_eur
        pct_delta = comparison.delta_total_pct

        delta_str = f'{d_eur:+,.2f} EUR' if d_eur is not None else 'NULL'
        if pct_delta is not None:
            delta_str += f' ({pct_delta:+.2f}%)'

        cols[0].metric('Baseline (EUR)', format_money(b_eur))
        cols[1].metric('Kịch bản (EUR)', format_money(s_eur), delta=delta_str)
        cols[2].metric(
            'Kịch bản (EUR/m²)',
            format_money(comparison.scenario.official_total_cost_per_m2, unit='EUR/m²'),
            delta=f'{comparison.delta_total_cost_per_m2:+,.2f} EUR/m²' if comparison.delta_total_cost_per_m2 is not None else None
        )
        cols[3].metric(
            'Priced / Expected Days',
            f'{comparison.scenario.priced_days} / {comparison.scenario.expected_days}'
        )

    if baseline_cost.official_total_cost_eur is None and baseline_cost.observed_total_cost_eur is not None:
        st.info(
            f'ℹ️ **Chi phí quan sát được**: {format_money(baseline_cost.observed_total_cost_eur)} '
            f'({format_money(baseline_cost.observed_total_cost_per_m2, unit="EUR/m²")}) — '
            f'Tổng chính thức là NULL do: {"; ".join(baseline_cost.reason_codes) or "Chưa đủ điều kiện định giá/lượng toàn kỳ"}.'
        )

    # Basis & Sensitivity Note
    if baseline_cost.basis_status == BasisStatus.ASSUMED:
        st.warning(
            f'⚠️ **{baseline_cost.basis_label or "Giả định chưa xác minh"} (`AGC_SHARED_BASIS_V1:v1`)**: '
            'Diện tích trồng 62.5 m² dùng cho chi phí tuyệt đối EUR là giả định phân tích, không phải dữ liệu đo lường. '
            f'Phân tích độ nhạy (Resources 96 m² / 62.5 m², hệ số 1.536): '
            f'Tổng chi phí EUR độ nhạy = **{format_money(baseline_cost.sensitivity_total_cost_eur)}**'
            + (f' (Kịch bản: **{format_money(scenario_cost.sensitivity_total_cost_eur)}**)' if scenario_cost else '') + '.'
        )
    elif baseline_cost.basis_label:
        st.info(f'Basis: {baseline_cost.basis_label}')

    if baseline_cost.reason_codes:
        st.info('Lý do / điều kiện chi phí: ' + ' · '.join(baseline_cost.reason_codes))

    # Tabs for structured exploration
    tab_comp, tab_days, tab_rates, tab_manage, tab_export = st.tabs([
        '📊 Đóng góp thành phần',
        '📅 Chi tiết ngày & Phép nhân',
        '📋 Biểu giá có hiệu lực',
        '⚙️ Tạo / Sửa đổi kịch bản',
        '💾 Xuất dữ liệu (Exports)',
    ])

    with tab_comp:
        st.subheader('Chi phí theo thành phần năng lượng')
        if comparison is None:
            comp_rows = []
            for c in baseline_cost.components:
                comp_rows.append({
                    'Thành phần': c.component.value,
                    'Đơn vị giá': c.price_unit,
                    'Chi phí chính thức (EUR/m²)': c.official_cost_per_m2,
                    'Chi phí chính thức (EUR)': c.official_cost_eur,
                    'Chi phí quan sát (EUR/m²)': c.observed_cost_per_m2,
                    'Chi phí quan sát (EUR)': c.observed_cost_eur,
                    'Số ngày định giá': f'{c.priced_days}/{c.expected_days}',
                    'Coverage định giá (%)': c.pricing_coverage_pct,
                    'Số ngày lượng accepted': f'{c.quantity_accepted_days}/{c.expected_days}',
                    'Coverage lượng (%)': c.quantity_coverage_pct,
                    'Lý do': ';'.join(c.reason_codes) or 'Đầy đủ',
                })
            st.dataframe(presentation_rows(comp_rows), width='stretch', hide_index=True)
        else:
            comp_rows = []
            delta_map = {d.component: d for d in comparison.component_deltas}
            b_map = {c.component: c for c in comparison.baseline.components}
            s_map = {c.component: c for c in comparison.scenario.components}

            for comp in (EnergyComponent.HEAT, EnergyComponent.ELEC_HIGH, EnergyComponent.ELEC_LOW):
                bc = b_map.get(comp)
                sc = s_map.get(comp)
                d = delta_map.get(comp)
                if bc and sc and d:
                    comp_rows.append({
                        'Thành phần': comp.value,
                        'Đơn vị': bc.price_unit,
                        'Baseline (EUR)': bc.official_cost_eur,
                        'Kịch bản (EUR)': sc.official_cost_eur,
                        'Delta (EUR)': d.delta_cost_eur,
                        'Delta (%)': f'{d.delta_cost_pct:+.2f}%' if d.delta_cost_pct is not None else 'NULL',
                        'Tỷ trọng Baseline (%)': f'{d.baseline_share_pct:.1f}%' if d.baseline_share_pct is not None else 'NULL',
                        'Tỷ trọng Kịch bản (%)': f'{d.scenario_share_pct:.1f}%' if d.scenario_share_pct is not None else 'NULL',
                        'Baseline (EUR/m²)': bc.official_cost_per_m2,
                        'Kịch bản (EUR/m²)': sc.official_cost_per_m2,
                        'Delta (EUR/m²)': d.delta_cost_per_m2,
                        'Priced Days': f'{sc.priced_days}/{sc.expected_days}',
                        'Lý do': ';'.join(sc.reason_codes) or 'Đầy đủ',
                    })
            st.dataframe(presentation_rows(comp_rows), width='stretch', hide_index=True)

    with tab_days:
        st.subheader('Truy vết lượng × giá chi tiết')
        st.caption('Kiểm tra phép tính chi phí theo ngày, tuần hoặc toàn vụ, kết nối trực tiếp với nguồn dữ liệu gốc Resources.csv.')

        grain = st.radio(
            'Mức tổng hợp (Grain)',
            ['Theo ngày (Daily)', 'Theo tuần (Weekly)', 'Toàn vụ (Cycle)'],
            horizontal=True,
            key='cost_grain_selector'
        )

        active_summary = scenario_cost if scenario_cost is not None else baseline_cost

        if grain == 'Theo tuần (Weekly)':
            if comparison is not None:
                weekly_comps = cost_svc.compare_scenarios_weekly(comparison)
                w_rows = []
                for w in weekly_comps:
                    w_rows.append({
                        'Tuần': f'{w.iso_week} ({w.start_date.isoformat()} đến {w.end_date.isoformat()})',
                        'Số ngày': w.days_count,
                        'Baseline (EUR)': w.baseline_total_eur,
                        'Kịch bản (EUR)': w.scenario_total_eur,
                        'Delta (EUR)': w.delta_total_eur,
                        'Delta (%)': f'{w.delta_total_pct:+.2f}%' if w.delta_total_pct is not None else 'NULL',
                        'Baseline (EUR/m²)': w.baseline_total_per_m2,
                        'Kịch bản (EUR/m²)': w.scenario_total_per_m2,
                        'Delta (EUR/m²)': w.delta_total_per_m2,
                        'Heat Delta (EUR)': w.heat_delta_eur,
                        'Elec High Delta (EUR)': w.elec_high_delta_eur,
                        'Elec Low Delta (EUR)': w.elec_low_delta_eur,
                    })
                st.dataframe(presentation_rows(w_rows), width='stretch', hide_index=True)
            else:
                weekly_costs = cost_svc.get_weekly_cost(baseline_cost)
                w_rows = []
                for w in weekly_costs:
                    w_rows.append({
                        'Tuần': f'{w.iso_week} ({w.start_date.isoformat()} đến {w.end_date.isoformat()})',
                        'Số ngày': w.days_count,
                        'Số ngày định giá': w.priced_days,
                        'Số ngày hoàn thành': w.quantity_complete_days,
                        'Tổng chi phí (EUR)': w.total_cost_eur,
                        'Tổng chi phí (EUR/m²)': w.total_cost_per_m2,
                        'Heat (EUR)': w.heat_cost_eur,
                        'Elec High (EUR)': w.elec_high_cost_eur,
                        'Elec Low (EUR)': w.elec_low_cost_eur,
                        'Diện tích hiệu dụng (m²)': w.effective_area_m2,
                        'Lý do': ';'.join(w.reason_codes) or 'Đầy đủ',
                    })
                st.dataframe(presentation_rows(w_rows), width='stretch', hide_index=True)

        elif grain == 'Toàn vụ (Cycle)':
            st.write(f'**Tổng hợp toàn vụ**: {eval_context.start} đến {eval_context.end}')
            cycle_rows = []
            for c in active_summary.components:
                cycle_rows.append({
                    'Thành phần': c.component.value,
                    'Đơn vị giá': c.price_unit,
                    'Tổng chi phí chính thức (EUR/m²)': c.official_cost_per_m2,
                    'Tổng chi phí chính thức (EUR)': c.official_cost_eur,
                    'Tổng chi phí quan sát (EUR/m²)': c.observed_cost_per_m2,
                    'Tổng chi phí quan sát (EUR)': c.observed_cost_eur,
                    'Số ngày định giá': f'{c.priced_days}/{c.expected_days}',
                    'Coverage định giá (%)': c.pricing_coverage_pct,
                    'Coverage lượng (%)': c.quantity_coverage_pct,
                    'Mã lý do': ';'.join(c.reason_codes) or 'Đầy đủ',
                })
            st.dataframe(presentation_rows(cycle_rows), width='stretch', hide_index=True)

        else:
            daily_rows = []
            for c in active_summary.components:
                for dc in c.day_costs:
                    q = dc.quantity
                    rate_val = dc.rate.rate if dc.rate else None
                    rate_unit = dc.rate.price_unit if dc.rate else ''
                    daily_rows.append({
                        'Ngày': dc.resource_date.isoformat(),
                        'Thành phần': dc.component.value,
                        'Lượng nguồn': q.source_value,
                        'Đơn vị nguồn': q.source_unit,
                        'Diện tích (m²)': q.basis.area_m2 if q.basis else None,
                        'Lượng tuyệt đối (kWh)': q.absolute_kwh,
                        'Đơn giá': format_rate(rate_val, rate_unit),
                        'Chi phí (EUR/m²)': dc.cost_per_m2,
                        'Chi phí (EUR)': dc.cost_eur,
                        'Quality status': q.quality.value,
                        'Lý do': dc.reason_code or q.reason_code or 'Accepted',
                        'Revision ID': q.revision_id,
                    })
            daily_rows.sort(key=lambda r: (r['Ngày'], r['Thành phần']))
            st.dataframe(presentation_rows(daily_rows, rate_keys={'Đơn giá'}), width='stretch', hide_index=True)

        with st.expander('Truy vết nguồn gốc dữ liệu (Source Trace Lineage & Checksums)'):
            st.caption('Truy vết từ bản ghi lượng năng lượng tới file nguồn Resources.csv, số dòng, mã hash và trạng thái xác thực.')
            energy_svc = getattr(cost_svc, 'energy', None)

            quantities_map = {}
            for c in active_summary.components:
                for dc in c.day_costs:
                    if dc.quantity and dc.quantity.revision_id:
                        lbl = f"{dc.resource_date} · {c.component.value} · rev:{dc.quantity.revision_id[:8]}"
                        quantities_map[lbl] = (dc.quantity, dc.quantity.revision_id)

            if quantities_map and energy_svc:
                options = list(quantities_map.keys())
                selected_label = st.selectbox(
                    'Chọn bản ghi năng lượng để truy vết nguồn (ngày · component · revision)',
                    options,
                    key='cost_lineage_select'
                )
                selected_qty, selected_rev = quantities_map[selected_label]
                effective_pub = publication if eval_context.publication_id == publication.publication_id else cost_svc.energy.adapter.publication(eval_context.publication_id)
                try:
                    lin = energy_svc.get_lineage(effective_pub, revision_id=selected_rev)
                    st.write(f'**File nguồn**: `{lin.relative_path}` · **Dòng nguồn**: {lin.line_number}–{lin.line_end}')
                    st.write(f'**Xác minh hash raw**: {"✅ Hợp lệ" if lin.raw_hash_verified else "❌ Không khớp"}')
                    c1, c2 = st.columns(2)
                    with c1:
                        st.caption(f'Revision ID: `{lin.revision_id}`')
                        st.caption(f'Logical Record Key: `{lin.logical_record_key}`')
                        st.caption(f'File Checksum: `{lin.file_checksum}`')
                        st.caption(f'Raw Hash: `{lin.raw_hash}`')
                    with c2:
                        st.caption(f'Thời điểm: `{lin.source_timestamp}`')
                        st.caption(f'Basis: `{selected_qty.basis.area_m2} m² ({selected_qty.basis.status.value})`' if selected_qty.basis else 'Basis: N/A')
                        st.caption(f'Quality Status: `{selected_qty.quality.value}`' if selected_qty.quality else 'Quality: N/A')
                        st.caption(f'Measure Units: `{", ".join(f"{u.measure_code}: {u.unit}" for u in lin.units)}`')
                    st.json(__import__('greenhouse.contracts', fromlist=['to_dict']).to_dict(lin))
                except (OSError, ValueError) as err:
                    st.warning(f'Không xác minh được raw lineage: {err}')
                except Exception as err:
                    st.error(f'Lỗi khi truy vết lineage: {err}')
                    st.error(f'Lỗi khi truy vết lineage: {err}')
            else:
                st.info('Không có thông tin truy vết lineage cho khoảng thời gian này.')

    with tab_rates:
        st.subheader('Biểu giá và khoảng hiệu lực')
        st.write('**Biểu giá tham chiếu baseline**:', effective_baseline_profile.label, f'(`{effective_baseline_profile.tariff_version}`)')
        st.caption(f'Nguồn tham chiếu: {effective_baseline_profile.source_reference} · Đơn vị tiền tệ: {effective_baseline_profile.currency}')

        rates_rows = []
        for r in effective_baseline_profile.rates:
            equiv = ''
            if r.component == EnergyComponent.HEAT:
                equiv = heat_equivalent_display(r.rate, r.price_unit)
            rates_rows.append({
                'Loại': 'Baseline',
                'Thành phần': r.component.value,
                'Đơn vị': r.price_unit,
                'Đơn vị giá': format_rate(r.rate) + equiv,
                'Hiệu lực từ': r.valid_from.isoformat(),
                'Đến trước': r.valid_to_exclusive.isoformat(),
                'Nguồn': r.source_reference,
            })

        if active_scenario is not None:
            for r in active_scenario.rates:
                equiv = ''
                if r.component == EnergyComponent.HEAT:
                    equiv = heat_equivalent_display(r.rate, r.price_unit)
                rates_rows.append({
                    'Loại': f'Kịch bản v{active_scenario.version}',
                    'Thành phần': r.component.value,
                    'Đơn vị': r.price_unit,
                    'Đơn vị giá': format_rate(r.rate) + equiv,
                    'Hiệu lực từ': r.valid_from.isoformat(),
                    'Đến trước': r.valid_to_exclusive.isoformat(),
                    'Nguồn': r.source_reference,
                })

        st.dataframe(presentation_rows(rates_rows, rate_keys={'Đơn vị giá'}), width='stretch', hide_index=True)

    with tab_manage:
        st.subheader('Quản lý kịch bản biểu giá')
        st.markdown(
            'Người dùng có thể tạo kịch bản mới hoặc tạo phiên bản sửa đổi (vN+1) cho một kịch bản đã có. '
            'Baseline tham chiếu là bất biến. Mọi kịch bản đều được lưu phiên bản và có thể mở lại chính xác.'
        )

        mode = st.radio(
            'Hành động',
            ['Tạo kịch bản mới', 'Thêm phiên bản sửa đổi (Revision) cho kịch bản đang chọn'],
            key='scenario_form_mode'
        )

        target_scenario_id = None
        default_name = 'Kịch bản tăng giá điện 20%'
        target_context = context
        target_pub = publication
        target_baseline = target_baseline_version

        if mode == 'Thêm phiên bản sửa đổi (Revision) cho kịch bản đang chọn':
            if active_scenario is None:
                st.warning('Vui lòng chọn một kịch bản có sẵn ở thanh bên trước khi thêm phiên bản sửa đổi.')
                st.stop()
            target_scenario_id = active_scenario.scenario_id
            default_name = f'{active_scenario.name} (v{active_scenario.version + 1})'
            target_context = active_scenario.analysis_context
            target_pub = active_scenario.publication
            target_baseline = active_scenario.baseline_tariff_version
            st.info(f'Đang sửa đổi kịch bản: **{active_scenario.name}** (`{target_scenario_id}`) · Phiên bản hiện tại: v{active_scenario.version}')

        with st.form(key='scenario_create_form'):
            scen_name = st.text_input('Tên kịch bản', value=default_name, key='scen_name')

            st.write('**1. Nhiệt sưởi (Heat - Heat_cons, gốc 0.0083 EUR/MJ)**')
            heat_choice = st.selectbox(
                'Hình thức thay đổi nhiệt',
                ['Giữ nguyên', 'Thay đổi theo phần trăm (%)', 'Đơn giá cố định mới (EUR/MJ)'],
                key='form_heat_choice'
            )
            heat_pct_input = st.text_input('Phần trăm thay đổi nhiệt (%)', value='0.0', key='form_heat_pct')
            heat_val_input = st.text_input('Đơn giá nhiệt mới (EUR/MJ)', value='0.0083', key='form_heat_val')

            st.write('**2. Điện cao điểm (Electric High - ElecHigh, gốc 0.08 EUR/kWh)**')
            high_choice = st.selectbox(
                'Hình thức thay đổi điện cao điểm',
                ['Giữ nguyên', 'Thay đổi theo phần trăm (%)', 'Đơn giá cố định mới (EUR/kWh)'],
                index=1,
                key='form_high_choice'
            )
            high_pct_input = st.text_input('Phần trăm thay đổi điện cao điểm (%)', value='20.0', key='form_high_pct')
            high_val_input = st.text_input('Đơn giá điện cao điểm mới (EUR/kWh)', value='0.096', key='form_high_val')

            st.write('**3. Điện thấp điểm (Electric Low - ElecLow, gốc 0.04 EUR/kWh)**')
            low_choice = st.selectbox(
                'Hình thức thay đổi điện thấp điểm',
                ['Giữ nguyên', 'Thay đổi theo phần trăm (%)', 'Đơn giá cố định mới (EUR/kWh)'],
                index=1,
                key='form_low_choice'
            )
            low_pct_input = st.text_input('Phần trăm thay đổi điện thấp điểm (%)', value='20.0', key='form_low_pct')
            low_val_input = st.text_input('Đơn giá điện thấp điểm mới (EUR/kWh)', value='0.048', key='form_low_val')

            submitted = st.form_submit_button('Lưu kịch bản bất biến', key='btn_save_scenario')

        if submitted:
            try:
                changes = []
                # Heat change
                if heat_choice == 'Thay đổi theo phần trăm (%)':
                    pct = parse_decimal_input(heat_pct_input, 'Phần trăm thay đổi nhiệt', min_val=Decimal('-100'))
                    if pct != 0:
                        changes.append(ScenarioChange(
                            component=EnergyComponent.HEAT,
                            price_unit='EUR/MJ',
                            change_percent=pct,
                        ))
                elif heat_choice == 'Đơn giá cố định mới (EUR/MJ)':
                    pr = parse_decimal_input(heat_val_input, 'Đơn giá nhiệt', min_val=Decimal('0'))
                    changes.append(ScenarioChange(
                        component=EnergyComponent.HEAT,
                        price_unit='EUR/MJ',
                        price=pr,
                    ))

                # Elec High change
                if high_choice == 'Thay đổi theo phần trăm (%)':
                    pct = parse_decimal_input(high_pct_input, 'Phần trăm thay đổi điện cao điểm', min_val=Decimal('-100'))
                    if pct != 0:
                        changes.append(ScenarioChange(
                            component=EnergyComponent.ELEC_HIGH,
                            price_unit='EUR/kWh',
                            change_percent=pct,
                        ))
                elif high_choice == 'Đơn giá cố định mới (EUR/kWh)':
                    pr = parse_decimal_input(high_val_input, 'Đơn giá điện cao điểm', min_val=Decimal('0'))
                    changes.append(ScenarioChange(
                        component=EnergyComponent.ELEC_HIGH,
                        price_unit='EUR/kWh',
                        price=pr,
                    ))

                # Elec Low change
                if low_choice == 'Thay đổi theo phần trăm (%)':
                    pct = parse_decimal_input(low_pct_input, 'Phần trăm thay đổi điện thấp điểm', min_val=Decimal('-100'))
                    if pct != 0:
                        changes.append(ScenarioChange(
                            component=EnergyComponent.ELEC_LOW,
                            price_unit='EUR/kWh',
                            change_percent=pct,
                        ))
                elif low_choice == 'Đơn giá cố định mới (EUR/kWh)':
                    pr = parse_decimal_input(low_val_input, 'Đơn giá điện thấp điểm', min_val=Decimal('0'))
                    changes.append(ScenarioChange(
                        component=EnergyComponent.ELEC_LOW,
                        price_unit='EUR/kWh',
                        price=pr,
                    ))

                if not changes:
                    st.error('Vui lòng chọn ít nhất một thay đổi giá cho kịch bản.')
                    st.stop()

                # Derive stable request_id from canonical submitted intent payload
                intent_dict = {
                    'mode': mode,
                    'target_scenario_id': target_scenario_id,
                    'name': scen_name.strip() or 'Kịch bản giá',
                    'baseline_version': target_baseline,
                    'publication_id': target_pub.publication_id,
                    'changes': [
                        {
                            'component': c.component.value,
                            'price_unit': c.price_unit,
                            'change_percent': str(c.change_percent) if c.change_percent is not None else None,
                            'price': str(c.price) if c.price is not None else None,
                        } for c in changes
                    ],
                    'context': {
                        'scope_id': target_context.scope_id,
                        'snapshot_id': target_context.snapshot_id,
                        'cycle_id': target_context.cycle_id,
                        'compartment_ids': list(target_context.compartment_ids),
                        'start': target_context.start.isoformat(),
                        'end': target_context.end.isoformat(),
                    } if target_context else None,
                }
                canonical_intent = json.dumps(intent_dict, sort_keys=True)
                intent_hash = hashlib.sha256(canonical_intent.encode('utf-8')).hexdigest()

                if 'cost_submitted_intents' not in st.session_state:
                    st.session_state['cost_submitted_intents'] = {}

                if intent_hash not in st.session_state['cost_submitted_intents']:
                    st.session_state['cost_submitted_intents'][intent_hash] = f'req-{uuid4().hex}'
                request_id = st.session_state['cost_submitted_intents'][intent_hash]

                cmd = ScenarioCommand(
                    request_id=request_id,
                    baseline_version_id=target_baseline,
                    name=scen_name.strip() or 'Kịch bản giá',
                    publication=target_pub,
                    changes=tuple(changes),
                    scenario_id=target_scenario_id,
                    analysis_context=target_context,
                )

                if target_scenario_id is not None:
                    saved_scen = cost_svc.append_scenario_version(target_scenario_id, cmd)
                else:
                    saved_scen = cost_svc.execute_scenario_command(cmd)

                # Explicit transaction ownership: commit to make durable across st.rerun / connection close
                if not is_clickhouse(connection):
                    connection.commit()

            except (ValueError, psycopg.Error) as e:
                if not is_clickhouse(connection):
                    connection.rollback()
                st.error(f'Lỗi khi lưu kịch bản: {e}')
            except Exception as e:
                if not is_clickhouse(connection):
                    connection.rollback()
                st.error(f'Lỗi khi lưu kịch bản: {e}')
            else:
                st.session_state['pending_scenario_select'] = f'{saved_scen.name} ({saved_scen.scenario_version})'
                st.rerun()

    with tab_export:
        st.subheader('Xuất dữ liệu chi phí đã pin')
        st.caption('Xuất chính xác số liệu DTO với toàn bộ độ chính xác Decimal, nhãn giả định và siêu dữ liệu.')

        active_comp = comparison if comparison is not None else compare_summaries(baseline_cost, baseline_cost)

        stem = f'cost-{eval_compartment_name}-{eval_context.start}-{eval_context.end}'
        scen_tag = active_scenario.scenario_version.replace(':', '_') if active_scenario else 'baseline'

        col1, col2 = st.columns(2)
        with col1:
            st.download_button(
                'CSV Summary chi phí (Baseline vs Kịch bản)',
                exports.summary_csv(active_comp, active_scenario),
                file_name=f'{stem}-{scen_tag}-summary.csv',
                mime='text/csv',
                key='cost_download_summary'
            )
            st.download_button(
                'CSV Chi phí thành phần (Component Costs)',
                exports.components_csv(active_comp, active_scenario),
                file_name=f'{stem}-{scen_tag}-components.csv',
                mime='text/csv',
                key='cost_download_components'
            )
            st.download_button(
                'CSV Chi tiết từng ngày (Daily Costs & Rates)',
                exports.days_csv(active_comp, active_scenario),
                file_name=f'{stem}-{scen_tag}-days.csv',
                mime='text/csv',
                key='cost_download_days'
            )
            st.download_button(
                'CSV Chi tiết từng tuần (Weekly Costs & Rates)',
                exports.weekly_csv(active_comp, active_scenario),
                file_name=f'{stem}-{scen_tag}-weekly.csv',
                mime='text/csv',
                key='cost_download_weekly'
            )

        with col2:
            st.download_button(
                'CSV Biểu giá hiệu lực (Tariff & Scenario Rates)',
                exports.rates_csv(
                    active_comp,
                    effective_baseline_profile.rates,
                    active_scenario.rates if active_scenario else None,
                    active_scenario,
                ),
                file_name=f'{stem}-{scen_tag}-rates.csv',
                mime='text/csv',
                key='cost_download_rates'
            )
            st.download_button(
                'JSON Chi phí (ScenarioComparison typed)',
                exports.comparison_json(active_comp),
                file_name=f'{stem}-{scen_tag}-comparison.json',
                mime='application/json',
                key='cost_download_comp_json'
            )
            if active_scenario is not None:
                st.download_button(
                    'JSON Kịch bản DTO (CostScenario typed)',
                    exports.scenario_json(active_scenario),
                    file_name=f'{stem}-{scen_tag}-scenario.json',
                    mime='application/json',
                    key='cost_download_scen_json'
                )
            else:
                st.download_button(
                    'JSON Baseline DTO (EnergyCostSummary typed)',
                    exports.to_json(baseline_cost).encode('utf-8'),
                    file_name=f'{stem}-baseline-summary.json',
                    mime='application/json',
                    key='cost_download_base_json'
                )
