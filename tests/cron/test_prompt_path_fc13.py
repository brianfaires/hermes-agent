"""FC-13: file-backed cron prompts (prompt_path) on v0.21 reconstruction tip."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cron.jobs import (
    combine_prompt_sources,
    create_job,
    job_payload_is_empty,
    read_prompt_file,
    update_job,
    _normalize_prompt_path,
)
from cron.scheduler import _build_job_prompt


def test_normalize_prompt_path_requires_absolute(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        _normalize_prompt_path("relative/prompt.md")
    abs_path = tmp_path / "prompt.md"
    abs_path.write_text("hello", encoding="utf-8")
    assert _normalize_prompt_path(str(abs_path)) == str(abs_path.resolve())


def test_read_prompt_file_bounds_and_missing(tmp_path):
    missing = tmp_path / "missing.md"
    with pytest.raises(ValueError, match="does not exist"):
        read_prompt_file(str(missing))
    path = tmp_path / "ok.md"
    path.write_text("from-file", encoding="utf-8")
    assert read_prompt_file(str(path)) == "from-file"


def test_create_job_prompt_path_only(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    prompt_file = tmp_path / "ops.md"
    prompt_file.write_text("maintained prompt body", encoding="utf-8")

    job = create_job(
        prompt="",
        schedule="every 1h",
        name="path-only",
        prompt_path=str(prompt_file),
        deliver="local",
    )
    assert job["prompt"] == ""
    assert job["prompt_path"] == str(prompt_file.resolve())
    assert not job_payload_is_empty(job)

    built = _build_job_prompt(job)
    assert "maintained prompt body" in built


def test_create_job_combines_inline_and_path(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    prompt_file = tmp_path / "ops.md"
    prompt_file.write_text("FILE", encoding="utf-8")

    job = create_job(
        prompt="INLINE",
        schedule="every 1h",
        name="combo",
        prompt_path=str(prompt_file),
        deliver="local",
    )
    assert combine_prompt_sources(job["prompt"], job["prompt_path"]) == "INLINE\nFILE"
    built = _build_job_prompt(job)
    assert "INLINE\nFILE" in built


def test_update_job_clears_and_sets_prompt_path(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    prompt_file = tmp_path / "ops.md"
    prompt_file.write_text("v1", encoding="utf-8")
    job = create_job(
        prompt="keep",
        schedule="every 1h",
        name="upd",
        prompt_path=str(prompt_file),
        deliver="local",
    )
    updated = update_job(job["id"], {"prompt_path": None})
    assert updated["prompt_path"] is None
    prompt_file.write_text("v2", encoding="utf-8")
    updated = update_job(job["id"], {"prompt_path": str(prompt_file)})
    assert updated["prompt_path"] == str(prompt_file.resolve())
    assert "v2" in _build_job_prompt(updated)


def test_cronjob_schema_exposes_prompt_path():
    from tools.cronjob_tools import CRONJOB_SCHEMA

    props = CRONJOB_SCHEMA["parameters"]["properties"]
    assert "prompt_path" in props
    assert "prompt_path" in props["action"]["description"]
