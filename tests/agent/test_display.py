"""Tests for agent/display.py — build_tool_preview() and inline diff previews."""

import json
from pathlib import Path

import pytest
from unittest.mock import MagicMock

from agent.display import (
    build_tool_preview,
    capture_local_edit_snapshot,
    extract_edit_diff,
    get_cute_tool_message,
    set_tool_preview_max_len,
    shorten_tool_display_args,
    shorten_tool_display_value,
    _render_inline_unified_diff,
    _summarize_rendered_diff_sections,
    _set_args_include_pipefail_option,
    render_edit_diff_with_delta,
)


@pytest.fixture(autouse=True)
def reset_tool_preview_max_len():
    set_tool_preview_max_len(0)
    yield
    set_tool_preview_max_len(0)


class TestBuildToolPreview:
    """Tests for build_tool_preview defensive handling and normal operation."""

    def test_none_args_returns_none(self):
        """PR #453: None args should not crash, should return None."""
        assert build_tool_preview("terminal", None) is None

    def test_empty_dict_returns_none(self):
        """Empty dict has no keys to preview."""
        assert build_tool_preview("terminal", {}) is None

    def test_known_tool_with_primary_arg(self):
        """Known tool with its primary arg should return a preview string."""
        result = build_tool_preview("terminal", {"command": "ls -la"})
        assert result is not None
        assert "ls -la" in result

    def test_web_search_preview(self):
        result = build_tool_preview("web_search", {"query": "hello world"})
        assert result is not None
        assert "hello world" in result

    def test_read_file_preview(self):
        result = build_tool_preview("read_file", {"path": "/tmp/test.py", "offset": 1})
        assert result is not None
        assert "/tmp/test.py" in result

    def test_unknown_tool_with_fallback_key(self):
        """Unknown tool but with a recognized fallback key should still preview."""
        result = build_tool_preview("custom_tool", {"query": "test query"})
        assert result is not None
        assert "test query" in result

    def test_unknown_tool_no_matching_key(self):
        """Unknown tool with no recognized keys should return None."""
        result = build_tool_preview("custom_tool", {"foo": "bar"})
        assert result is None

    def test_long_value_truncated(self):
        """Preview should truncate long values."""
        long_cmd = "a" * 100
        result = build_tool_preview("terminal", {"command": long_cmd}, max_len=40)
        assert result is not None
        assert len(result) <= 43  # max_len + "..."

    def test_process_tool_with_none_args(self):
        """Process tool special case should also handle None args."""
        assert build_tool_preview("process", None) is None

    def test_process_tool_normal(self):
        result = build_tool_preview("process", {"action": "poll", "session_id": "abc123"})
        assert result is not None
        assert "poll" in result

    def test_todo_tool_read(self):
        result = build_tool_preview("todo", {"merge": False})
        assert result is not None
        assert "reading" in result

    def test_todo_tool_with_todos(self):
        result = build_tool_preview("todo", {"todos": [{"id": "1", "content": "test", "status": "pending"}]})
        assert result is not None
        assert "1 task" in result

    def test_memory_tool_add(self):
        result = build_tool_preview("memory", {"action": "add", "target": "user", "content": "test note"})
        assert result is not None
        assert "user" in result

    def test_memory_replace_missing_old_text_marked(self):
        # Avoid empty quotes "" in the preview when old_text is missing/None.
        result = build_tool_preview("memory", {"action": "replace", "target": "memory"})
        assert result == '~memory: "<missing old_text>"'
        result = build_tool_preview("memory", {"action": "remove", "target": "memory", "old_text": None})
        assert result == '-memory: "<missing old_text>"'

    def test_session_search_preview(self):
        result = build_tool_preview("session_search", {"query": "find something"})
        assert result is not None
        assert "find something" in result

    def test_delegate_task_single_goal_preview(self):
        result = build_tool_preview("delegate_task", {"goal": "Review gateway status"})
        assert result == "Review gateway status"

    def test_delegate_task_batch_goal_preview(self):
        result = build_tool_preview(
            "delegate_task",
            {"tasks": [{"goal": "Review PR A"}, {"goal": "Review PR B"}]},
        )
        assert result == "2 tasks: Review PR A | Review PR B"

    def test_delegate_task_batch_preview_handles_missing_non_string_goals(self):
        result = build_tool_preview(
            "delegate_task",
            {"tasks": [{"goal": None}, {"goal": 123}, "not-a-task"]},
        )
        assert result == "2 tasks: ? | 123"

    def test_delegate_task_batch_preview_respects_max_len(self):
        result = build_tool_preview(
            "delegate_task",
            {"tasks": [{"goal": "A" * 80}, {"goal": "B" * 80}]},
            max_len=30,
        )
        assert result == "2 tasks: AAAAAAAAAAAAAAAAAA..."
        assert len(result) == 30

    def test_false_like_args_zero(self):
        """Non-dict falsy values should return None, not crash."""
        assert build_tool_preview("terminal", 0) is None
        assert build_tool_preview("terminal", "") is None
        assert build_tool_preview("terminal", []) is None

    @pytest.mark.parametrize("tool_name", ["read_file", "patch", "write_file"])
    def test_repo_paths_are_shortened_for_display_only(self, tool_name, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        import agent.display as display_mod
        repo_root = Path(display_mod.__file__).resolve().parent.parent
        value = f"{repo_root}/gateway/run.py"
        expected = f"{repo_root.name}/gateway/run.py"

        assert shorten_tool_display_value(tool_name, "path", value) == expected
        assert build_tool_preview(tool_name, {"path": value}) == expected

    @pytest.mark.parametrize("tool_name", ["read_file", "patch", "write_file"])
    def test_profile_paths_are_shortened_for_display_only(self, tool_name, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        value = f"{tmp_path}/profiles/ops/scripts/kanban/discord_forum_mirror.py"
        expected = "ops/scripts/kanban/discord_forum_mirror.py"

        assert shorten_tool_display_value(tool_name, "path", value) == expected
        assert build_tool_preview(tool_name, {"path": value}) == expected

    @pytest.mark.parametrize("tool_name", ["read_file", "patch", "write_file"])
    def test_hermes_home_paths_are_shortened_for_display_only(self, tool_name, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        value = f"{hermes_home}/cron/jobs.json"
        expected = ".hermes/cron/jobs.json"

        assert shorten_tool_display_value(tool_name, "path", value) == expected
        assert build_tool_preview(tool_name, {"path": value}) == expected

    @pytest.mark.parametrize("tool_name", ["read_file", "patch", "write_file"])
    def test_home_paths_are_shortened_for_display_only(self, tool_name, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        home = str(Path.home()).rstrip("/")
        value = f"{home}/Documents/Runbooks/ops.md"
        expected = "~/Documents/Runbooks/ops.md"

        assert shorten_tool_display_value(tool_name, "path", value) == expected
        assert build_tool_preview(tool_name, {"path": value}) == expected

    def test_terminal_strict_shell_prefix_is_shortened_for_display_only(self):
        args = {"command": "set -euo pipefail python scripts/check.py --target important"}

        assert shorten_tool_display_value("terminal", "command", args["command"]) == "...python scripts/check.py --target important"
        assert build_tool_preview("terminal", args) == "...python scripts/check.py --target important"

    def test_terminal_strict_shell_prefix_with_newline_is_shortened_for_display_only(self):
        args = {"command": "set -euo pipefail\npython scripts/check.py --target important"}

        assert shorten_tool_display_value("terminal", "command", args["command"]) == "...python scripts/check.py --target important"
        assert build_tool_preview("terminal", args) == "...python scripts/check.py --target important"

    @pytest.mark.parametrize("flags", ["-e", "-u", "-eu"])
    def test_terminal_short_strict_shell_prefix_with_newline_is_shortened_for_display_only(self, flags):
        command = f"set {flags}\npython scripts/check.py --target important"

        assert shorten_tool_display_value("terminal", "command", command) == "...python scripts/check.py --target important"
        assert build_tool_preview("terminal", {"command": command}) == "...python scripts/check.py --target important"

    def test_terminal_set_o_pipefail_prefix_is_shortened_for_display_only(self):
        args = {"command": "set -o pipefail; python scripts/check.py --target important"}

        assert shorten_tool_display_value("terminal", "command", args["command"]) == "...python scripts/check.py --target important"
        assert build_tool_preview("terminal", args) == "...python scripts/check.py --target important"

    def test_terminal_unrelated_pipefail_text_is_not_shortened(self):
        command = "printf 'pipefail is mentioned but not enabled' && python scripts/check.py"
        args = {"command": command}

        assert shorten_tool_display_value("terminal", "command", command) == command
        assert build_tool_preview("terminal", args) == command

    def test_terminal_set_without_pipefail_is_not_shortened(self):
        command = "set -o nounset; echo pipefail"
        args = {"command": command}

        assert shorten_tool_display_value("terminal", "command", command) == command
        assert build_tool_preview("terminal", args) == command

    @pytest.mark.parametrize(
        "setup",
        ["set -- -e", "set positional -u", "set -eu -- positional", "set -- -o pipefail"],
    )
    def test_terminal_positional_set_args_are_not_shortened(self, setup):
        command = f"{setup}\necho visible"

        assert shorten_tool_display_value("terminal", "command", command) == command
        assert build_tool_preview("terminal", {"command": command}) == command.replace("\n", " ")

    def test_shortened_args_copy_preserves_original_execution_inputs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        import agent.display as display_mod
        repo_root = Path(display_mod.__file__).resolve().parent.parent
        original = f"{repo_root}/tests/agent/test_display.py"
        args = {
            "path": original,
            "content": "keep this content visible",
        }

        display_args = shorten_tool_display_args("write_file", args)

        assert display_args["path"] == f"{repo_root.name}/tests/agent/test_display.py"
        assert display_args["content"] == "keep this content visible"
        assert args["path"] == original


class TestSetArgsIncludePipefailOption:
    """Tests for _set_args_include_pipefail_option flag pattern validation."""

    def test_single_flag_o_exact_match(self):
        """Exact match: -o or +o."""
        assert _set_args_include_pipefail_option(["set", "-o", "pipefail"])
        assert _set_args_include_pipefail_option(["set", "+o", "pipefail"])

    def test_combined_flags_with_o(self):
        """Combined flag strings with o: -euo, -eo, -op, etc."""
        assert _set_args_include_pipefail_option(["set", "-euo", "pipefail"])
        assert _set_args_include_pipefail_option(["set", "-eo", "pipefail"])
        assert _set_args_include_pipefail_option(["set", "-op", "pipefail"])
        assert _set_args_include_pipefail_option(["set", "+euo", "pipefail"])

    def test_five_letter_flags_with_o(self):
        """Maximum length (5 letters) with o included."""
        assert _set_args_include_pipefail_option(["set", "-euopx", "pipefail"])
        assert _set_args_include_pipefail_option(["set", "+abcod", "pipefail"])

    def test_flags_without_o_not_matched(self):
        """Flags without o: -e, -u, -eup (no o)."""
        assert not _set_args_include_pipefail_option(["set", "-e", "pipefail"])
        assert not _set_args_include_pipefail_option(["set", "-u", "pipefail"])
        assert not _set_args_include_pipefail_option(["set", "-eup", "pipefail"])

    def test_invalid_flag_too_long(self):
        """Flags longer than 5 letters are rejected (shell limitation)."""
        assert not _set_args_include_pipefail_option(["set", "-euopxyz", "pipefail"])

    def test_invalid_flag_non_alpha(self):
        """Flags with non-alphabetic characters are rejected."""
        assert not _set_args_include_pipefail_option(["set", "-eo1", "pipefail"])
        assert not _set_args_include_pipefail_option(["set", "-e-o", "pipefail"])
        assert not _set_args_include_pipefail_option(["set", "-e_o", "pipefail"])

    def test_pipefail_as_first_token_not_matched(self):
        """pipefail as first token (not preceded by flags) returns false."""
        assert not _set_args_include_pipefail_option(["pipefail"])

    def test_pipefail_not_in_tokens(self):
        """No pipefail in tokens returns false."""
        assert not _set_args_include_pipefail_option(["set", "-euo"])
        assert not _set_args_include_pipefail_option(["set", "-euo", "other"])

    def test_multiple_tokens_before_pipefail(self):
        """Only immediate previous token matters."""
        assert _set_args_include_pipefail_option(["set", "-e", "-u", "-o", "pipefail"])
        assert not _set_args_include_pipefail_option(["set", "-e", "pipefail"])


class TestCuteToolMessagePreviewLength:
    def test_terminal_preview_unlimited_when_config_is_zero(self):
        set_tool_preview_max_len(0)
        command = "curl -s http://localhost:9222/json/list | jq -r '.[] | select(.type==\"page\")' | head -5"

        line = get_cute_tool_message("terminal", {"command": command}, 0.1)

        assert command in line
        assert "..." not in line

    def test_terminal_preview_uses_positive_configured_limit(self):
        set_tool_preview_max_len(80)
        command = "curl -s http://localhost:9222/json/list | jq -r '.[] | select(.type==\"page\")' | head -5"

        line = get_cute_tool_message("terminal", {"command": command}, 0.1)

        assert command[:77] in line
        assert "..." in line
        assert "head -5" not in line

    def test_search_files_preview_uses_positive_configured_limit_not_default(self):
        set_tool_preview_max_len(80)
        pattern = "function.formatToolCall.context.preview.compactPreview.maxLength.truncate"

        line = get_cute_tool_message("search_files", {"pattern": pattern}, 0.1)

        assert pattern in line
        assert "..." not in line

    def test_path_preview_uses_positive_configured_limit_not_default(self):
        set_tool_preview_max_len(80)
        path = "/tmp/hermes-test-preview-length/deeply/nested/path/test-output.txt"

        line = get_cute_tool_message("read_file", {"path": path}, 0.1)

        assert path in line
        assert "..." not in line

    def test_write_file_lint_error_result_is_not_marked_failed(self):
        result = json.dumps({
            "bytes_written": 12,
            "lint": {"status": "error", "output": "SyntaxError: invalid syntax"},
        })

        line = get_cute_tool_message("write_file", {"path": "/tmp/a.py"}, 0.1, result=result)

        assert "[error]" not in line

    def test_patch_lsp_diagnostics_result_is_not_marked_failed(self):
        result = json.dumps({
            "success": True,
            "diff": "--- a/tmp.py\n+++ b/tmp.py\n",
            "lsp_diagnostics": "<diagnostics>ERROR [1:1] type mismatch</diagnostics>",
        })

        line = get_cute_tool_message("patch", {"path": "/tmp/a.py"}, 0.1, result=result)

        assert "[error]" not in line

    def test_delegate_task_batch_message_includes_goals(self):
        line = get_cute_tool_message(
            "delegate_task",
            {"tasks": [{"goal": "Review PR A"}, {"goal": "Review PR B"}]},
            1.2,
        )
        assert "2x: Review PR A | Review PR B" in line


class TestEditDiffPreview:
    def test_extract_edit_diff_for_patch(self):
        diff = extract_edit_diff("patch", '{"success": true, "diff": "--- a/x\\n+++ b/x\\n"}')
        assert diff is not None
        assert "+++ b/x" in diff

    def test_render_inline_unified_diff_colors_added_and_removed_lines(self):
        rendered = _render_inline_unified_diff(
            "--- a/cli.py\n"
            "+++ b/cli.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-old line\n"
            "+new line\n"
            " context\n"
        )

        assert "a/cli.py" in rendered[0]
        assert "b/cli.py" in rendered[0]
        assert any("old line" in line for line in rendered)
        assert any("new line" in line for line in rendered)
        assert any("48;2;" in line for line in rendered)

    def test_extract_edit_diff_ignores_non_edit_tools(self):
        assert extract_edit_diff("web_search", '{"diff": "--- a\\n+++ b\\n"}') is None

    def test_extract_edit_diff_uses_local_snapshot_for_write_file(self, tmp_path):
        target = tmp_path / "note.txt"
        target.write_text("old\n", encoding="utf-8")

        snapshot = capture_local_edit_snapshot("write_file", {"path": str(target)})

        target.write_text("new\n", encoding="utf-8")

        diff = extract_edit_diff(
            "write_file",
            '{"bytes_written": 4}',
            function_args={"path": str(target)},
            snapshot=snapshot,
        )

        assert diff is not None
        assert "--- a/" in diff
        assert "+++ b/" in diff
        assert "-old" in diff
        assert "+new" in diff

    def test_render_edit_diff_with_delta_invokes_printer(self):
        printer = MagicMock()

        rendered = render_edit_diff_with_delta(
            "patch",
            '{"diff": "--- a/x\\n+++ b/x\\n@@ -1 +1 @@\\n-old\\n+new\\n"}',
            print_fn=printer,
        )

        assert rendered is True
        assert printer.call_count >= 2
        calls = [call.args[0] for call in printer.call_args_list]
        assert any("a/x" in line and "b/x" in line for line in calls)
        assert any("old" in line for line in calls)
        assert any("new" in line for line in calls)

    def test_render_edit_diff_with_delta_skips_without_diff(self):
        rendered = render_edit_diff_with_delta(
            "patch",
            '{"success": true}',
        )

        assert rendered is False

    def test_render_edit_diff_with_delta_handles_renderer_errors(self, monkeypatch):
        printer = MagicMock()

        monkeypatch.setattr("agent.display._summarize_rendered_diff_sections", MagicMock(side_effect=RuntimeError("boom")))

        rendered = render_edit_diff_with_delta(
            "patch",
            '{"diff": "--- a/x\\n+++ b/x\\n"}',
            print_fn=printer,
        )

        assert rendered is False
        assert printer.call_count == 0

    def test_summarize_rendered_diff_sections_truncates_large_diff(self):
        diff = "--- a/x.py\n+++ b/x.py\n" + "".join(f"+line{i}\n" for i in range(120))

        rendered = _summarize_rendered_diff_sections(diff, max_lines=20)

        assert len(rendered) == 21
        assert "omitted" in rendered[-1]

    def test_summarize_rendered_diff_sections_limits_file_count(self):
        diff = "".join(
            f"--- a/file{i}.py\n+++ b/file{i}.py\n+line{i}\n"
            for i in range(8)
        )

        rendered = _summarize_rendered_diff_sections(diff, max_files=3, max_lines=50)

        assert any("a/file0.py" in line for line in rendered)
        assert any("a/file1.py" in line for line in rendered)
        assert any("a/file2.py" in line for line in rendered)
        assert not any("a/file7.py" in line for line in rendered)
        assert "additional file" in rendered[-1]
