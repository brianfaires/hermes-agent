"""External skill paths expand at the operation's served-profile boundary."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from agent import skill_utils
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


@pytest.fixture(autouse=True)
def isolated_config_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    skill_utils._external_dirs_cache_clear()
    yield
    skill_utils._external_dirs_cache_clear()


def config(home, entries):
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.yaml').write_text(json.dumps({'skills': {'external_dirs': entries}}))


def dirs_for(home):
    token = set_hermes_home_override(str(home))
    try:
        return skill_utils.get_external_skills_dirs()
    finally:
        reset_hermes_home_override(token)


def test_exact_owner_tokens_keep_user_and_suffix_semantics(tmp_path, monkeypatch):
    launch = tmp_path / 'launch'; launch.mkdir()
    served = tmp_path / 'custom deployment' / 'profiles' / 'served'
    shared = tmp_path / 'shared'; shared.mkdir()
    suffix = tmp_path / 'suffix'; suffix.mkdir()
    user = tmp_path / 'user'; user.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(launch))
    monkeypatch.setenv('HERMES_HOME_SUFFIX', str(suffix))
    monkeypatch.setenv('SHARED_SKILLS', str(shared))
    entries = ['$HERMES_HOME/one', '${HERMES_HOME}/two', '$HERMES_HOME_SUFFIX',
               '$SHARED_SKILLS', '~/user', '$HOME/user', 'relative',
               '$HERMES_HOME/skills', '${HERMES_HOME}/one', 'missing', 'file']
    config(served, entries)
    for n in ('one', 'two', 'skills', 'relative'):
        (served / n).mkdir()
    (served / 'file').write_text('not a directory')
    before = dict(os.environ)
    assert dirs_for(served) == [served / 'one', served / 'two', suffix, shared, user, served / 'relative']
    assert dict(os.environ) == before


def test_replacement_dollars_are_literal(tmp_path, monkeypatch):
    served = tmp_path / '$OTHER' / 'served'
    config(served, ['$HERMES_HOME/ext', '${HERMES_HOME}/ext'])
    (served / 'ext').mkdir()
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'launch'))
    monkeypatch.setenv('OTHER', 'incorrect-recursive-expansion')
    assert dirs_for(served) == [served / 'ext']


def test_unchanged_config_mtime_reexpands_other_variables(tmp_path, monkeypatch):
    served = tmp_path / 'served'
    config(served, ['$EXTERNAL_ROOT'])
    first = tmp_path / 'first'; first.mkdir()
    second = tmp_path / 'second'; second.mkdir()
    stamp = (served / 'config.yaml').stat().st_mtime_ns
    monkeypatch.setenv('EXTERNAL_ROOT', str(first))
    assert dirs_for(served) == [first]
    monkeypatch.setenv('EXTERNAL_ROOT', str(second))
    assert dirs_for(served) == [second]
    assert (served / 'config.yaml').stat().st_mtime_ns == stamp


def test_consecutive_and_concurrent_profiles_never_use_launch_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'launch'))
    homes = [tmp_path / 'custom root' / 'profiles' / n for n in ('alpha', 'beta')]
    for h in homes:
        config(h, ['$HERMES_HOME/ext']); (h / 'ext').mkdir()
    before = dict(os.environ)
    for h in [*homes, homes[0]]:
        assert dirs_for(h) == [h / 'ext']
    barrier = Barrier(2)
    def worker(h):
        token = set_hermes_home_override(str(h))
        try:
            for _ in range(3):
                barrier.wait(5)
                assert skill_utils.get_external_skills_dirs() == [h / 'ext']
        finally:
            reset_hermes_home_override(token)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(worker, homes))
    assert dict(os.environ) == before


def test_windows_path_tokens_preserve_native_expandvars_semantics(monkeypatch):
    # ntpath is a pure path grammar: this does not pretend the host OS changed.
    import ntpath
    monkeypatch.setenv('OTHER', 'shared')
    monkeypatch.setenv('HERMES_HOME', 'launch')
    monkeypatch.setenv('HERMES_HOME-suffix', 'suffix-variable')
    home = Path('served$OTHER')
    for entry in ('%OTHER%/skills', '$$HERMES_HOME', "'$HERMES_HOME'", '$HERMES_HOME-suffix', '%%OTHER%%'):
        assert skill_utils._expand_external_dir(entry, home, ntpath) == ntpath.expandvars(entry)
    assert skill_utils._expand_external_dir('${HERMES_HOME}/%OTHER%', home, ntpath) == 'served$OTHER/shared'


@pytest.mark.parametrize('grammar', ['native', 'ntpath'])
@pytest.mark.parametrize('entry', [
    '${BROKEN/$OTHER', '${BROKEN/$HERMES_HOME', '${BROKEN/${HERMES_HOME}',
    "'$OTHER", "'$HERMES_HOME", '"$HERMES_HOME"', '$$HERMES_HOME',
    '$$$HERMES_HOME', '${HERMES_HOME', '${HERMES_HOME}/$OTHER',
    '%BROKEN/$HERMES_HOME', '%HERMES_HOME%', '$HERMES_HOME_SUFFIX',
])
def test_path_grammar_matches_native_expansion(monkeypatch, grammar, entry):
    import ntpath
    path_module = os.path if grammar == 'native' else ntpath
    # Equal ambient/served values make stdlib expansion an exact oracle,
    # including malformed token boundaries and quoted/escaped owner tokens.
    monkeypatch.setenv('HERMES_HOME', 'served$OTHER')
    monkeypatch.setenv('OTHER', 'shared')
    monkeypatch.setenv('HERMES_HOME_SUFFIX', 'suffix')
    before = dict(os.environ)
    assert skill_utils._expand_external_dir(entry, Path('served$OTHER'), path_module) == path_module.expanduser(path_module.expandvars(entry))
    assert dict(os.environ) == before
