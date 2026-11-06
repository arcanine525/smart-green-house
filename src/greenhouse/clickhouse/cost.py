"""BR06 commands store each complete scenario+rates in one journaled envelope."""
from dataclasses import replace
import json

from greenhouse.clickhouse.coordinator import writer_lock
from greenhouse.clickhouse.energy import EnergyAdapter
from greenhouse.clickhouse.repository import Repository
from greenhouse.contracts import CostScenario,ScenarioCommand,TariffProfile,from_dict,to_dict


def validate_rates(version,currency,rates):
    if len({r.rate_id for r in rates})!=len(rates) or any(r.tariff_version!=version or r.currency!=currency for r in rates):
        raise ValueError('Rate parent, currency or identity mismatch')
    # Contract additionally rejects interval overlap at each component grain.
    TariffProfile(version,'validation',1,'validation',currency,'validation','validation',rates)


class CostAdapter(EnergyAdapter):
    def __init__(self,client,coordinator=None,*,raw_snapshots=None):
        super().__init__(client,raw_snapshots=raw_snapshots);self.coordinator=coordinator
        self.repo=Repository(client,coordinator)

    def _envelopes(self,kind):
        return [r for r in self.repo.configurations()['rows'] if r['kind']==kind]

    def validate_cycle(self,scope_id,snapshot_id,cycle_id,compartment_id):
        rows=self.client.query('''SELECT DISTINCT publication_id FROM energy_cycle WHERE scope_id={scope:String}
            AND cycle_id={cycle:String} AND compartment_id={comp:String}''',{'scope':scope_id,'cycle':cycle_id,'comp':compartment_id})
        for row in rows:
            try:
                pub=self.publication(row['publication_id'])
            except ValueError: continue  # An unfinished candidate does not authorize a context.
            if pub.snapshot_id==snapshot_id: return True
        return False

    def get_tariff(self,tariff_version):
        rows=[r for r in self._envelopes('tariff') if r['version_id']==tariff_version]
        if len(rows)>1: raise ValueError('Ambiguous tariff identity')
        if not rows:return None
        profile=from_dict(TariffProfile,json.loads(rows[0]['payload']))
        if profile.tariff_version!=rows[0]['version_id'] or profile.data_scope_id!=rows[0]['scope_id']:
            raise ValueError('Tariff envelope identity mismatch')
        validate_rates(profile.tariff_version,profile.currency,profile.rates)
        return profile

    def register_tariff(self,profile):
        if not isinstance(profile,TariffProfile):raise ValueError('Expected a validated TariffProfile')
        validate_rates(profile.tariff_version,profile.currency,profile.rates)
        self.repo.register_configuration('tariff',profile.data_scope_id,profile.tariff_version,'tariff:'+profile.tariff_version,to_dict(profile))
        return self.get_tariff(profile.tariff_version)

    def get_baseline_tariff(self,data_scope_id,compartment_id=None):
        profiles=[self.get_tariff(r['version_id']) for r in self._envelopes('tariff') if r['scope_id']==data_scope_id]
        profiles=[p for p in profiles if p.is_baseline and p.compartment_id in (None,compartment_id)]
        return max(profiles,key=lambda p:(p.compartment_id is not None,p.version,p.tariff_id,p.tariff_version)) if profiles else None

    def register_historical_tariff(self,data_scope_id,compartment_id=None):
        from greenhouse.queries.reference_tariff import historical_profile
        return self.register_tariff(historical_profile(data_scope_id,compartment_id))

    def get_scenario(self,scenario_version):
        rows=[r for r in self._envelopes('cost_scenario') if r['version_id']==scenario_version]
        if len(rows)>1: raise ValueError('Ambiguous scenario version')
        if not rows:return None
        value=json.loads(rows[0]['payload']);scenario=from_dict(CostScenario,value['scenario'])
        if scenario.scenario_version!=rows[0]['version_id'] or scenario.publication.scope_id!=rows[0]['scope_id']:
            raise ValueError('Scenario envelope identity mismatch')
        if self.publication(scenario.publication.publication_id)!=scenario.publication:
            raise ValueError('Scenario frozen publication metadata differs')
        validate_rates(scenario.scenario_version,'EUR',scenario.rates)
        baseline=self.get_tariff(scenario.baseline_tariff_version)
        if baseline is None:raise ValueError('Scenario baseline missing')
        from greenhouse.queries.cost import derive_scenario_rates
        expected=derive_scenario_rates(baseline,scenario.scenario_version,scenario.name,scenario.changes)
        if scenario.rates!=expected:raise ValueError('Scenario has missing or changed derived rates')
        command=from_dict(ScenarioCommand,value['command'])
        expected_command=ScenarioCommand(scenario.request_id,scenario.baseline_tariff_version,scenario.name,
            scenario.publication,scenario.changes,scenario.scenario_id,scenario.analysis_context)
        if command!=expected_command or rows[0]['request_id']!='scenario-command:'+command.request_id:
            raise ValueError('Scenario command/content mismatch')
        return scenario

    def get_scenario_by_request_id(self,request_id):
        rows=[r for r in self._envelopes('cost_scenario') if r['request_id']=='scenario-command:'+request_id]
        if len(rows)>1:raise ValueError('Conflicting scenario request')
        return self.get_scenario(rows[0]['version_id']) if rows else None

    def list_scenarios(self,data_scope_id=None):
        rows=[r for r in self._envelopes('cost_scenario') if data_scope_id is None or r['scope_id']==data_scope_id]
        scenarios=[self.get_scenario(r['version_id']) for r in rows]
        return tuple(sorted(scenarios,key=lambda s:(s.scenario_id,s.version)))

    def get_latest_scenario_version(self,scenario_id):
        values=[s for s in self.list_scenarios() if s.scenario_id==scenario_id]
        return max(values,key=lambda s:s.version) if values else None

    def execute_scenario_transaction(self,command,rate_factory,*,_imported_scenario=None):
        if not isinstance(command,ScenarioCommand):raise ValueError('Expected a validated ScenarioCommand')
        target=command.scenario_id or 'scenario:'+command.request_id
        # One database-wide shared lock covers retry, version allocation and append.
        with writer_lock(self.coordinator,self.client):
            latest=self.get_latest_scenario_version(target)
            if latest is not None:
                if (latest.publication,latest.baseline_tariff_version)!=(command.publication,command.baseline_version_id):
                    raise ValueError('Cannot change scenario publication or baseline')
                if latest.analysis_context is not None and command.analysis_context is None:
                    command=replace(command,analysis_context=latest.analysis_context)
                if latest.analysis_context!=command.analysis_context:
                    raise ValueError('Cannot change frozen scenario analysis context')
            command=replace(command,scenario_id=target)
            existing=self.get_scenario_by_request_id(command.request_id)
            if existing is not None:
                expected=ScenarioCommand(existing.request_id,existing.baseline_tariff_version,existing.name,
                    existing.publication,existing.changes,existing.scenario_id,existing.analysis_context)
                if command!=expected:raise ValueError('Conflicting content for scenario request ID')
                return existing
            if self.publication(command.publication.publication_id)!=command.publication:
                raise ValueError('Scenario publication mismatch')
            baseline=self.get_tariff(command.baseline_version_id)
            if baseline is None or baseline.data_scope_id!=command.publication.scope_id:
                raise ValueError('Scenario baseline scope mismatch')
            version=latest.version+1 if latest else 1
            scenario_version=target+':v'+str(version)
            rates=rate_factory(scenario_version)
            validate_rates(scenario_version,'EUR',rates)
            from greenhouse.queries.cost import derive_scenario_rates
            if rates!=derive_scenario_rates(baseline,scenario_version,command.name,command.changes):
                raise ValueError('Scenario factory did not produce complete derived rates')
            scenario=CostScenario(scenario_version,target,version,command.request_id,command.baseline_version_id,
                command.publication,command.name,rates,command.changes,analysis_context=command.analysis_context)
            if _imported_scenario is not None:
                scenario=replace(scenario,author=_imported_scenario.author,created_at=_imported_scenario.created_at)
                if scenario!=_imported_scenario:
                    raise ValueError('Imported scenario differs from next immutable version/content')
            self.repo._register_configuration_locked('cost_scenario',command.publication.scope_id,scenario_version,
                'scenario-command:'+command.request_id,{'scenario':to_dict(scenario),'command':to_dict(command)})
            return scenario

    def register_scenario(self,scenario,command_payload=None):
        if not isinstance(scenario,CostScenario):raise ValueError('Expected a validated CostScenario')
        command=ScenarioCommand(scenario.request_id,scenario.baseline_tariff_version,scenario.name,scenario.publication,
            scenario.changes,scenario.scenario_id,scenario.analysis_context)
        result=self.execute_scenario_transaction(command,lambda version:scenario.rates,_imported_scenario=scenario)
        if result!=scenario:raise ValueError('Imported scenario identity/version differs from next immutable version')
        return result
