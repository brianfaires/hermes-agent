"""Profile refresh must remain private to the routed execution context."""
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from agent import secret_scope as ss
from hermes_cli import env_loader as loader
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(ss, '_MULTIPLEX_ACTIVE', True)
    loader.reset_secret_source_cache()
    yield
    loader.reset_secret_source_cache()


@contextmanager
def routed(home):
    ht = set_hermes_home_override(str(home))
    st = ss.set_secret_scope(ss.build_profile_secret_scope(home))
    try:
        yield
    finally:
        ss.reset_secret_scope(st)
        reset_hermes_home_override(ht)


def test_literal_rotation_refreshes_current_mapping_without_process_or_file_changes(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-launch-key')
    home = tmp_path / 'served'; home.mkdir()
    env = home / '.env'; env.write_text('OPENAI_API_KEY=fake-old\n')
    before = dict(os.environ)
    prior_scope = ss.current_secret_scope()
    with routed(home):
        env.write_bytes(b'\xef\xbb\xbfOPENAI_API_KEY="fake-new"\nOWNER_ONLY="quoted \\\"value\\\"" # comment\n')
        original = env.read_bytes()
        assert loader.load_hermes_dotenv(hermes_home=home) == [env]
        assert ss.get_secret('OPENAI_API_KEY') == 'fake-new'
        assert ss.get_secret('OWNER_ONLY') == 'quoted "value"'
        assert dict(os.environ) == before
        assert env.read_bytes() == original
    assert ss.current_secret_scope() is prior_scope


def test_implicit_home_uses_routed_profile(tmp_path, monkeypatch):
    launch = tmp_path / 'launch'; launch.mkdir()
    served = tmp_path / 'served'; served.mkdir()
    (launch / '.env').write_text('OWNER_ONLY=launch\n')
    (served / '.env').write_text('OWNER_ONLY=served\n')
    monkeypatch.setenv('HERMES_HOME', str(launch))
    before = dict(os.environ)
    with routed(served):
        loader.load_hermes_dotenv()
        assert ss.get_secret('OWNER_ONLY') == 'served'
        assert dict(os.environ) == before


def test_interleaved_profiles_keep_distinct_rotated_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-launch-key')
    homes = [tmp_path / n for n in ('alpha', 'beta')]
    for h in homes:
        h.mkdir(); (h / '.env').write_text('OPENAI_API_KEY=fake-old\n')
    before = dict(os.environ); barrier = Barrier(2)
    def worker(home):
        prior = ss.current_secret_scope()
        with routed(home):
            (home / '.env').write_text(f'OPENAI_API_KEY=fake-{home.name}\n')
            original = (home / '.env').read_bytes()
            barrier.wait(5)
            loader.load_hermes_dotenv(hermes_home=home)
            barrier.wait(5)
            result = (ss.get_secret('OPENAI_API_KEY'), dict(os.environ), (home / '.env').read_bytes())
            assert result == (f'fake-{home.name}', before, original)
        assert ss.current_secret_scope() is prior
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(worker, homes))


def test_external_sources_op_bootstrap_cache_and_literal_refresh(tmp_path, monkeypatch):
    from agent.secret_sources import registry
    home = tmp_path / 'served'; home.mkdir()
    (home / '.env').write_text('OPENAI_API_KEY=placeholder\nLITERAL=first\n')
    (home / '.op.env').write_text('OP_SERVICE_ACCOUNT_TOKEN=fake-bootstrap\n')
    monkeypatch.setattr(loader, '_load_secrets_config', lambda h: {'fixture': {}})
    calls = []
    def apply(cfg, h, *, environ):
        calls.append(h)
        assert environ['OP_SERVICE_ACCOUNT_TOKEN'] == 'fake-bootstrap'
        environ['OPENAI_API_KEY'] = 'fake-external'
        return SimpleNamespace(sources=['fixture'], provenance={'OPENAI_API_KEY': SimpleNamespace(source='fixture')})
    monkeypatch.setattr(registry, 'apply_all', apply)
    before = dict(os.environ)
    with routed(home):
        loader.load_hermes_dotenv(hermes_home=home)
        assert ss.get_secret('OPENAI_API_KEY') == 'fake-external'
        (home / '.env').write_text('OPENAI_API_KEY=placeholder\nLITERAL=second\n')
        loader.load_hermes_dotenv(hermes_home=home)
        assert ss.get_secret('LITERAL') == 'second'
        assert ss.get_secret('OPENAI_API_KEY') == 'fake-external'
    assert calls == [home]
    assert dict(os.environ) == before


def test_external_failure_preserves_literal_scope_and_process(tmp_path, monkeypatch):
    from agent.secret_sources import registry
    home = tmp_path / 'served'; home.mkdir()
    (home / '.env').write_text('OPENAI_API_KEY=fake-literal\n')
    monkeypatch.setattr(loader, '_load_secrets_config', lambda h: {'fixture': {}})
    def fail(*a, **kw):
        raise RuntimeError('fixture external source unavailable')
    monkeypatch.setattr(registry, 'apply_all', fail)
    before = dict(os.environ)
    with routed(home):
        loader.load_hermes_dotenv(hermes_home=home)
        assert ss.get_secret('OPENAI_API_KEY') == 'fake-literal'
    assert dict(os.environ) == before


def test_explicit_foreign_home_cannot_replace_active_scope(tmp_path):
    active = tmp_path / 'active'; active.mkdir()
    foreign = tmp_path / 'foreign'; foreign.mkdir()
    (active / '.env').write_text('OPENAI_API_KEY=fake-active\n')
    (foreign / '.env').write_text('OPENAI_API_KEY=fake-foreign\n')
    before = dict(os.environ)
    with routed(active):
        with pytest.raises(ValueError, match='routed profile'):
            loader.load_hermes_dotenv(hermes_home=foreign)
        assert ss.get_secret('OPENAI_API_KEY') == 'fake-active'
    assert dict(os.environ) == before
