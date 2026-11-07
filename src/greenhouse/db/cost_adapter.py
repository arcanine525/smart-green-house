"""Tariff and scenario persistence adapter for BR-06 energy cost (T20).

Enforces immutable tariff profiles, rate interval exclusion, and request_id
idempotency. Registration is atomic in a single repository-owned transaction.
"""
from datetime import date
from decimal import Decimal
import json
from typing import Callable
from psycopg.rows import dict_row

from greenhouse.contracts import (
    CostScenario, EnergyComponent, PublicationRef, ScenarioChange, ScenarioCommand,
    TariffProfile, TariffRate,
)
from greenhouse.db.migrate import sql
from greenhouse.db.query_adapter import PostgresCoreQueryAdapter

HISTORICAL_TARIFF_ID = 'AGC_2020_REFERENCE_V1'
HISTORICAL_WINDOW_START = date(2019, 12, 16)
HISTORICAL_WINDOW_END = date(2020, 5, 30)


class PostgresCostAdapter(PostgresCoreQueryAdapter):
    def _tariff_from_rows(self, rows):
        if not rows:
            return None
        first = rows[0]
        selected_version = first['tariff_version']
        if any(r['tariff_version'] != selected_version for r in rows):
            raise ValueError(f'Mixed tariff versions in query rows: expected {selected_version}')

        rates = []
        for r in rows:
            if r['rate_id'] is not None:
                rate_ver = r.get('rate_tariff_version') or r['tariff_version']
                if rate_ver != selected_version:
                    raise ValueError(f'Tariff rate parent mismatch: {rate_ver} != {selected_version}')
                rate_currency = r.get('rate_currency') or r['currency']
                if rate_currency != first['currency']:
                    raise ValueError(f'Tariff rate currency mismatch: {rate_currency} != {first["currency"]}')
                rates.append(TariffRate(
                    rate_id=r['rate_id'],
                    tariff_version=selected_version,
                    component=EnergyComponent(r['component']),
                    currency=rate_currency,
                    price_unit=r['price_unit'],
                    rate=Decimal(str(r['rate'])),
                    valid_from=r['valid_from'],
                    valid_to_exclusive=r['valid_to_exclusive'],
                    source_reference=r['rate_source_reference'],
                ))
        sorted_rates = tuple(sorted(rates, key=lambda rate: (rate.component.value, rate.valid_from)))
        return TariffProfile(
            tariff_version=first['tariff_version'],
            tariff_id=first['tariff_id'],
            version=first['version'],
            data_scope_id=first['data_scope_id'],
            currency=first['currency'],
            source_reference=first['source_reference'],
            label=first['label'],
            rates=sorted_rates,
            is_baseline=first['is_baseline'],
            compartment_id=first['compartment_id'],
        )

    def validate_cycle(self, scope_id: str, snapshot_id: str, cycle_id: str, compartment_id: str) -> bool:
        rows = self._rows('query_cycle', (scope_id, snapshot_id, cycle_id, compartment_id))
        return len(rows) == 1

    def get_tariff(self, tariff_version: str) -> TariffProfile | None:
        with self.connection.cursor(row_factory=dict_row) as cursor:
            rows = cursor.execute(sql('read_tariff_profile'), (tariff_version,)).fetchall()
        return self._tariff_from_rows(rows)

    def get_baseline_tariff(self, data_scope_id: str, compartment_id: str | None = None) -> TariffProfile | None:
        with self.connection.cursor(row_factory=dict_row) as cursor:
            rows = cursor.execute(sql('read_baseline_tariff'), (data_scope_id, compartment_id)).fetchall()
        return self._tariff_from_rows(rows)

    def register_tariff(self, profile: TariffProfile) -> TariffProfile:
        if not isinstance(profile, TariffProfile):
            raise ValueError('Expected a validated TariffProfile')
        stored = self.get_tariff(profile.tariff_version)
        if stored is not None:
            if stored != profile:
                raise ValueError(
                    f'Tariff drift for {profile.tariff_version}: stored profile differs from candidate'
                )
            return stored
        with self.connection.transaction():
            self.connection.execute(sql('insert_tariff_profile'), (
                profile.tariff_version, profile.tariff_id, profile.version,
                profile.data_scope_id, profile.compartment_id, profile.currency,
                profile.source_reference, profile.label, profile.is_baseline
            ))
            for rate in profile.rates:
                self.connection.execute(sql('insert_tariff_rate'), (
                    rate.rate_id, profile.tariff_version, rate.component.value,
                    rate.currency, rate.price_unit, rate.rate,
                    rate.valid_from, rate.valid_to_exclusive, rate.source_reference
                ))
        return profile

    def _is_canonical_historical(self, profile: TariffProfile) -> bool:
        if profile.tariff_id != HISTORICAL_TARIFF_ID:
            return False
        rates_by_comp = {r.component: r for r in profile.rates}
        if set(rates_by_comp.keys()) != {EnergyComponent.HEAT, EnergyComponent.ELEC_HIGH, EnergyComponent.ELEC_LOW}:
            return False
        heat = rates_by_comp[EnergyComponent.HEAT]
        elec_h = rates_by_comp[EnergyComponent.ELEC_HIGH]
        elec_l = rates_by_comp[EnergyComponent.ELEC_LOW]
        return (
            heat.rate == Decimal('0.0083') and heat.price_unit == 'EUR/MJ' and
            heat.valid_from == HISTORICAL_WINDOW_START and heat.valid_to_exclusive == HISTORICAL_WINDOW_END and
            elec_h.rate == Decimal('0.08') and elec_h.price_unit == 'EUR/kWh' and
            elec_h.valid_from == HISTORICAL_WINDOW_START and elec_h.valid_to_exclusive == HISTORICAL_WINDOW_END and
            elec_l.rate == Decimal('0.04') and elec_l.price_unit == 'EUR/kWh' and
            elec_l.valid_from == HISTORICAL_WINDOW_START and elec_l.valid_to_exclusive == HISTORICAL_WINDOW_END
        )

    def register_historical_tariff(self, data_scope_id: str, compartment_id: str | None = None) -> TariffProfile:
        """Explicit idempotent registration of AGC_2020_REFERENCE_V1 for a verified scope."""
        # 1. If an existing baseline in this exact scope AND compartment is already canonical historical, return it.
        existing_baseline = self.get_baseline_tariff(data_scope_id, compartment_id)
        if existing_baseline is not None and self._is_canonical_historical(existing_baseline):
            if existing_baseline.compartment_id == compartment_id:
                return existing_baseline

        # 2. Check if legacy un-scoped version matches this scope and compartment
        legacy_version = f'tariff:{HISTORICAL_TARIFF_ID}:v1'
        legacy_stored = self.get_tariff(legacy_version)
        if (legacy_stored is not None
                and legacy_stored.data_scope_id == data_scope_id
                and legacy_stored.compartment_id == compartment_id
                and self._is_canonical_historical(legacy_stored)):
            return legacy_stored

        # 3. Use scoped tariff_version for multi-scope and compartment isolation
        suffix = f':{compartment_id}' if compartment_id else ''
        tariff_version = f'tariff:{HISTORICAL_TARIFF_ID}:{data_scope_id}{suffix}:v1'
        scoped_stored = self.get_tariff(tariff_version)
        if scoped_stored is not None and self._is_canonical_historical(scoped_stored):
            return scoped_stored

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
        return self.register_tariff(profile)

    def _scenario_from_rows(self, rows):
        if not rows:
            return None
        first = rows[0]
        selected_version = first['scenario_version']
        if any(r['scenario_version'] != selected_version for r in rows):
            raise ValueError(f'Mixed scenario versions in query rows: expected {selected_version}')

        publication = self.publication(first['publication_id'])
        rates = []
        for r in rows:
            if r['scenario_rate_id'] is not None:
                rate_ver = r.get('rate_scenario_version') or r['scenario_version']
                if rate_ver != selected_version:
                    raise ValueError(f'Scenario rate parent mismatch: {rate_ver} != {selected_version}')
                rate_currency = r.get('rate_currency') or r['currency']
                rates.append(TariffRate(
                    rate_id=r['scenario_rate_id'],
                    tariff_version=selected_version,
                    component=EnergyComponent(r['component']),
                    currency=rate_currency,
                    price_unit=r['price_unit'],
                    rate=Decimal(str(r['rate'])),
                    valid_from=r['valid_from'],
                    valid_to_exclusive=r['valid_to_exclusive'],
                    source_reference=r['rate_source_reference'],
                ))
        sorted_rates = tuple(sorted(rates, key=lambda rate: (rate.component.value, rate.valid_from)))

        raw_changes = first.get('changes') or []
        if isinstance(raw_changes, str):
            raw_changes = json.loads(raw_changes)
        parsed_changes = tuple(
            ScenarioChange(
                component=EnergyComponent(c['component']),
                price_unit=c['price_unit'],
                price=Decimal(str(c['price'])) if c.get('price') is not None else None,
                change_percent=Decimal(str(c['change_percent'])) if c.get('change_percent') is not None else None,
            )
            for c in raw_changes
        )

        analysis_context = None
        raw_payload = first.get('command_payload')
        if isinstance(raw_payload, str):
            try:
                raw_payload = json.loads(raw_payload)
            except Exception as e:
                raise ValueError(f'Malformed command_payload for scenario {first["scenario_version"]}') from e
        if isinstance(raw_payload, dict) and 'analysis_context' in raw_payload:
            ctx_dict = raw_payload['analysis_context']
            if ctx_dict is not None:
                from greenhouse.contracts import from_dict, QueryContext
                try:
                    analysis_context = from_dict(QueryContext, ctx_dict)
                except Exception as e:
                    raise ValueError(f'Corrupted analysis_context in stored scenario {first["scenario_version"]}') from e

        return CostScenario(
            scenario_version=first['scenario_version'],
            scenario_id=first['scenario_id'],
            version=first['version'],
            request_id=first['request_id'],
            baseline_tariff_version=first['baseline_tariff_version'],
            publication=publication,
            name=first['name'],
            rates=sorted_rates,
            changes=parsed_changes,
            author=first['author'],
            created_at=first.get('created_at'),
            analysis_context=analysis_context,
        )

    def list_scenarios(self, data_scope_id: str | None = None) -> tuple[CostScenario, ...]:
        query = "SELECT DISTINCT scenario_version FROM config.cost_scenario ORDER BY scenario_version"
        with self.connection.cursor() as cur:
            rows = cur.execute(query).fetchall()
        scenarios = []
        for (ver,) in rows:
            scen = self.get_scenario(ver)
            if scen is not None:
                if data_scope_id is None or scen.publication.scope_id == data_scope_id:
                    scenarios.append(scen)
        scenarios.sort(key=lambda s: (s.scenario_id, s.version))
        return tuple(scenarios)

    def get_scenario(self, scenario_version: str) -> CostScenario | None:
        with self.connection.cursor(row_factory=dict_row) as cursor:
            rows = cursor.execute(sql('read_cost_scenario'), (scenario_version,)).fetchall()
        return self._scenario_from_rows(rows)

    def get_scenario_by_request_id(self, request_id: str) -> CostScenario | None:
        with self.connection.cursor(row_factory=dict_row) as cursor:
            rows = cursor.execute(sql('read_cost_scenario_by_request_id'), (request_id,)).fetchall()
        return self._scenario_from_rows(rows)

    def get_latest_scenario_version(self, scenario_id: str) -> CostScenario | None:
        with self.connection.cursor(row_factory=dict_row) as cursor:
            row = cursor.execute(sql('read_latest_scenario_version'), (scenario_id,)).fetchone()
        if row is None:
            return None
        return self.get_scenario(row['scenario_version'])

    def execute_scenario_transaction(
        self, command: ScenarioCommand, rate_factory: Callable[[str], tuple[TariffRate, ...]],
    ) -> CostScenario:
        with self.connection.transaction():
            # 1. Advisory transaction lock on request_id to serialize identical retries
            with self.connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f'req:{command.request_id}',))

            target_scen_id = command.scenario_id or f'scenario:{command.request_id}'

            # 2. Check if request_id already exists
            existing_req = self.get_scenario_by_request_id(command.request_id)
            if existing_req is not None:
                if (existing_req.scenario_id == target_scen_id and
                    existing_req.baseline_tariff_version == command.baseline_version_id and
                    existing_req.name == command.name and
                    existing_req.publication.publication_id == command.publication.publication_id and
                    existing_req.publication.snapshot_id == command.publication.snapshot_id and
                    existing_req.publication.scope_id == command.publication.scope_id and
                    existing_req.changes == command.changes and
                    existing_req.analysis_context == command.analysis_context):
                    return existing_req
                raise ValueError(f'Conflicting scenario command for existing request_id: {command.request_id}')

            # 3. Advisory transaction lock on logical scenario_id to serialize version allocation
            with self.connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f'scen:{target_scen_id}',))

            # 4. Under scenario_id lock, read latest existing version
            latest = self.get_latest_scenario_version(target_scen_id)

            if latest is not None:
                # Validate R3: append must preserve frozen analysis identity!
                if (latest.publication.publication_id != command.publication.publication_id or
                    latest.publication.snapshot_id != command.publication.snapshot_id or
                    latest.publication.scope_id != command.publication.scope_id or
                    latest.baseline_tariff_version != command.baseline_version_id):
                    raise ValueError(
                        f'Cannot change publication, snapshot, scope, or baseline of existing logical scenario {target_scen_id}'
                    )
                if latest.analysis_context is not None:
                    if command.analysis_context is None:
                        command = replace(command, analysis_context=latest.analysis_context)
                    elif command.analysis_context != latest.analysis_context:
                        raise ValueError(
                            f'Cannot change analysis context of existing logical scenario {target_scen_id}'
                        )
                elif command.analysis_context is not None:
                    raise ValueError(
                        f'Cannot add analysis context to legacy scenario {target_scen_id} that has no context'
                    )
                version = latest.version + 1
            else:
                version = 1

            scenario_version = f'{target_scen_id}:v{version}'

            # 5. Build scenario rates using the passed factory
            scenario_rates = rate_factory(scenario_version)

            # 6. Construct CostScenario
            scenario = CostScenario(
                scenario_version=scenario_version,
                scenario_id=target_scen_id,
                version=version,
                request_id=command.request_id,
                baseline_tariff_version=command.baseline_version_id,
                publication=command.publication,
                name=command.name,
                rates=scenario_rates,
                changes=command.changes,
                analysis_context=command.analysis_context,
            )

            # 7. Insert scenario and rates
            changes_json = json.dumps([
                {
                    'component': c.component.value,
                    'price_unit': c.price_unit,
                    'price': str(c.price) if c.price is not None else None,
                    'change_percent': str(c.change_percent) if c.change_percent is not None else None,
                }
                for c in scenario.changes
            ])
            payload_dict = {
                'request_id': command.request_id,
                'scenario_id': target_scen_id,
                'version': version,
                'name': command.name,
                'baseline_version_id': command.baseline_version_id,
                'publication_id': command.publication.publication_id,
                'changes': [
                    {
                        'component': c.component.value,
                        'price_unit': c.price_unit,
                        'price': str(c.price) if c.price is not None else None,
                        'change_percent': str(c.change_percent) if c.change_percent is not None else None,
                    }
                    for c in command.changes
                ],
            }
            if command.analysis_context is not None:
                from greenhouse.contracts import to_dict
                payload_dict['analysis_context'] = to_dict(command.analysis_context)
            payload_json = json.dumps(payload_dict)

            self.connection.execute(sql('insert_cost_scenario'), (
                scenario.scenario_version, scenario.scenario_id, scenario.version,
                scenario.request_id, scenario.baseline_tariff_version,
                scenario.publication.publication_id, scenario.name, scenario.author,
                changes_json, payload_json,
            ))
            for rate in scenario.rates:
                self.connection.execute(sql('insert_cost_scenario_rate'), (
                    rate.rate_id, scenario.scenario_version, rate.component.value,
                    rate.currency, rate.price_unit, rate.rate,
                    rate.valid_from, rate.valid_to_exclusive, rate.source_reference,
                ))

        stored = self.get_scenario(scenario.scenario_version)
        if stored is None:
            raise RuntimeError(f'Failed to reload registered scenario {scenario.scenario_version}')
        return stored

    def register_scenario(self, scenario: CostScenario, command_payload: dict | None = None) -> CostScenario:
        if not isinstance(scenario, CostScenario):
            raise ValueError('Expected a validated CostScenario')

        with self.connection.transaction():
            # Advisory transaction locks
            with self.connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f'req:{scenario.request_id}',))
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f'scen:{scenario.scenario_id}',))

            existing_req = self.get_scenario_by_request_id(scenario.request_id)
            if existing_req is not None:
                if existing_req.author != scenario.author or (scenario.created_at is not None and existing_req.created_at != scenario.created_at):
                    raise ValueError('Imported scenario audit identity differs from stored version')
                if (existing_req.scenario_version == scenario.scenario_version and
                    existing_req.scenario_id == scenario.scenario_id and
                    existing_req.baseline_tariff_version == scenario.baseline_tariff_version and
                    existing_req.name == scenario.name and
                    existing_req.publication.publication_id == scenario.publication.publication_id and
                    existing_req.publication.snapshot_id == scenario.publication.snapshot_id and
                    existing_req.publication.scope_id == scenario.publication.scope_id and
                    existing_req.rates == scenario.rates and
                    existing_req.changes == scenario.changes and
                    existing_req.analysis_context == scenario.analysis_context):
                    return existing_req
                raise ValueError(f'Conflicting scenario command for existing request_id: {scenario.request_id}')

            existing_ver = self.get_scenario(scenario.scenario_version)
            if existing_ver is not None:
                if (existing_ver.request_id == scenario.request_id and
                    existing_ver.scenario_id == scenario.scenario_id and
                    existing_ver.baseline_tariff_version == scenario.baseline_tariff_version and
                    existing_ver.name == scenario.name and
                    existing_ver.publication.publication_id == scenario.publication.publication_id and
                    existing_ver.publication.snapshot_id == scenario.publication.snapshot_id and
                    existing_ver.publication.scope_id == scenario.publication.scope_id and
                    existing_ver.rates == scenario.rates and
                    existing_ver.changes == scenario.changes and
                    existing_ver.analysis_context == scenario.analysis_context):
                    return existing_ver
                raise ValueError(f'Scenario drift for {scenario.scenario_version}: stored scenario differs from candidate')

            # Validate R3: append cannot change publication, baseline, or erase analysis context of existing logical scenario
            latest = self.get_latest_scenario_version(scenario.scenario_id)
            if latest is not None:
                if (latest.publication.publication_id != scenario.publication.publication_id or
                    latest.publication.snapshot_id != scenario.publication.snapshot_id or
                    latest.publication.scope_id != scenario.publication.scope_id or
                    latest.baseline_tariff_version != scenario.baseline_tariff_version):
                    raise ValueError(
                        f'Cannot change publication, snapshot, scope, or baseline of existing logical scenario {scenario.scenario_id}'
                    )
                if latest.analysis_context is not None:
                    if scenario.analysis_context is None:
                        raise ValueError(
                            f'Cannot omit or erase analysis context of existing logical scenario {scenario.scenario_id}'
                        )
                    if scenario.analysis_context != latest.analysis_context:
                        raise ValueError(
                            f'Cannot change analysis context of existing logical scenario {scenario.scenario_id}'
                        )
                elif scenario.analysis_context is not None:
                    raise ValueError(
                        f'Cannot add analysis context to legacy scenario {scenario.scenario_id} that has no context'
                    )

            # Validate baseline exists and scope matches
            baseline = self.get_tariff(scenario.baseline_tariff_version)
            if baseline is None:
                raise ValueError(f'Baseline tariff version not found: {scenario.baseline_tariff_version}')
            if baseline.data_scope_id != scenario.publication.scope_id:
                raise ValueError(
                    f'Baseline scope {baseline.data_scope_id} does not match scenario publication scope {scenario.publication.scope_id}'
                )

            if scenario.analysis_context is not None:
                ac = scenario.analysis_context
                if ac.rule_version is not None:
                    raise ValueError('Scenario analysis context cannot have a threshold rule')
                if (ac.publication_id != scenario.publication.publication_id or
                    ac.scope_id != scenario.publication.scope_id or
                    ac.snapshot_id != scenario.publication.snapshot_id):
                    raise ValueError(
                        f'Analysis context publication {ac.publication_id} does not match scenario publication {scenario.publication.publication_id}'
                    )
                if ac.start >= ac.end:
                    raise ValueError('Invalid analysis context calendar window')
                if not ac.compartment_ids:
                    raise ValueError('Analysis context must specify at least one compartment')
                for comp_id in ac.compartment_ids:
                    if not self.validate_cycle(ac.scope_id, ac.snapshot_id, ac.cycle_id, comp_id):
                        raise ValueError(
                            f'Cycle {ac.cycle_id} and compartment {comp_id} are not mapped to scope {ac.scope_id} and snapshot {ac.snapshot_id}'
                        )
                if baseline.compartment_id is not None and baseline.compartment_id not in ac.compartment_ids:
                    raise ValueError(
                        f'Baseline tariff compartment {baseline.compartment_id} does not match analysis context compartments {ac.compartment_ids}'
                    )

            # Guard caller-supplied command_payload consistency
            if command_payload is not None and 'analysis_context' in command_payload:
                from greenhouse.contracts import from_dict, to_dict, QueryContext
                ctx_dict = command_payload['analysis_context']
                if ctx_dict is not None:
                    try:
                        parsed_ctx = from_dict(QueryContext, ctx_dict)
                    except Exception as e:
                        raise ValueError('Malformed analysis_context in command_payload') from e
                    if scenario.analysis_context is not None and parsed_ctx != scenario.analysis_context:
                        raise ValueError('command_payload analysis_context does not match scenario.analysis_context')
                    if scenario.analysis_context is None:
                        raise ValueError('command_payload has analysis_context but scenario.analysis_context is None')
                elif scenario.analysis_context is not None:
                    raise ValueError('command_payload analysis_context is None but scenario.analysis_context is set')
            elif command_payload is None and scenario.analysis_context is not None:
                from greenhouse.contracts import to_dict
                command_payload = {'analysis_context': to_dict(scenario.analysis_context)}
            elif command_payload is not None and scenario.analysis_context is not None and 'analysis_context' not in command_payload:
                from greenhouse.contracts import to_dict
                command_payload = dict(command_payload)
                command_payload['analysis_context'] = to_dict(scenario.analysis_context)

            changes_json = json.dumps([
                {
                    'component': c.component.value,
                    'price_unit': c.price_unit,
                    'price': str(c.price) if c.price is not None else None,
                    'change_percent': str(c.change_percent) if c.change_percent is not None else None,
                }
                for c in scenario.changes
            ])
            payload_json = json.dumps(command_payload) if command_payload is not None else None

            self.connection.execute(sql('insert_cost_scenario_import'), (
                scenario.scenario_version, scenario.scenario_id, scenario.version,
                scenario.request_id, scenario.baseline_tariff_version,
                scenario.publication.publication_id, scenario.name, scenario.author,
                changes_json, payload_json, scenario.created_at,
            ))
            for rate in scenario.rates:
                self.connection.execute(sql('insert_cost_scenario_rate'), (
                    rate.rate_id, scenario.scenario_version, rate.component.value,
                    rate.currency, rate.price_unit, rate.rate,
                    rate.valid_from, rate.valid_to_exclusive, rate.source_reference
                ))

        stored = self.get_scenario(scenario.scenario_version)
        if stored is None:
            raise RuntimeError(f'Failed to reload registered scenario {scenario.scenario_version}')
        return stored
