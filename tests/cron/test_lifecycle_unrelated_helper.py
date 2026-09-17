"""Inert guard checks: no command text in this module is executed."""
import json

from cron.lifecycle_guard import (
    _direct_lifecycle_scan,
    _iter_referenced_shell_scripts,
    _mask_python_data_read_paths,
    contains_gateway_lifecycle_command_or_referenced_script,
)


def test_unrelated_config_walker_does_not_turn_json_into_code(tmp_path):
    data = tmp_path / 'clarifications.json'
    data.write_text(json.dumps({'padding': 'x' * (1024 * 1024)}))
    body = f'''import json
from pathlib import Path
def walk(v, p=''):
    if isinstance(v, dict):
        for k, x in v.items():
            if k == 'enable_auto_consolidation':
                print(p+k, x)
            elif isinstance(x, dict):
                walk(x, p+k+'.')
walk({{}})
a=json.loads(Path({str(data)!r}).read_text())
'''
    command = "python3 -B - <<'PY'\n" + body + 'PY\n'
    print('direct_scan', _direct_lifecycle_scan(command))
    print('data_masked', _mask_python_data_read_paths(body) != body)
    print('references', list(map(str, _iter_referenced_shell_scripts(command))))
    assert not contains_gateway_lifecycle_command_or_referenced_script(command, cwd=str(tmp_path))


SCOPE_BODY = "import json,urllib.request,collections,os\nfrom pathlib import Path\nos.umask(0o077)\nr=Path('/synthetic/state')\nd=json.load(urllib.request.urlopen('http://127.0.0.1:9177/v1/default/banks/hermes/config',timeout=20))\nprint('config response keys',sorted(d))\ndef walk(v,p=''):\n if isinstance(v,dict):\n  for k,x in v.items():\n   if k in ('enable_auto_consolidation','enable_observations','consolidation_reconcile_interval_seconds'):\n    print(p+k,x)\n   elif isinstance(x,dict):walk(x,p+k+'.')\nwalk(d)\na=json.loads(Path('/synthetic/clarifications.json').read_text())\nchanges=json.loads((r/'cleanup-steps-1-2/entity-changes.json').read_text())\nassigned={x['fact_id'] for x in a if x.get('clarified_profile') and x.get('basis') in ('review confirmed','session-owner default approved by Brian')}\ngeneric={x['fact_id'] for x in a if not x.get('clarified_profile')}\npins=json.loads((r/'historical-pilot-20260916/mapping-decisions.json').read_text())\ncandidate=json.loads((r/'historical-pilot-20260916/first-production-batch-candidate.json').read_text())\nselected=set(candidate['source_fact_ids']);facts=set(x['source_fact_id'] for x in pins)\nprotected_profile_changes=[x['fact_id'] for x in changes if x['fact_id'] in generic and any(n in ('referent:Ops profile','referent:Default profile','referent:Ang profile') for n in set(x['after_entities'])-set(x['before_entities']))]\nreport={'assignment_records':len(assigned),'generic_records':len(generic),'unique_classification_ids':len(set(x['fact_id'] for x in a)),'initial_entity_change_proposals':len(changes),'generic_profile_reassignments_in_proposal':protected_profile_changes,'pilot_mapped_fact_count':len(facts),'first_batch_fact_count':len(selected),'outside_first_batch_pilot_fact_count':len(facts-selected),'full_historical_scope_frozen':False,'duplicate_deletion_ids_approved_in_this_inventory':[],'automatic_consolidation_config':{k:v for k,v in d.get('config',d).items() if k in ('enable_auto_consolidation','enable_observations','consolidation_reconcile_interval_seconds')}}\n(r/'cleanup-steps-1-2/scope-validation.json').write_text(json.dumps(report,indent=2)+'\\n')\nprint(json.dumps(report,indent=2))\n"

def test_full_scope_check_is_data_not_executable(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    data = tmp_path / "clarifications.json"
    data.write_text(json.dumps({"padding": "x" * (1024 * 1024)}))
    inert_body = SCOPE_BODY.replace("/synthetic/state", str(state)).replace("/synthetic/clarifications.json", str(data))
    command = "python3 -B - <<'PY'\n" + inert_body + "PY\n"
    print("direct_scan", _direct_lifecycle_scan(command))
    print("data_masked", _mask_python_data_read_paths(inert_body) != inert_body)
    print("references", list(map(str, _iter_referenced_shell_scripts(command))))
    assert not contains_gateway_lifecycle_command_or_referenced_script(command, cwd=str(tmp_path))
