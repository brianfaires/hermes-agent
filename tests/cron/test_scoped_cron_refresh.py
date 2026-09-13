"""Exercise both real run_job reload sites with private profile state."""
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from agent import secret_scope as ss
from cron import scheduler
from hermes_cli import env_loader
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


@pytest.mark.parametrize('no_agent', [True, False])
def test_run_job_refresh_is_private_and_refreshes_only_its_external_sources(tmp_path, monkeypatch, no_agent):
    # Import once before threads, as the gateway does at startup. Never run an agent.
    import run_agent
    from agent.secret_sources import registry
    monkeypatch.setattr(run_agent, 'AIAgent', lambda **kw: pytest.fail('agent construction forbidden'))
    monkeypatch.setattr(ss, '_MULTIPLEX_ACTIVE', True)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-process-key')
    env_loader.reset_secret_source_cache()
    homes = [tmp_path / n for n in ('alpha', 'beta')]
    for h in homes:
        h.mkdir(); (h / '.env').write_text('OPENAI_API_KEY=fake-old\n')
    monkeypatch.setattr(env_loader, '_load_secrets_config', lambda h: {'fixture': {}})
    pulls = []
    def apply(cfg, h, *, environ):
        pulls.append(h)
        environ['EXTERNAL_KEY'] = f'fake-external-{h.name}'
        return SimpleNamespace(sources=['fixture'], provenance={'EXTERNAL_KEY': SimpleNamespace(source='fixture')})
    monkeypatch.setattr(registry, 'apply_all', apply)
    for h in homes:
        env_loader.hydrate_profile_secret_sources(h)
    barrier = Barrier(2); observed = {}
    before = dict(os.environ)
    class AtBoundary(BaseException):
        pass
    def capture(*args, **kwargs):
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
        barrier.wait(10)
        observed[home] = (ss.get_secret('OPENAI_API_KEY'), ss.get_secret('EXTERNAL_KEY'), dict(os.environ))
        if no_agent:
            return True, 'fixture output'
        raise AtBoundary()
    monkeypatch.setattr(scheduler, '_run_job_script_with_claim_heartbeat', capture)
    monkeypatch.setattr(scheduler, '_resolve_delivery_target', capture)
    monkeypatch.setattr(scheduler, '_teardown_cron_agent', lambda *a: None)
    def worker(home):
        prior = ss.current_secret_scope()
        ht = set_hermes_home_override(str(home))
        st = ss.set_secret_scope(ss.build_profile_secret_scope(home))
        try:
            (home / '.env').write_text(f'OPENAI_API_KEY=fake-{home.name}\n')
            original = (home / '.env').read_bytes()
            barrier.wait(10)
            job = {'id': home.name, 'prompt': 'fixture prompt', 'no_agent': no_agent, 'deliver': 'local'}
            if no_agent:
                job['script'] = 'fixture.py'
                assert scheduler.run_job(job)[0] is True
            else:
                with pytest.raises(AtBoundary):
                    scheduler.run_job(job)
            assert (home / '.env').read_bytes() == original
        finally:
            ss.reset_secret_scope(st)
            reset_hermes_home_override(ht)
        assert ss.current_secret_scope() is prior
    try:
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(worker, homes))
        for h in homes:
            assert observed[h] == (f'fake-{h.name}', f'fake-external-{h.name}', before)
        assert all(pulls.count(h) == 2 for h in homes)
        assert dict(os.environ) == before
    finally:
        env_loader.reset_secret_source_cache()


