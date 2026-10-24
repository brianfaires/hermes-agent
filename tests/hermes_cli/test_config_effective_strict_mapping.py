"""Strict policy readers reject non-mapping YAML while retaining empty-setting semantics."""

import pytest

from hermes_cli import config
from hermes_cli.config_effective import load_user_config_effective


@pytest.mark.parametrize('warm_reader', [None, load_user_config_effective, config.read_raw_config])
def test_strict_reader_rejects_nonmapping_after_any_cached_reader(tmp_path, monkeypatch, warm_reader):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    path = tmp_path / 'config.yaml'
    path.write_text('[]\n')
    if warm_reader is not None:
        warm_reader()

    with pytest.raises(ValueError, match='mapping'):
        load_user_config_effective(path, fail_closed=True)


@pytest.mark.parametrize('document', [None, '', 'null\n', '{}\n'])
def test_strict_reader_accepts_missing_or_empty_settings(tmp_path, monkeypatch, document):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    path = tmp_path / 'config.yaml'
    if document is not None:
        path.write_text(document)
    assert load_user_config_effective(path, fail_closed=True) == {}
