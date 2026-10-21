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
