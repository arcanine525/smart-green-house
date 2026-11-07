"""Diagnostic HTTP/GC/CPU boundaries; this is never an acceptance benchmark."""
import argparse,cProfile,gc,io,json,pstats,time
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qsl,urlencode,urlsplit,urlunsplit
from greenhouse.app import wiring
from greenhouse.clickhouse.client import Client,ClickHouseSettings
import greenhouse.clickhouse.client as transport
from greenhouse.contracts import QueryContext,RuleBasis
from read_business_reference import fingerprint

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--publication-id',required=True);p.add_argument('--output',type=Path,required=True)
p.add_argument('--reference',type=Path,required=True);p.add_argument('--thread-controls',type=int,nargs='+',choices=(1,2),default=[2,1])
a=p.parse_args();a.output.parent.mkdir(parents=True,exist_ok=True)
report={'status':'diagnostic_in_progress','thread_controls':a.thread_controls,'calls':[]}
def save():a.output.write_text(json.dumps(report,indent=2)+'\n')
def connect():return Client(ClickHouseSettings.from_env()).begin_action(shared_proofs=True)
start=time.perf_counter()
with closing(connect()) as c:
    pub=wiring.energy_service(c).adapter.publication(a.publication_id)
    cycle=next(x for x in wiring.catalog_service(c).cycles(pub) if x.compartment_id=='Automatoes')
q=QueryContext(pub.scope_id,pub.publication_id,pub.snapshot_id,cycle.cycle_id,('Automatoes',),cycle.start,cycle.end)
report['cold_setup_seconds']=time.perf_counter()-start
reference=json.loads(a.reference.read_text());current=None;gc_started={}
def gc_event(phase,info):
    generation=info['generation']
    if phase=='start':gc_started[generation]=time.perf_counter()
    elif current is not None:
        current['gc'].append({'generation':generation,'ms':(time.perf_counter()-gc_started.pop(generation,time.perf_counter()))*1000,'collected':info['collected']})
original_request=Client._request;original_open=transport.urlopen
class Response:
    def __init__(self,original,event):self.original=original;self.event=event
    def __enter__(self):self.original.__enter__();return self
    def __exit__(self,*args):return self.original.__exit__(*args)
    def __iter__(self):return self
    def __next__(self):
        start=time.perf_counter()
        try:chunk=next(self.original)
        finally:self.event['read_ms']+=(time.perf_counter()-start)*1000
        self.event['bytes']+=len(chunk);return chunk
    def read(self,*args):
        start=time.perf_counter()
        try:chunk=self.original.read(*args)
        finally:self.event['read_ms']+=(time.perf_counter()-start)*1000
        self.event['bytes']+=len(chunk);return chunk
    def __getattr__(self,name):return getattr(self.original,name)
def open_request(request,**kwargs):
    event={'sql':request.data.decode()[:120],'open_ms':0,'read_ms':0,'bytes':0}
    start=time.perf_counter();response=original_open(request,**kwargs);event['open_ms']=(time.perf_counter()-start)*1000
    if current is not None:current['http'].append(event)
    return Response(response,event)
def request(self,sql,parameters,query_id,*,streaming=False):
    r=original_request(self,sql,parameters,query_id,streaming=streaming)
    if not streaming:
        u=urlsplit(r.full_url);params=dict(parse_qsl(u.query));params['max_threads']=str(a.threads)
        r.full_url=urlunsplit((u.scheme,u.netloc,u.path,urlencode(params),u.fragment))
    return r
Client._request=request;transport.urlopen=open_request;gc.callbacks.append(gc_event)
operations={'BR01':lambda c:wiring.climate_service(c).get_evaluation(q,basis=RuleBasis.REQUESTED,rule_version='climate-demo-b01:Automatoes:v1',use_cache=False),
    'BR04':lambda c:wiring.intensity_service(c).get_intensity(q)}
try:
    for control in a.thread_controls:
        a.threads=control
        for name,operation in operations.items():
            for index in range(35):
                current={'threads_control':a.threads,'workload':name,'phase':'warmup' if index<5 else 'sample','index':index,'http':[],'gc':[]}
                def timed_action():
                    started=time.perf_counter()
                    with closing(connect()) as c:result=operation(c)
                    current['ms']=(time.perf_counter()-started)*1000
                    current['proof_hits']=c._shared_proof_hits;current['proof_misses']=c._shared_proof_misses
                    return result
                measured=current;returned=timed_action();current=None;value=returned
                if fingerprint(value)!=reference['workloads'][name]['fingerprint']:raise ValueError('Business fingerprint differs')
                report['calls'].append(measured);save()
            print(json.dumps({'stage':name,'threads':a.threads,'sample_ms':[round(x['ms'],2) for x in report['calls'] if x['workload']==name and x['phase']=='sample' and x['threads_control']==a.threads]}),flush=True)
    profiler=cProfile.Profile()
    with closing(connect()) as c:
        profiler.enable();value=operations['BR01'](c);profiler.disable()
    if fingerprint(value)!=reference['workloads']['BR01']['fingerprint']:raise ValueError('Profiled result differs')
    stream=io.StringIO();pstats.Stats(profiler,stream=stream).sort_stats('cumulative').print_stats(45)
    a.output.with_suffix('.profile.txt').write_text(stream.getvalue())
    report.update(status='diagnostic_only',all_business_fingerprints_match=True);save()
finally:
    gc.callbacks.remove(gc_event);Client._request=original_request;transport.urlopen=original_open
