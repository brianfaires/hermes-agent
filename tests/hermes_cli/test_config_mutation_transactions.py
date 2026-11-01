"""Concurrent native config set calls must retain both independent mutations."""
import threading

import pytest
import hermes_yaml as yaml
from hermes_cli import config


@pytest.fixture
def document(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    path = tmp_path / 'config.yaml'
    path.write_text('race: {}\n')
    return path


def read(path):
    return yaml.safe_load(path.read_text())


def test_native_set_read_and_commit_are_one_transaction(document, monkeypatch):
    original = config.require_readable_config_before_write
    first_read, second_read, release_first, second_finished = (threading.Event() for _ in range(4))
    errors = []
    blocked = set()

    def snapshot(path):
        value = original(path)
        name = threading.current_thread().name
        if name == 'first' and name not in blocked:
            blocked.add(name)
            first_read.set()
            if not release_first.wait(10):
                raise TimeoutError('test failed to release first writer')
        elif name == 'second':
            second_read.set()
        return value

    def write(name):
        try:
            config.set_config_value('race.' + name, '1', force=True)
        except BaseException as exc:
            errors.append(exc)
        finally:
            if name == 'second':
                second_finished.set()

    monkeypatch.setattr(config, 'require_readable_config_before_write', snapshot)
    first = threading.Thread(target=write, args=('first',), name='first')
    second = threading.Thread(target=write, args=('second',), name='second')
    first.start()
    assert first_read.wait(10)
    second.start()
    # Without shared exclusion the second commits its old snapshot before the first resumes.
    if second_read.wait(2):
        assert second_finished.wait(10)
    release_first.set()
    first.join(10)
    second.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert read(document)['race'] == {'first': 1, 'second': 1}


def test_native_writers_share_the_cross_process_mutation_lock(document):
    import os
    from pathlib import Path
    import subprocess
    import sys
    from hermes_cli.config_transactions import config_mutation_lock

    child = None
    try:
        with config_mutation_lock(document):
            state = config.require_readable_config_before_write(document)
            child = subprocess.Popen(
                [sys.executable, '-c',
                 'from hermes_cli import config; print("ready", flush=True); '
                 'config.set_config_value("race.child", "1", force=True)'],
                cwd=Path(__file__).resolve().parents[2],
                env={**os.environ, 'HERMES_HOME': str(document.parent)},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert child.stdout.readline().strip() == 'ready'
            with pytest.raises(subprocess.TimeoutExpired):
                child.wait(timeout=2)
            state['race']['parent'] = 1
            config.atomic_config_replace(document, state)
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, (stdout, stderr)
        assert read(document)['race'] == {'parent': 1, 'child': 1}
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_rejected_structural_callback_leaves_config_and_mirrors_intact(document):
    from hermes_cli.config_transactions import mutate_config

    mirror = document.parent / '.env'
    mirror.write_text('TERMINAL_ENV=local\n')
    before = document.read_bytes(), mirror.read_bytes()

    def invalid(state):
        state['terminal'] = {'backend': 'ssh'}
        raise ValueError('invalid complete edit')

    with pytest.raises(ValueError, match='invalid complete edit'):
        mutate_config(document, invalid)
    assert (document.read_bytes(), mirror.read_bytes()) == before


@pytest.mark.parametrize('same_owner', [False, True])
def test_structural_parent_edit_syncs_only_owning_profile_mirrors(document, monkeypatch, same_owner):
    from agent.secret_scope import current_secret_scope, reset_secret_scope, set_secret_scope
    from dotenv import dotenv_values
    from hermes_cli.config_transactions import mutate_config
    from hermes_constants import get_hermes_home

    document.write_text('# Retain this comment\nterminal: {backend: local, timeout: 5}\nrace: {}\n')
    mirror = document.parent / '.env'
    mirror.write_text('TERMINAL_ENV=local\nTERMINAL_TIMEOUT=5\n')
    other = document.parent / 'launch-profile'
    other.mkdir()
    other_mirror = other / '.env'
    other_mirror.write_text('TERMINAL_ENV=docker\n')
    monkeypatch.setenv('HERMES_HOME', str(other))

    def change(state):
        state['terminal'] = {'backend': 'ssh'}
        state['race']['structural'] = 1
        return 'committed'

    installed_scope = {'TERMINAL_ENV': 'local' if same_owner else 'docker'}
    token = set_secret_scope(installed_scope, profile_home=str(document.parent if same_owner else other))
    try:
        assert mutate_config(document, change) == 'committed'
        assert current_secret_scope() is installed_scope
        assert installed_scope == {'TERMINAL_ENV': 'ssh' if same_owner else 'docker'}
    finally:
        reset_secret_scope(token)
    assert read(document) == {'terminal': {'backend': 'ssh'}, 'race': {'structural': 1}}
    assert '# Retain this comment' in document.read_text()
    assert dotenv_values(mirror) == {'TERMINAL_ENV': 'ssh'}
    assert other_mirror.read_text() == 'TERMINAL_ENV=docker\n'
    assert get_hermes_home() == other
