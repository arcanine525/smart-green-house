"""Independent bounded raw-CSV oracle: no warehouse adapters or duration engine."""
import csv
from datetime import datetime,timedelta
from decimal import Decimal,InvalidOperation,ROUND_HALF_EVEN

EPOCH=datetime(1899,12,30)
CLIMATE=('Tair','CO2air','t_heat_sp','t_vent_sp','co2_sp','t_heat_vip','t_ventlee_vip','co2_vip')
DRAIN=('EC_drain_PC','pH_drain_PC')
NULLS={'','nan','na','n/a'}


def source_rows(path,start,end):
    rows={}; conflicts=set()
    with path.open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream)
        for raw in reader:
            raw={k.strip():v for k,v in raw.items()}
            serial=Decimal(raw['%time']);slot=(serial*288).to_integral_value(rounding=ROUND_HALF_EVEN)
            when=EPOCH+timedelta(minutes=int(slot)*5)
            if not start<=when<end or abs(serial*288-slot)*300>Decimal('.5'): continue
            values={};quality={}
            for code in CLIMATE+DRAIN:
                token=raw[code].strip();value=None
                status,reason='accepted',None
                if token.lower() in NULLS:
                    status='missing';reason={'':'empty','nan':'nan_token','na':'na_token','n/a':'n_a_token'}[token.lower()]
                else:
                    try:
                        value=Decimal(token)
                        if not value.is_finite(): raise InvalidOperation
                    except InvalidOperation:
                        value=None;status,reason='invalid','invalid_numeric'
                if value is not None:
                    if code=='EC_drain_PC' and value<0: status,reason='invalid','negative_quantity'
                    elif code=='pH_drain_PC' and value==0: status,reason='suspect','demo_zero_ph_suspect'
                    elif code in ('co2_sp','co2_vip') and value==0: reason='off_mode_unverified'
                app='unknown' if value is None or code in ('co2_sp','co2_vip') and value==0 else 'applicable'
                values[code]=value;quality[code]={'status':status,'applicability':app,'reason':reason}
            row={'values':values,'quality':quality}
            if when in rows and rows[when]!=row: conflicts.add(when)
            else: rows[when]=row
    return {k:v for k,v in rows.items() if k not in conflicts}


def evaluate(rows,start,end,kind,basis,*,include_boundary_episodes=False):
    metrics=('temperature','co2') if kind=='climate' else ('ec','ph')
    reports=[];episodes=[];states={}
    for metric in metrics:
        counts=dict(expected_minutes=0,valid_minutes=0,unknown_minutes=0,out_minutes=0,suspect_minutes=0,out_low_minutes=0,out_high_minutes=0)
        run=None
        when=start
        while when<end:
            counts['expected_minutes']+=5
            row=rows.get(when);state='unknown'
            if row:
                v=row['values'];q=row['quality']
                if metric=='temperature':
                    codes=('Tair','t_heat_sp','t_vent_sp') if basis=='requested' else ('Tair','t_heat_vip','t_ventlee_vip')
                    actual=v[codes[0]];lo=v[codes[1]]-Decimal('.5') if v[codes[1]] is not None else None;hi=v[codes[2]]+Decimal('.5') if v[codes[2]] is not None else None
                elif metric=='co2':
                    codes=('CO2air','co2_sp' if basis=='requested' else 'co2_vip');actual=v[codes[0]]
                    lo=max(Decimal(0),v[codes[1]]-Decimal(50)) if v[codes[1]] is not None else None;hi=v[codes[1]]+Decimal(50) if v[codes[1]] is not None else None
                else:
                    codes=('EC_drain_PC',) if metric=='ec' else ('pH_drain_PC',);actual=v[codes[0]]
                    lo,hi=(Decimal(2),Decimal(5)) if metric=='ec' else (Decimal('5.5'),Decimal('6.5'))
                statuses=[q[c]['status'] for c in codes]
                if 'invalid' not in statuses and 'suspect' in statuses: counts['suspect_minutes']+=5
                if all(q[c]['status']=='accepted' and q[c]['applicability']=='applicable' for c in codes) and actual is not None and lo is not None and hi is not None and lo<=hi:
                    counts['valid_minutes']+=5
                    if actual<lo: state='low';counts['out_low_minutes']+=5
                    elif actual>hi: state='high';counts['out_high_minutes']+=5
                    else: state='inside'
            if state=='unknown': counts['unknown_minutes']+=5
            if state in ('low','high'):
                counts['out_minutes']+=5
                if run is None: run=when
            elif run is not None:
                episodes.append([metric,run.isoformat(),when.isoformat(),str(Decimal((when-run).total_seconds()/60))]);run=None
            states[(metric,when)]=state
            when+=timedelta(minutes=5)
        if run is not None: episodes.append([metric,run.isoformat(),end.isoformat(),str(Decimal((end-run).total_seconds()/60))])
        reports.append({'metric':metric}|{k:str(v) for k,v in counts.items()})
    return reports,episodes if include_boundary_episodes else [e for e in episodes if start.isoformat()<e[1] and e[2]<end.isoformat()]
