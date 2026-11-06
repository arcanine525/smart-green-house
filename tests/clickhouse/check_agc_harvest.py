"""Independent AGC Production CSV oracle and existing harvest UI checks."""
import csv
from datetime import date,datetime,timedelta
from decimal import Decimal,localcontext
import json
import os
from pathlib import Path

from greenhouse.clickhouse.client import Client,ClickHouseSettings
from greenhouse.clickhouse.harvest import HarvestAdapter
from greenhouse.clickhouse.water_run import AGC_SHA,AGC_SNAPSHOT
from greenhouse.contracts import QueryContext,to_dict
from greenhouse.queries.harvest import HarvestService

raw=Path(os.environ['GREENHOUSE_RAW_DIR'])/'agc-2nd-2019'/AGC_SHA
scope=AGC_SNAPSHOT+':historical'
client=Client(ClickHouseSettings.from_env())
adapter=HarvestAdapter(client,raw_snapshots={AGC_SNAPSHOT:raw}); service=HarvestService(adapter)
publication=service.pin_publication(scope)
checks=[]
for compartment in ('AICU','Automatoes','Digilog','IUACAAS','Reference','TheAutomators'):
    with (raw/compartment/'Production.csv').open(newline='') as stream:
        reader=csv.reader(stream); next(reader)
        source=[]; outside=0
        for tokens in reader:
            when=(datetime(1899,12,30)+timedelta(days=int(Decimal(tokens[0])))).date()
            if date(2019,12,16)<=when<date(2020,5,30):
                source.append((when,Decimal(tokens[1]),Decimal(tokens[2])))
            else: outside+=1
    for start,end,label in ((date(2020,3,2),date(2020,3,4),'two-day'),(date(2019,12,16),date(2020,5,30),'full-cycle')):
        observed=[(a,b) for when,a,b in source if start<=when<end]
        valid=lambda v:v.is_finite() and v>=0
        a_values=[a for a,b in observed if valid(a)]; b_values=[b for a,b in observed if valid(b)]
        pairs=[(a,b) for a,b in observed if valid(a) and valid(b)]
        expected_a=sum(a_values,Decimal(0)) if a_values else (None if outside else Decimal(0))
        expected_b=sum(b_values,Decimal(0)) if b_values else (None if outside else Decimal(0))
        paired_a=sum((a for a,b in pairs),Decimal(0)); paired_b=sum((b for a,b in pairs),Decimal(0))
        with localcontext() as arithmetic:
            arithmetic.prec=50
            expected_share=paired_b*100/(paired_a+paired_b) if paired_a+paired_b else None
        context=QueryContext(scope,publication.publication_id,AGC_SNAPSHOT,'agc-cycle-'+compartment,(compartment,),start,end)
        summary=service.get_harvest_summary(context)
        assert summary.prod_a_observed==expected_a,(compartment,label,summary.prod_a_observed,expected_a)
        assert summary.prod_b_observed==expected_b
        assert summary.class_b_share_pct==expected_share
        assert summary.completeness.unresolved_records==outside
        checks.append({'compartment':compartment,'window':label,'prod_a':str(summary.prod_a_observed),
            'prod_b':str(summary.prod_b_observed),'paired_a':str(summary.paired_prod_a),'paired_b':str(summary.paired_prod_b),
            'share_b':str(summary.class_b_share_pct),'cumulative_a':str(summary.cumulative_a),
            'snapshot_complete':summary.completeness.snapshot_complete,'unresolved':outside,'status':'passed'})
from streamlit.testing.v1 import AppTest
app=AppTest.from_file('/workspace/src/greenhouse/app/home.py',default_timeout=240).run()
app.radio(key='screen_choice').set_value('🍅 Thu hoạch (BR-05)').run()
assert not app.exception,[e.message for e in app.exception]
index=next(i for i,label in enumerate(app.selectbox(key='harv_pub_choice').options) if scope in label)
app.selectbox(key='harv_pub_choice').select_index(index).run()
index=next(i for i,label in enumerate(app.selectbox(key='harv_cycle_choice').options) if 'agc-cycle-Digilog' in label)
app.selectbox(key='harv_cycle_choice').select_index(index).run()
with (raw/'Digilog/Production.csv').open(newline='') as stream:
    first=next(csv.DictReader(stream))
    first_day=(datetime(1899,12,30)+timedelta(days=int(Decimal(first['%time'])))).date()
app.date_input(key='harv_window').set_value((first_day,first_day)).run()
assert not app.exception and not app.error,([e.message for e in app.exception],[e.value for e in app.error])
metrics={item.label:item.value for item in app.metric}
assert metrics['A observed']=='0,05 kg/m²' and metrics['B observed']=='0,10 kg/m²' and metrics['Share B (paired)']=='66,67 %',metrics
index=next(i for i,label in enumerate(app.selectbox(key='harv_cycle_choice').options) if 'agc-cycle-Reference' in label)
app.selectbox(key='harv_cycle_choice').select_index(index).run()
assert not app.exception,[e.message for e in app.exception]
reference_metrics={item.label:item.value for item in app.metric}
assert any('snapshot: chưa hoàn chỉnh' in item.value for item in app.markdown)
assert not app.error,[e.value for e in app.error]
print(json.dumps({'status':'passed','publication':publication.publication_id,'snapshot':AGC_SNAPSHOT,
    'comparisons':checks,'ui_digilog_first_harvest':metrics,'ui_digilog_window':[first_day.isoformat(),(first_day+timedelta(days=1)).isoformat()],'ui_reference':reference_metrics,
    'pg_env_present':any(os.environ.get(k) for k in ('PGHOST','PGDATABASE','PGUSER','PGPASSWORD'))},ensure_ascii=False,indent=2))
