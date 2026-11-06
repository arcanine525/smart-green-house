"""Independent raw oracle and production AppTest for locked AGC BR-02."""
import csv
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
import json
import os
from pathlib import Path

from greenhouse.clickhouse.client import Client, ClickHouseSettings
from greenhouse.clickhouse.water import WaterAdapter
from greenhouse.clickhouse.water_run import AGC_SNAPSHOT, AGC_SHA
from greenhouse.contracts import QueryContext, to_dict
from greenhouse.queries.irrigation import IrrigationService

raw=Path(os.environ['GREENHOUSE_RAW_DIR'])/'agc-2nd-2019'/AGC_SHA
scope=AGC_SNAPSHOT+':historical'
client=Client(ClickHouseSettings.from_env())
adapter=WaterAdapter(client,raw_snapshots={AGC_SNAPSHOT:raw})
service=IrrigationService(adapter)
publication=service.pin_publication(scope)
comparisons=[]
full_cycle=[]
for compartment in ('AICU','Automatoes','Digilog','IUACAAS','Reference','TheAutomators'):
    with (raw/compartment/'Resources.csv').open(newline='') as stream:
        reader=csv.reader(stream)
        headers=[s.strip().lstrip('%').lower() for s in next(reader)][:7]
        source=[]; all_source=[]
        for tokens in reader:
            row=dict(zip(headers,tokens[:7]))
            when=(datetime(1899,12,30)+timedelta(days=int(Decimal(row['time'])))).date()
            if date(2019,12,16)<=when<date(2020,5,30):
                all_source.append((Decimal(row['irr']),Decimal(row['drain'])))
            if date(2020,3,2)<=when<date(2020,3,4):
                source.append((Decimal(row['irr']),Decimal(row['drain'])))
    assert len(source)==2
    irrigation=sum((v[0] for v in source),Decimal(0)); drain=sum((v[1] for v in source),Decimal(0))
    with localcontext() as context:
        context.prec=50
        ratio=drain*100/irrigation
    request=QueryContext(scope,publication.publication_id,AGC_SNAPSHOT,'agc-cycle-'+compartment,
        (compartment,),date(2020,3,2),date(2020,3,4))
    summary=service.get_irrigation_summary(request)
    assert (summary.irrigation_observed_l_m2,summary.drain_observed_l_m2,summary.drain_fraction_pct)==(irrigation,drain,ratio)
    assert summary.paired_days==2 and summary.paired_coverage_pct==100
    comparisons.append({'compartment':compartment,'irrigation':str(irrigation),'drain':str(drain),'ratio':str(ratio),'status':'passed'})
    usable=lambda value:value.is_finite() and value>=0
    observed_i=[i for i,d in all_source if usable(i)]
    observed_d=[d for i,d in all_source if usable(d)]
    pairs=[(i,d) for i,d in all_source if usable(i) and usable(d)]
    full_i=sum(observed_i,Decimal(0)); full_d=sum(observed_d,Decimal(0))
    paired_i=sum((i for i,d in pairs),Decimal(0)); paired_d=sum((d for i,d in pairs),Decimal(0))
    with localcontext() as context:
        context.prec=50
        full_ratio=paired_d*100/paired_i if paired_i else None
    full=service.get_irrigation_summary(QueryContext(scope,publication.publication_id,AGC_SNAPSHOT,
        'agc-cycle-'+compartment,(compartment,),date(2019,12,16),date(2020,5,30)))
    assert (full.irrigation_observed_l_m2,full.drain_observed_l_m2,full.drain_fraction_pct)==(full_i,full_d,full_ratio)
    assert full.expected_days==166 and full.paired_days==len(pairs)
    full_cycle.append({'compartment':compartment,'irrigation':str(full_i),'drain':str(full_d),
        'ratio':str(full_ratio),'source_rows':len(all_source),'paired_days':len(pairs),'status':'passed'})

from streamlit.testing.v1 import AppTest
app=AppTest.from_file('/workspace/src/greenhouse/app/home.py',default_timeout=240).run()
assert not app.exception,[e.message for e in app.exception]
index=next(i for i,label in enumerate(app.selectbox(key='pub_choice').options) if scope in label)
app.selectbox(key='pub_choice').select_index(index).run()
index=next(i for i,label in enumerate(app.selectbox(key='cycle_choice').options) if 'agc-cycle-Automatoes' in label)
app.selectbox(key='cycle_choice').select_index(index).run()
app.date_input(key='window').set_value((date(2020,3,2),date(2020,3,3))).run()
assert not app.exception and not app.error,([e.message for e in app.exception],[e.value for e in app.error])
metrics={item.label:item.value for item in app.metric}
assert metrics['Irrigation observed']=='9,36 L/m²' and metrics['Drain observed']=='3,89 L/m²'
assert metrics['Tỷ lệ drain (paired)']=='41,56 %'
assert any('Raw hash xác minh khớp' in item.value for item in app.success)
result={'status':'passed','scope':scope,'snapshot':AGC_SNAPSHOT,'publication':publication.publication_id,
    'source_window':['2020-03-02','2020-03-04'],'compartment_oracles':comparisons,'full_cycle_oracles':full_cycle,
    'ui_metrics':metrics,'ui_chart_count':len(app.get('plotly_chart')),
    'ui_downloads':[button.label for button in app.get('download_button')],
    'raw_hash_verified':True,'pg_env_present':any(os.environ.get(k) for k in ('PGHOST','PGDATABASE','PGUSER','PGPASSWORD'))}
print(json.dumps(result,ensure_ascii=False,indent=2))
