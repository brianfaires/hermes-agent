"""Opaque synthetic credentials in diagnostic output, never live diagnostics."""

import shlex

import pytest

import agent.redact as redact


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(redact, "_REDACT_ENABLED", True)


@pytest.mark.parametrize("command", [
    "ps auxww", "/usr/bin/ps eww", "sudo -n /bin/systemctl status demo",
    "timeout 2 /usr/bin/journalctl -u demo", "echo ready && ps aux | cat",
    "bash -lc '/bin/systemctl show demo'",
])
def test_diagnostic_assignments(command):
    secret = "fictional" + "OpaqueValue976413"
    output = f"PID=42 ActiveState=active DEMO_TOKEN={secret} MODE=normal --flag ok"
    result = redact.redact_terminal_output(output, command)
    assert secret not in result
    for benign in ("PID=42", "ActiveState=active", "MODE=normal", "--flag ok"):
        assert benign in result


@pytest.mark.parametrize("command", [None, "", "diagnostic-wrapper"])
def test_unknown_context_setenv(command):
    secret = "fictional" + "OpaqueValue976413"
    output = f"42 runner --setenv=DEMO_TOKEN={secret} --setenv=MODE=normal"
    result = redact.redact_terminal_output(output, command)
    assert secret not in result
    assert "42 runner" in result
    assert "--setenv=MODE=normal" in result


@pytest.mark.parametrize("name", ["DEMO_TOKEN", "demo_token"])
@pytest.mark.parametrize("value", [
    "opaqueFirst opaqueSecond",
    "opaqueFirst\nopaqueSecond",
    'opaqueFirst\\"opaqueSecond',
    "opaqueFirst'opaqueSecond",
])
def test_double_quoted_values(name, value):
    # Check each substantial fragment, not only the unsplit full value.
    text = f'{name}="{value}" MODE=normal\nPID=42'
    result = redact.redact_sensitive_text(text)
    assert "opaqueFirst" not in result
    assert "opaqueSecond" not in result
    assert "MODE=normal\nPID=42" in result


@pytest.mark.parametrize("assignment", [
    "DEMO_TOKEN='opaqueFirst opaqueSecond'",
    "DEMO_TOKEN='opaqueFirst\nopaqueSecond'",
    "DEMO_TOKEN=opaqueFirst\\ opaqueSecond",
    "DEMO_TOKEN='opaqueFirst'\\''opaqueSecond'",
    '"DEMO_TOKEN=opaqueFirst opaqueSecond"',
    "'DEMO_TOKEN=opaqueFirst opaqueSecond'",
])
def test_shell_word_and_outer_assignment_quotes(assignment):
    text = f"--setenv={assignment} --unit=demo MODE=normal"
    result = redact.redact_sensitive_text(text)
    assert "opaqueFirst" not in result
    assert "opaqueSecond" not in result
    assert "--unit=demo MODE=normal" in result


@pytest.mark.parametrize("command", ["cat config.py", "cat config.yaml", "cat .env.example"])
def test_source_and_template_display(command):
    text = 'MAX_TOKENS=100\nDEMO_TOKEN="example value"\n"apiKey": "fixture"'
    assert redact.redact_terminal_output(text, command) == text
    assert redact.redact_sensitive_text(text, code_file=True) == text


def test_opt_out_and_force(monkeypatch):
    text = "DEMO_TOKEN=fictionalOpaqueValue976413"
    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)
    assert redact.redact_terminal_output(text, "ps aux") == text
    assert "fictionalOpaqueValue976413" not in redact.redact_terminal_output(
        text, "ps aux", force=True
    )


def test_unquoted_whitespace_has_no_recoverable_value_boundary():
    result = redact.redact_sensitive_text("DEMO_TOKEN=opaqueFirst status active")
    assert "opaqueFirst" not in result
    assert "status active" in result


