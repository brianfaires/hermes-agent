"""Legacy configuration writers must not erase a newer structural edit."""
import copy

import pytest

from hermes_cli import config
from hermes_cli.plugins_state import save_plugin_setting


@pytest.fixture
def document(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    path = tmp_path / 'config.yaml'
    path.write_text('custom: {first: 1}\nplugins: {}\n')
    return path


@pytest.mark.parametrize('reader', [config.load_config, config.read_raw_config,
                                    config.read_user_config_raw,
                                    config.require_readable_config_before_write])
@pytest.mark.parametrize('clone', [lambda value: value, copy.deepcopy, lambda value: value.copy()])
def test_legacy_snapshot_refuses_stale_save(document, reader, clone):
    state = clone(reader())
    state['custom']['first'] = 2
    save_plugin_setting('concurrent', ('mode',), 'on')
    before = document.read_bytes()
    with pytest.raises(RuntimeError, match='changed since it was read'):
        config.save_config(state)
    assert document.read_bytes() == before
    assert config.read_user_config_raw()['plugins']['entries']['concurrent']['settings']['mode'] == 'on'


def test_native_plugin_settings_and_repeated_saves_complete(document):
    save_plugin_setting('qualified-plugin', ('mode',), 'on')
    state = config.load_config()
    state['custom']['first'] = 2
    config.save_config(state)
    state['custom']['first'] = 3
    config.save_config(state)
    final = config.read_user_config_raw()
    assert final['custom']['first'] == 3
    assert final['plugins']['entries']['qualified-plugin']['settings']['mode'] == 'on'


@pytest.mark.parametrize('writer', [config.atomic_config_write, config.atomic_config_replace])
def test_direct_writer_rejects_stale_raw_snapshot(document, writer):
    state = config.read_user_config_raw()
    save_plugin_setting('concurrent', ('mode',), 'on')
    before = document.read_bytes()
    with pytest.raises(RuntimeError, match='changed since it was read'):
        writer(document, state)
    assert document.read_bytes() == before


def test_snapshot_cannot_be_saved_to_another_profile(document, tmp_path, monkeypatch):
    state = config.read_user_config_raw()
    other = tmp_path / 'other'
    other.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(other))
    with pytest.raises(RuntimeError, match='changed since it was read'):
        config.save_config(state)
    assert not (other / 'config.yaml').exists()
