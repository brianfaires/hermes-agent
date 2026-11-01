"""The API image renderer shares executable standalone-media eligibility."""
import pytest

from gateway.platforms.api_server_media import resolve_media_to_data_urls


@pytest.mark.parametrize('template', ['Example MEDIA:{path}', '`example` MEDIA:{path}',
                                     'MEDIA:{path} `example`', '```\nMEDIA:{path}\n```',
                                     '~~~\nMEDIA:{path}\n~~~', '```\nMEDIA:{path}'])
def test_api_literal_media_does_not_inline_file_bytes(tmp_path, monkeypatch, template):
    path = tmp_path / 'image.png'
    path.write_bytes(b'synthetic image bytes')
    monkeypatch.setattr('gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS', (tmp_path,))
    text = template.format(path=path)
    assert resolve_media_to_data_urls(text) == text


def test_api_standalone_spaced_image_and_sentinel_are_preserved(tmp_path, monkeypatch):
    path = tmp_path / 'image with spaces.png'
    path.write_bytes(b'synthetic image bytes')
    monkeypatch.setattr('gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS', (tmp_path,))
    out = resolve_media_to_data_urls('Summary.\nMEDIA:' + str(path) + '<|eos|>')
    assert out.startswith('Summary.\n![image](data:image/png;base64,')
    assert 'MEDIA:' not in out and '<|eos|>' not in out