@pytest.mark.parametrize("suffix", ["opaqueSuffix976413", "'opaqueSuffix976413'", '"opaqueSuffix976413"', "'opaqueSuffix976413\nsecondFragment'"])
@pytest.mark.parametrize("command", ["ps aux", "diagnostic-wrapper"])
def test_outer_quote_concatenation(suffix, command):
    from agent.redact import redact_terminal_output
    text = '--setenv="DEMO_TOKEN=opaqueFirst"' + suffix + ' MODE=normal'
    result = redact_terminal_output(text, command)
    assert result.count("\n") == text.count("\n")
    assert "secondFragment" not in result
    assert "MODE=normal" in result
    assert "opaqueSuffix976413" not in result




@pytest.mark.parametrize("lookup", [
    "os.getenv('EXAMPLE_TOKEN')", "os.environ['EXAMPLE_TOKEN']",
    "os.environ.get('EXAMPLE_TOKEN')", "process.env.EXAMPLE_TOKEN", "$ENV{EXAMPLE_TOKEN}",
])
def test_assignment_lookup_prefix_exception(lookup):
    text = "DEMO_TOKEN=" + lookup + " MODE=normal"
    assert redact.redact_sensitive_text(text) == text


@pytest.mark.parametrize("command", [
    "ps auxww", "diagnostic-wrapper",
    "ps auxww; git show HEAD:tools/terminal_tool.py",
])
def test_whole_option_shlex_join(command):
    # Reconstructed synthetic argv from the documented Ops failure; never run.
    names = [
        "TAVILY_API_KEY", "BRAVE_API_KEY", "YOU_DOT_COM_API_KEY",
        "HERMES_WEBHOOK_TOKEN", "HERMES_BACKUP_PASSWORD",
    ]
    fragments = [
        (f"FictionalFirst{i}", f"FictionalSecond{i}", f"FictionalThird{i}")
        for i in range(len(names))
    ]
    options = [
        f"--setenv={name}={first} {second}\n{third}"
        for name, (first, second, third) in zip(names, fragments)
    ]
    text = shlex.join([
        "systemd-run", *options, "--unit=synthetic", "--", "/synthetic/codex",
    ])
    result = redact.redact_terminal_output(text, command)
    assert not [part for group in fragments for part in group if part in result]
    assert result.count("\n") == text.count("\n")
    assert result.startswith("systemd-run ")
    assert result.endswith(" --unit=synthetic -- /synthetic/codex")
    assert all(f"--setenv={name}=" in result for name in names)


@pytest.mark.parametrize("command", ["ps auxww", "diagnostic-wrapper"])
@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("suffix", [
    "", "FictionalSuffix", "'FictionalSuffix\nFictionalTail'",
])
def test_whole_option_quotes_and_suffix(command, quote, suffix):
    text = (
        f"runner {quote}--setenv=DEMO_TOKEN=FictionalFirst FictionalSecond\n"
        f"FictionalThird{quote}{suffix} '--setenv=MODE=normal mode' --unit=demo"
    )
    result = redact.redact_terminal_output(text, command)
    for part in ("First", "Second", "Third", "Suffix", "Tail"):
        assert f"Fictional{part}" not in result
    assert result.count("\n") == text.count("\n")
    assert result.startswith(f"runner {quote}--setenv=DEMO_TOKEN=")
    assert result.endswith(" '--setenv=MODE=normal mode' --unit=demo")


@pytest.mark.parametrize("command", ["ps auxww", "diagnostic-wrapper"])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_whole_option_lookup_control(command, quote):
    text = f"{quote}--setenv=DEMO_TOKEN=process.env.EXAMPLE_TOKEN{quote} MODE=normal"
    assert redact.redact_terminal_output(text, command) == text


@pytest.mark.parametrize("command", ["ps auxww", "diagnostic-wrapper"])
def test_whole_option_opt_out_control(monkeypatch, command):
    text = shlex.join(["--setenv=DEMO_TOKEN=FictionalFirst FictionalSecond\nFictionalThird"])
    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)
    assert redact.redact_terminal_output(text, command) == text
    result = redact.redact_terminal_output(text, command, force=True)
    assert not any(part in result for part in (
        "FictionalFirst", "FictionalSecond", "FictionalThird",
    ))