@pytest.mark.parametrize('script_ok', [True, False])
def test_run_one_job_keeps_scope_through_script_and_delivery_then_restores(tmp_path, monkeypatch, script_ok):
    from cron.jobs import create_job, use_cron_store, get_job
    from hermes_constants import get_hermes_home, get_hermes_home_override
    home = tmp_path / 'custom root' / 'profiles' / 'served'
    (home / 'scripts').mkdir(parents=True)
    (home / 'scripts' / 'fixture.py').write_text('print("fixture")\n')
    (home / '.env').write_text('OPENAI_API_KEY=fake-served\nOWNER_ONLY=fake-owner\n')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(home.parent.parent))
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-launch')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(ss, '_MULTIPLEX_ACTIVE', True)
    captured = []
    class Script:
        returncode = 0 if script_ok else 1
        def __init__(self, argv, **kw):
            captured.append(kw['env'])
            assert Path(argv[1]) == home / 'scripts' / 'fixture.py'
            assert kw['cwd'] == str(home / 'scripts')
            assert kw['env']['HERMES_HOME'] == str(home)
            assert 'OPENAI_API_KEY' not in kw['env']
            assert ss.get_secret('OWNER_ONLY') == 'fake-owner'
        def communicate(self, timeout):
            return ('fixture output', '') if script_ok else ('', 'fixture script failed')
    monkeypatch.setattr(scheduler.subprocess, 'Popen', Script)
    deliveries = []
    def deliver(job, content, **kw):
        deliveries.append(content)
        assert get_hermes_home() == home
        assert ss.get_secret('OPENAI_API_KEY') == 'fake-served'
        return None
    monkeypatch.setattr(scheduler, '_deliver_result', deliver)
    previous_home = get_hermes_home_override()
    previous_scope = ss.current_secret_scope()
    ht = set_hermes_home_override(str(home))
    try:
        with use_cron_store(home):
            job = create_job(prompt='', schedule='every 5m', script='fixture.py', no_agent=True, deliver='local')
            before = dict(os.environ)
            assert scheduler.run_one_job(job) is True
            assert len(captured) == 1 and len(deliveries) == 1
            assert get_job(job['id'])['last_status'] == ('ok' if script_ok else 'error')
            assert ss.current_secret_scope() is previous_scope
            assert dict(os.environ) == before
    finally:
        reset_hermes_home_override(ht)
    assert get_hermes_home_override() == previous_home


def test_cold_agent_prerun_hydrates_sources_before_child(tmp_path, monkeypatch):
    import run_agent
    from agent.secret_sources import registry
    from cron.jobs import create_job, use_cron_store
    home = tmp_path / 'served'
    (home / 'scripts').mkdir(parents=True)
    (home / 'scripts' / 'fixture.py').write_text('print("fixture")\n')
    (home / '.env').write_text('OPENAI_API_KEY=placeholder\n')
    (home / '.op.env').write_text('OP_SERVICE_ACCOUNT_TOKEN=fake-bootstrap\n')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(ss, '_MULTIPLEX_ACTIVE', True)
    monkeypatch.setattr(run_agent, 'AIAgent', lambda **kw: pytest.fail('wake gate must skip agent'))
    monkeypatch.setattr(env_loader, '_load_secrets_config', lambda h: {'fixture': {}})
    env_loader.reset_secret_source_cache()
    pulls = []
    def apply(cfg, h, *, environ):
        assert environ['OP_SERVICE_ACCOUNT_TOKEN'] == 'fake-bootstrap'
        pulls.append(h)
        environ['OPENAI_API_KEY'] = 'fake-external'
        return SimpleNamespace(sources=['fixture'], provenance={'OPENAI_API_KEY': SimpleNamespace(source='fixture')})
    monkeypatch.setattr(registry, 'apply_all', apply)
    observed = []
    class Script:
        returncode = 0
        def __init__(self, argv, **kw):
            observed.append(ss.get_secret('OPENAI_API_KEY'))
        def communicate(self, timeout):
            return '{"wakeAgent": false}', ''
    monkeypatch.setattr(scheduler.subprocess, 'Popen', Script)
    monkeypatch.setattr(scheduler, '_deliver_result', lambda *a, **kw: pytest.fail('silent gate must not deliver'))
    prior = ss.current_secret_scope()
    ht = set_hermes_home_override(str(home))
    before = dict(os.environ)
    try:
        with use_cron_store(home):
            job = create_job(prompt='fixture', schedule='every 5m', script='fixture.py', deliver='local')
            assert scheduler.run_one_job(job) is True
        assert observed == ['fake-external']
        assert pulls and all(h == home for h in pulls)
        assert ss.current_secret_scope() is prior
        assert dict(os.environ) == before
    finally:
        reset_hermes_home_override(ht)
        env_loader.reset_secret_source_cache()
