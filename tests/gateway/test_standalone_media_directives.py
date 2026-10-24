"""Prose/examples stay literal; complete standalone spaced paths remain deliverable."""
import os

import pytest

from gateway.platforms.base import BasePlatformAdapter


@pytest.mark.parametrize('template,name', [
    ('For example MEDIA:{path}', 'image.png'),
    ('MEDIA:{path} is an example', 'image.png'),
    ('`example` MEDIA:{path}', 'image.png'),
    ('MEDIA:{path} `example`', 'image.png'),
    ('```text\nMEDIA:{path}\n```', 'image.png'),
    ('~~~text\nMEDIA:{path}\n~~~', 'image.png'),
    ('```text\nMEDIA:{path}', 'image.png'),
    ('> MEDIA:{path}', 'image.png'),
    ('The file is {path} for reference.', 'image.png'),
    ('> {path}', 'image.png'),
    ('MEDIA:{path} is the log you asked for', 'runtime full.log'),
], ids=['prefix', 'suffix', 'masked-prefix', 'masked-suffix', 'backtick-fence', 'tilde-fence',
        'open-fence', 'blockquote', 'bare-prose', 'quoted-bare', 'spaced-prose'])
def test_literal_examples_never_become_attachment_candidates(tmp_path, monkeypatch, template, name):
    path = tmp_path / name
    path.write_bytes(b'synthetic media')
    monkeypatch.setattr('gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS', (tmp_path,))
    text = template.format(path=path)
    media, cleaned = BasePlatformAdapter.extract_media(text)
    assert media == []
    assert cleaned == text
    assert BasePlatformAdapter.strip_media_directives_for_display(text) == text
    files, remaining = BasePlatformAdapter.extract_local_files(cleaned)
    assert files == []
    assert remaining == text


@pytest.mark.parametrize('name', ['map data.kmz', 'runtime full.log', 'Caddy file',
                                  'report.png notes.custom', 'one two three four five six seven eight nine ten.log'])
def test_complete_standalone_paths_with_spaces_never_shorten_to_existing_prefix(tmp_path, monkeypatch, name):
    path = tmp_path / name
    path.write_bytes(b'synthetic media')
    (tmp_path / 'report.png').write_bytes(b'prefix decoy')
    monkeypatch.setattr('gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS', (tmp_path,))
    text = 'Summary.\nMEDIA:' + str(path)
    media, cleaned = BasePlatformAdapter.extract_media(text)
    assert [(os.path.realpath(candidate), voice) for candidate, voice in media] == [(os.path.realpath(path), False)]
    assert cleaned == 'Summary.'
    assert BasePlatformAdapter.strip_media_directives_for_display(text) == 'Summary.'
