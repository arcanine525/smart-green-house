"""Independent raw source-day energy/cost math; no greenhouse imports."""
import csv
from datetime import datetime,timedelta
from decimal import Decimal,InvalidOperation,localcontext

AREA=Decimal('62.5');EPOCH=datetime(1899,12,30)


def number(token):
    try:
        value=Decimal(token)
        return value if value.is_finite() else None
    except InvalidOperation:return None


def total(values):
    values=[v for v in values if v is not None]
    if not values:return None
    with localcontext() as ctx:
        ctx.prec=max(200,sum(len(v.as_tuple().digits) for v in values)+4)
        return sum(values,Decimal(0))


def product(a,b):
    with localcontext() as ctx:
        ctx.prec=max(100,len(a.as_tuple().digits)+len(b.as_tuple().digits)+4)
        return a*b


def normalize(heat):
    with localcontext() as ctx:
        ctx.prec=50
        return heat/Decimal('3.6')


def evaluate(raw,comp,start,end):
    days={};production=[];complete=True
    with (raw/comp/'Resources.csv').open(newline='') as stream:
        for r in csv.DictReader(stream):
            when=(EPOCH+timedelta(days=int(Decimal(r['%Time '])))).date()
            if start<=when<end:
                assert when not in days
                days[when]=[number(r[c]) for c in ('Heat_cons','ElecHigh','ElecLow')]
    for offset in range((end-start).days):
        row=days.get(start+timedelta(days=offset))
        if row is None or any(v is None or v<0 for v in row):complete=False
    rows=list(days.values());observed=[];baseline=[];scenario=[]
    for row in rows:
        for index,(value,rate) in enumerate(zip(row,(Decimal('.0083'),Decimal('.08'),Decimal('.04')))):
            if value is None or value<0:continue
            observed.append(normalize(product(value,AREA)) if index==0 else product(value,AREA))
            baseline.append(product(value,rate))
            scenario.append(product(value,product(rate,Decimal('1.2')) if index else rate))
    unresolved=0
    with (raw/comp/'Production.csv').open(newline='') as stream:
        for r in csv.DictReader(stream):
            serial=Decimal(r['%time']);when=(EPOCH+timedelta(days=int(serial))).date()
            a,b=number(r['ProdA']),number(r['ProdB'])
            if not Decimal(43815)<=serial<Decimal(43981) or a is None or b is None or a<0 or b<0:
                unresolved+=1;continue
            if start<=when<end:production.append(a)
    harvest_complete=unresolved==0
    observed_energy=total(observed);official_energy=observed_energy if complete else None
    a_mass=product(total(production) or Decimal(0),AREA) if harvest_complete else None
    ratio=None
    if official_energy is not None and a_mass is not None and a_mass>0:
        with localcontext() as ctx:
            ctx.prec=50;ratio=official_energy/a_mass
    b_cost=total(baseline) if complete else None;s_cost=total(scenario) if complete else None
    b_eur=product(b_cost,AREA) if b_cost is not None else None;s_eur=product(s_cost,AREA) if s_cost is not None else None
    delta=None
    if b_eur is not None and s_eur is not None:
        with localcontext() as ctx:ctx.prec=100;delta=s_eur-b_eur
    return {'official_energy_kwh':str(official_energy),'observed_energy_kwh':str(observed_energy),
        'energy_coverage':'100' if complete else None,'official_a_kg':str(a_mass),'intensity_kwh_kg_a':str(ratio),
        'harvest_complete':harvest_complete,'baseline_eur':str(b_eur),'baseline_eur_m2':str(b_cost),
        'scenario_eur':str(s_eur),'scenario_eur_m2':str(s_cost),'delta_eur':str(delta)}
