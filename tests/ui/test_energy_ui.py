"""BR04 real fixture screen navigation, NULL, source and exact export contracts."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import csv,io
import pytest

from greenhouse.app import wiring, energy_exports
from greenhouse.contracts import EnergyIntensity, EnergyPortfolio, from_json
from ui.energy_fixture import energy_scope
from test_climate_ui import run_app, metric

SCREEN='⚡ Năng lượng (BR-04)'


def open_energy(run_app,kwargs,e):
    at=run_app(kwargs)
    at.radio(key='screen_choice').set_value(SCREEN).run()
    assert not at.exception, [x.value for x in at.exception]
    assert not at.error, [x.value for x in at.error]
    return at


def test_energy_screen_500_over_100_not_per_area_or_ab(energy_scope,contracts_database,run_app,monkeypatch):
    e=energy_scope()
    monkeypatch.setenv('GREENHOUSE_RAW_DIR',str(e.raw))
    at=open_energy(run_app,contracts_database,e)
    assert metric(at,'Năng lượng (kWh)')=='500.000'
    assert metric(at,'Sản lượng A (kg)')=='100.000'
    assert metric(at,'kWh/kg A')=='5.000'
    assert metric(at,'Portfolio kWh/kg A · ΣE / ΣA')=='5.000'
    assert len(at.download_button)==6
    assert len(at.get('plotly_chart'))==1
    at.button(key='energy_lineage').click().run()
    assert not at.exception
    assert any('File Synthetic/' in w.value for w in at.markdown)
    # A-only preharvest subwindow remains a visible positive numerator/NULL ratio.
    at.date_input(key='energy_window').set_value((e.context.start,e.context.start)).run()
    assert not at.exception
    assert metric(at,'Năng lượng (kWh)')=='200.000'
    assert metric(at,'kWh/kg A')=='NULL'
    assert any('descriptive_subwindow' in x.value for x in at.info)
    assert metric(at,'Portfolio kWh/kg A · ΣE / ΣA')=='5.000'


@pytest.mark.parametrize('options,reason',[
    ({'rows':(('3.6','','0'),('7.2','0','1'))},'incomplete_energy_quantity'),
    ({'harvest_rows':((-321,'2','1'),)},'harvest_snapshot_incomplete'),
    ({'basis_status':'unknown'},'incompatible_or_unknown_basis')])
def test_energy_null_retains_quantities_and_excluded(options,reason,energy_scope,contracts_database,run_app):
    e=energy_scope(**options)
    at=open_energy(run_app,contracts_database,e)
    assert metric(at,'kWh/kg A')=='NULL'
    assert metric(at,'Portfolio kWh/kg A · ΣE / ΣA')=='NULL'
    assert any(reason in x.value for x in at.info)
    assert len(at.download_button)==6
    assert any('Không đủ điều kiện' in x.value for x in at.warning)


def test_energy_assumption_sensitivity_and_exact_export(energy_scope,contracts_database,run_app):
    e=energy_scope(basis_status='assumed_unverified',production_area='62.5',production_kind='production_area')
    at=open_energy(run_app,contracts_database,e)
    assert metric(at,'kWh/kg A')=='2.500'
    assert any('AGC_SHARED_BASIS_V1:v1' in w.value and '1.536' in w.value for w in at.warning)
    s=wiring.intensity_service(e.conn)
    r=s.get_intensity(e.context)
    q=s.energy.get_quantities(e.context)
    assert from_json(EnergyIntensity,energy_exports.result_json(r))==r
    p=s.get_portfolio((e.context,))
    assert from_json(EnergyPortfolio,energy_exports.portfolio_json(p))==p
    data=energy_exports.summary_csv(r).decode('utf-8-sig')
    rows=list(csv.DictReader(io.StringIO('\n'.join(x for x in data.splitlines() if not x.startswith('#')))))
    assert Decimal(rows[0]['intensity_kwh_kg_a'])==Decimal('2.5')
    assert rows[0]['basis_label']=='giả định chưa xác minh'
    assert Decimal(rows[0]['sensitivity_kwh_kg_a'])==Decimal('3.84')
    assert b'quantity_version' in energy_exports.quantities_csv(r,q)
    assert e.publication.publication_id.encode() in energy_exports.portfolio_csv(p)
    # Widget scope change resets its date window instead of leaking subwindow.
    at.date_input(key='energy_window').set_value((e.context.start,e.context.start)).run()
    other=energy_scope(rows=(('0','0','0'),('0','0','0')),production_area='50')
    at.run()
    at.selectbox(key='energy_publication').select_index(0).run()
    assert not at.exception
    assert tuple(at.date_input(key='energy_window').value)==(other.context.start,other.context.end-timedelta(days=1))


def test_unknown_basis_exposes_aggregated_source_per_area_and_table_presentation(energy_scope, contracts_database, run_app):
    e = energy_scope(basis_status='unknown', rows=(('3.6', '0', '0'), ('7.2', '0', '0')))
    at = open_energy(run_app, contracts_database, e)
    assert metric(at, 'kWh/kg A') == 'NULL'
    assert metric(at, 'Năng lượng (kWh)') == 'NULL'
    assert any('incompatible_or_unknown_basis' in x.value for x in at.info)
    frames = [item.value for item in at.dataframe]
    comp_df = frames[0]
    heat_row = comp_df[comp_df['component'] == 'heat'].iloc[0]
    assert heat_row['source_unit'] == 'MJ/m²'
    assert heat_row['observed_source_per_m2'] == '10.800'
    assert heat_row['observed_kwh_m2'] == '3.000'
    assert heat_row['observed_kwh'] == 'NULL'
    assert heat_row['official_kwh'] == 'NULL'
    port_df = frames[1]
    assert not port_df.iloc[0]['included_in_portfolio']
    assert 'no_comparable_eligible_population' in port_df.iloc[0]['portfolio_reasons']
    assert isinstance(heat_row['observed_source_per_m2'], str)


def test_mixed_comparability_portfolio_surfaces_reasons_in_ui_and_csv(energy_scope):
    e = energy_scope(rows=(('3.6', '0', '0'), ('7.2', '0', '0')), production_area='50', basis_status='verified')
    s = wiring.intensity_service(e.conn)
    r1 = s.get_intensity(e.context)
    context = replace(r1.context, cycle_id='cycle:second')
    cycle = replace(r1.cycle, cycle_id='cycle:second')
    rec = replace(r1.harvest_a[0].record, cycle_id='cycle:second', logical_record_key='k2')
    contribution = replace(r1.harvest_a[0], record=rec, absolute_a_kg=Decimal('100'))
    components = tuple(replace(c, official_absolute_kwh=Decimal(v)) for c, v in zip(r1.energy.components, ('120', '40', '40')))
    energy = replace(r1.energy, context=context, official_total_kwh=Decimal('200'), components=components)
    second = replace(r1, context=context, cycle=cycle, energy=energy, harvest_a=(contribution,),
        observed_a_kg=Decimal('100'), official_a_kg=Decimal('100'), intensity_kwh_kg_a=Decimal('2'),
        heat_kwh_kg_a=Decimal('1.2'), light_kwh_kg_a=Decimal('.8'), assumptions=('different:v2',))
    from greenhouse.queries.intensity import portfolio
    p = portfolio((r1, second))
    assert 'mixed_windows_or_assumption_versions' in p.reason_codes
    csv_bytes = energy_exports.portfolio_csv(p)
    assert b'# portfolio_reasons=no_comparable_eligible_population;mixed_windows_or_assumption_versions' in csv_bytes
    data = csv_bytes.decode('utf-8-sig')
    rows = list(csv.DictReader(io.StringIO('\n'.join(x for x in data.splitlines() if not x.startswith('#')))))
    assert 'mixed_windows_or_assumption_versions' in rows[0]['portfolio_reasons']
    assert rows[0]['included_in_portfolio'] == 'False'


def test_nonterminating_heat_table_cells_render_formatted_strings(energy_scope, contracts_database, run_app, monkeypatch):
    # Heat 1 MJ and 2 MJ / 3.6 yields non-terminating repeating decimal:
    # 3 MJ / 3.6 * 100 m² = 83.3333333... kWh
    e = energy_scope(rows=(('1', '0', '0'), ('2', '0', '0')), basis_status='verified', production_area='50')
    monkeypatch.setenv('GREENHOUSE_RAW_DIR', str(e.raw))
    at = open_energy(run_app, contracts_database, e)
    assert metric(at, 'Năng lượng (kWh)') == '83.333'
    frames = [item.value for item in at.dataframe]
    comp_df = frames[0]
    heat_row = comp_df[comp_df['component'] == 'heat'].iloc[0]
    assert heat_row['observed_kwh'] == '83.333'
    assert heat_row['official_kwh'] == '83.333'
    assert heat_row['observed_kwh_m2'] == '0.833'
    assert isinstance(heat_row['observed_kwh'], str)
    assert not isinstance(heat_row['observed_kwh'], Decimal)
    assert all(not isinstance(val, Decimal) for val in comp_df.values.ravel())
    port_df = frames[1]
    port_row = port_df.iloc[0]
    assert port_row['energy_kwh'] == '83.333'
    assert port_row['heat_kwh'] == '83.333'
    assert isinstance(port_row['energy_kwh'], str)
    assert not isinstance(port_row['energy_kwh'], Decimal)
    assert all(not isinstance(val, Decimal) for val in port_df.values.ravel())
    s = wiring.intensity_service(e.conn)
    result = s.get_intensity(e.context)
    assert isinstance(result.energy.official_total_kwh, Decimal)
    assert result.energy.official_total_kwh != Decimal('83.333')
