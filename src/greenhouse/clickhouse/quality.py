"""Pure counterpart of sensor_stream.measure_verdict, shared by all CH marts."""
from decimal import Decimal


AGC_QUALITY_DEFINITION={
    'missing':'per_measure','nonnegative':['Irr','Drain','ProdA','ProdB','EC_drain_PC'],
    'off_mode_zero':['co2_sp','co2_vip'],'out_of_target_is_quality':False,
    'suspect_zero':['pH_drain_PC'],
}


def measure_verdict(definition, record, code):
    value=record['values'].get(code)
    value=Decimal(value) if value is not None else None
    invalid=any(i.get('code')=='invalid_numeric' and i.get('column')==code for i in record['issues'])
    if invalid:
        quality,reason='invalid','invalid_numeric'
    elif value is None:
        quality,reason='missing',record['null_reasons'].get(code,'missing_source_value')
    elif code in definition.get('nonnegative',[]) and value<0:
        quality,reason='invalid','negative_quantity'
    elif code in definition.get('suspect_zero',[]) and value==0:
        quality,reason='suspect','demo_zero_ph_suspect'
    else:
        quality,reason='accepted',None
    off=value==0 and code in definition.get('off_mode_zero',[])
    applicability='unknown' if value is None or off else 'applicable'
    if not invalid and value is not None and not reason and off:
        reason='off_mode_unverified'
    return value,quality,applicability,reason
