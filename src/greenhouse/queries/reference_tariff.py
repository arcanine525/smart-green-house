"""AGC historical source-price constants, not a real invoice or live price feed."""
from datetime import date
from decimal import Decimal
from greenhouse.contracts import EnergyComponent,TariffProfile,TariffRate
HISTORICAL_TARIFF_ID='AGC_2020_REFERENCE_V1'
HISTORICAL_WINDOW_START=date(2019,12,16)
HISTORICAL_WINDOW_END=date(2020,5,30)


def historical_profile(data_scope_id,compartment_id=None):
    suffix=':'+compartment_id if compartment_id else ''
    tariff_version=f'tariff:{HISTORICAL_TARIFF_ID}:{data_scope_id}{suffix}:v1'
    rates = (
        TariffRate(
            rate_id=f'rate:{tariff_version}:heat',
            tariff_version=tariff_version,
            component=EnergyComponent.HEAT,
            currency='EUR',
            price_unit='EUR/MJ',
            rate=Decimal('0.0083'),
            valid_from=HISTORICAL_WINDOW_START,
            valid_to_exclusive=HISTORICAL_WINDOW_END,
            source_reference='Economics.pdf page 2: Heat 0.0083 EUR/MJ',
        ),
        TariffRate(
            rate_id=f'rate:{tariff_version}:elec_high',
            tariff_version=tariff_version,
            component=EnergyComponent.ELEC_HIGH,
            currency='EUR',
            price_unit='EUR/kWh',
            rate=Decimal('0.08'),
            valid_from=HISTORICAL_WINDOW_START,
            valid_to_exclusive=HISTORICAL_WINDOW_END,
            source_reference='Economics.pdf page 1: ElecHigh 0.08 EUR/kWh',
        ),
        TariffRate(
            rate_id=f'rate:{tariff_version}:elec_low',
            tariff_version=tariff_version,
            component=EnergyComponent.ELEC_LOW,
            currency='EUR',
            price_unit='EUR/kWh',
            rate=Decimal('0.04'),
            valid_from=HISTORICAL_WINDOW_START,
            valid_to_exclusive=HISTORICAL_WINDOW_END,
            source_reference='Economics.pdf page 2: ElecLow 0.04 EUR/kWh',
        ),
    )
    profile = TariffProfile(
        tariff_version=tariff_version,
        tariff_id=HISTORICAL_TARIFF_ID,
        version=1,
        data_scope_id=data_scope_id,
        currency='EUR',
        source_reference='Economics.pdf pages 1-2',
        label='AGC 2020 Reference Tariff',
        rates=rates,
        is_baseline=True,
        compartment_id=compartment_id,
    )
    return profile
