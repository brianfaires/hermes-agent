"""Standalone stdlib behavioral suite: disposable Git and fake service, never systemctl.

Run directly with python -I -B tests/scripts/test_guarded_switch.py.
"""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/claude_release_switch/guarded_switch.py"
BASE = "a" * 32
EXECUTOR = "b" * 32
NEW = "c" * 32


def write_json(p, data):
    p.write_text(json.dumps(data))
    p.chmod(0o600)


def load_guard(p):
    spec = importlib.util.spec_from_file_location("guarded_switch", p)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FixtureService:
    """Persistent fake adapter, including crash BEFORE stop() returns/phase update."""
    def __init__(self, m, filename, sha):
        self.root = Path(m["state_dir"]).parent
        self.file = self.root / "service.json"

    def read(self):
        return json.loads(self.file.read_text())

    def write(self, data):
        write_json(self.file, data)

    def executor(self, recovering):
        s = self.read()
        if recovering and not s["fenced"]:
            raise RuntimeError("forward cgroup not fenced")
        return s["executor"]

    def gateway(self):
        return self.read()["gateway"]

    def pre_arm(self, current, gateway):
        return not (self.root / "busy").exists()

    def stop(self):
        s = self.read()
        s["actions"].append("stop")
        if s.get("stop_failure") == "no_effect":
            self.write(s)
            raise RuntimeError("stop failed without effect")
        s["gateway"].update(active="inactive", pid=0)
        self.write(s)
        fault = s.pop("stop_fault", "")
        if fault:
            self.write(s)
            if fault == "exit":
                os._exit(33)
            if fault == "sigkill":
                os.kill(os.getpid(), signal.SIGKILL)
            if fault == "partial_failure":
                raise RuntimeError("stop failed after partial effect")
            if fault == "deactivating":
                s["gateway"].update(active="deactivating", pid=123)
                self.write(s)
                raise RuntimeError("stop failed with baseline still deactivating")
            if fault == "wait":
                (self.root / "waiting").touch()
                time.sleep(30)
            if fault == "dirty":
                (self.root / "repo" / "app.py").write_text("dirty\n")

    def start(self):
        s = self.read()
        s["actions"].append("start")
        s.setdefault("smoke_at_start", []).append((self.root / "smokes").read_text().splitlines()[-1])
        fault = s.pop("start_fault", "")
        self.write(s)
        if fault == "failure":
            raise RuntimeError("start failed")
        s["gateway"] = {"active": "active", "pid": 321, "invocation": NEW}
        self.write(s)
        if fault == "sigkill":
            os.kill(os.getpid(), signal.SIGKILL)


def worker():
    guard = load_guard(Path(sys.argv[2]))
    try:
        result = guard.execute(sys.argv[3], sys.argv[4], sys.argv[5], FixtureService)
        print(result)
        return {"switched": 0, "not_armed": 0, "recovered": 2}.get(result, 1)
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="guarded-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.install = self.root / "install"
        self.install.mkdir(mode=0o700)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        (self.state / "lock").touch(mode=0o600)
        self.guard = self.install / "guarded_switch.py"
        shutil.copyfile(SCRIPT, self.guard)
        self.guard.chmod(0o600)
        self.module = load_guard(self.guard)
        self.environment = {"PATH": "/usr/bin:/bin", "HOME": str(self.root),
                            "HERMES_HOME": str(self.root / "home"), "LANG": "C.UTF-8",
                            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
        self.git("init", "-b", "good")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Offline fixture")
        (self.repo / "app.py").write_text("VALUE = 'good'\n")
        (self.repo / ".gitignore").write_text("ignored\n")
        self.git("add", ".")
        self.git("commit", "-m", "known good")
        good = self.spec("good")
        self.git("switch", "-c", "target")
        (self.repo / "app.py").write_text("VALUE = 'target'\n")
        self.git("commit", "-am", "target")
        target = self.spec("target")
        self.git("switch", "good")
        self.smoke = self.install / "smoke.py"
        self.smoke.write_text(
            "import pathlib, sys\n"
            "repo = pathlib.Path(sys.argv[1])\n"
            "sys.path.insert(0, str(repo))\n"
            "import app\n"
            "if (repo.parent / ('fail-' + app.VALUE)).exists(): raise RuntimeError('import/config failure')\n"
            "with (repo.parent / 'smokes').open('a') as log: log.write(app.VALUE + '\\n')\n"
        )
        self.smoke.chmod(0o600)
        self.m = {"repo": str(self.repo), "target": target, "known_good": good,
                  "service": "fixture-gateway.service", "baseline_invocation": BASE,
                  "executor": "fixture-executor.service", "state_dir": str(self.state),
                  "interpreter": str(Path(sys.executable).resolve()),
                  "smoke_argv": [str(Path(sys.executable).resolve()), "-I", "-S", "-B", str(self.smoke), str(self.repo)],
                  "smoke_sha256": hashlib.sha256(self.smoke.read_bytes()).hexdigest()}
        self.manifest = self.install / "manifest.json"
        self.freeze()
        self.service = self.root / "service.json"
        write_json(self.service, {"executor": EXECUTOR, "fenced": False, "actions": [],
                                 "gateway": {"active": "active", "pid": 123, "invocation": BASE}})

    def git(self, *args):
        p = subprocess.run(["/usr/bin/git", "-C", str(self.repo), *args],
                           env=self.environment, text=True, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def spec(self, branch):
        return {"branch": branch, "sha": self.git("rev-parse", "HEAD"),
                "tree": self.git("rev-parse", "HEAD^{tree}")}

    def freeze(self):
        write_json(self.manifest, self.m)
        self.sha = hashlib.sha256(self.manifest.read_bytes()).hexdigest()

    def configure(self, **kwargs):
        s = self.service_data()
        s.update(kwargs)
        write_json(self.service, s)

    def service_data(self):
        return json.loads(self.service.read_text())

    def transaction(self):
        return json.loads((self.state / "transaction.json").read_text())

    def argv(self, mode):
        return [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "worker",
                str(self.guard), mode, str(self.manifest), self.sha]

    def run_guard(self, mode="run", expected=0):
        p = subprocess.run(self.argv(mode), env=self.environment, capture_output=True, text=True, timeout=15)
        self.assertEqual(p.returncode, expected, p.stdout + p.stderr)
        return p

    def recover(self, expected=2):
        self.configure(fenced=True)
        return self.run_guard("recover", expected)

    def assert_good(self):
        self.assertEqual(self.git("branch", "--show-current"), "good")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.m["known_good"]["sha"])
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual(self.service_data()["gateway"]["active"], "active")
        self.assertEqual(self.transaction()["outcome"], "recovered")

    def test_success_missing_model_report_and_no_replay(self):
        (self.repo / "extra").write_text("preserve me")
        (self.repo / "ignored").write_text("preserve ignored")
        before = self.git("show-ref")
        self.run_guard()
        self.assertEqual(self.git("branch", "--show-current"), "target")
        self.assertEqual(self.git("show-ref"), before)
        self.assertEqual((self.repo / "extra").read_text(), "preserve me")
        self.assertEqual((self.repo / "ignored").read_text(), "preserve ignored")
        self.assertFalse((self.root / "result.md").exists())
        self.assertEqual(self.recover(expected=0).stdout.strip(), "switched")
        self.run_guard(expected=1)
        self.assertEqual(self.service_data()["actions"], ["stop", "start"])
        self.assertEqual((self.state / "transaction.json").stat().st_mode & 0o777, 0o600)

    def test_pre_stop_model_exit_does_nothing(self):
        self.assertEqual(self.recover(expected=0).stdout.strip(), "not_armed")
        self.assertEqual(self.service_data()["actions"], [])
        self.assertFalse((self.state / "transaction.json").exists())

    def test_dirty_preflight_has_no_mutation(self):
        (self.repo / "app.py").write_text("dirt")
        self.run_guard(expected=1)
        self.assertEqual(self.service_data()["actions"], [])
        self.assertFalse((self.state / "transaction.json").exists())
        self.assertEqual((self.repo / "app.py").read_text(), "dirt")

    def test_assume_unchanged_dirt_is_rejected(self):
        self.git("update-index", "--assume-unchanged", "app.py")
        self.test_dirty_preflight_has_no_mutation()

    def crlf_checkout(self, attributes="*.ps1 text eol=crlf\n"):
        for branch, key in (("target", "target"), ("good", "known_good")):
            self.git("switch", branch)
            (self.repo / ".gitattributes").write_text(attributes)
            (self.repo / "script.ps1").write_bytes(f"echo {branch}\necho done\n".encode())
            self.git("add", ".gitattributes", "script.ps1")
            self.git("commit", "-m", "EOL source fixture")
            self.m[key] = self.spec(branch)
        # Force an actual checkout transformation, not manually manufactured CRLF.
        self.git("switch", "target")
        self.git("switch", "good")
        self.freeze()
        self.assertEqual((self.repo / "script.ps1").read_bytes(), b"echo good\r\necho done\r\n")
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")

    def test_git_attributes_crlf_checkout_switch_and_recovery(self):
        self.crlf_checkout()
        (self.root / "fail-target").touch()
        self.run_guard(expected=2)
        self.assert_good()
        self.assertEqual(self.service_data()["smoke_at_start"], ["good"])
        self.assertEqual((self.repo / "script.ps1").read_bytes(), b"echo good\r\necho done\r\n")

    def test_git_attributes_crlf_checkout_forward(self):
        self.crlf_checkout()
        self.run_guard()
        self.assertEqual((self.repo / "script.ps1").read_bytes(), b"echo target\r\necho done\r\n")

    def test_git_autocrlf_checkout(self):
        self.git("config", "core.autocrlf", "true")
        self.crlf_checkout(attributes="*.ps1 text=auto\n")
        self.run_guard()

    def test_crlf_real_dirt_hidden_from_status_is_refused(self):
        self.crlf_checkout()
        self.git("update-index", "--assume-unchanged", "script.ps1")
        (self.repo / "script.ps1").write_bytes(b"echo altered\r\necho done\r\n")
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")
        self.run_guard(expected=1)
        self.assertEqual(self.service_data()["actions"], [])
        self.assertFalse((self.state / "transaction.json").exists())

    def test_binary_crlf_dirt_is_not_normalized(self):
        (self.repo / ".gitattributes").write_text("*.bin -text\n")
        binary = self.repo / "data.bin"
        binary.write_bytes(b"bytes\x00\n")
        self.git("add", ".")
        self.git("commit", "-m", "binary source fixture")
        self.m["known_good"] = self.spec("good")
        self.freeze()
        binary.write_bytes(b"bytes\x00\r\n")
        self.run_guard(expected=1)
        self.assertEqual(self.service_data()["actions"], [])

    def test_custom_filter_is_rejected_without_execution(self):
        self.crlf_checkout(attributes="*.ps1 text eol=crlf filter=bad\n")
        self.git("config", "filter.bad.clean", "touch " + str(self.root / "filter-ran"))
        self.run_guard(expected=1)
        self.assertFalse((self.root / "filter-ran").exists())
        self.assertEqual(self.service_data()["actions"], [])

    def test_ident_conversion_cannot_hide_changed_bytes(self):
        (self.repo / ".gitattributes").write_text("*.txt ident\n")
        source = self.repo / "id.txt"
        source.write_bytes(b"$Id$\n")
        self.git("add", ".")
        self.git("commit", "-m", "ident fixture")
        self.m["known_good"] = self.spec("good")
        self.freeze()
        source.write_bytes(b"$Id: changed $\n")
        p = self.run_guard(expected=1)
        self.assertIn("unsupported non-EOL source conversion", p.stderr)
        self.assertEqual(self.service_data()["actions"], [])

    def recovery_only(self, other_branch=False):
        if other_branch:
            self.git("switch", "target")
            self.m["current"] = self.m["target"]
        self.m["target"] = dict(self.m["known_good"])
        self.freeze()

    def test_same_target_good_recovery_only(self):
        self.recovery_only()
        self.assertEqual(self.run_guard(expected=2).stdout.strip(), "recovered")
        self.assert_good()
        self.assertEqual(self.recover().stdout.strip(), "recovered")
        self.assertEqual(self.service_data()["actions"], ["stop", "start"])

    def test_recovery_only_from_bound_current_branch(self):
        self.recovery_only(other_branch=True)
        before = self.git("show-ref")
        self.run_guard(expected=2)
        self.assert_good()
        self.assertEqual(self.git("show-ref"), before)
        self.assertEqual((self.root / "smokes").read_text().splitlines(), ["target", "good"])
        self.assertEqual(self.service_data()["smoke_at_start"], ["good"])

    def test_recovery_only_partial_stop_failure(self):
        self.recovery_only(other_branch=True)
        self.configure(stop_fault="partial_failure")
        self.run_guard(expected=2)
        self.assert_good()
        self.assertEqual(self.service_data()["smoke_at_start"], ["good"])

    def test_recovery_only_hook_after_stop_exit(self):
        self.recovery_only(other_branch=True)
        self.configure(stop_fault="exit")
        self.run_guard(expected=33)
        self.recover()
        self.assert_good()
        self.assertEqual(self.service_data()["smoke_at_start"], ["good"])

    def test_recovery_only_failed_good_smoke_never_starts(self):
        self.recovery_only(other_branch=True)
        (self.root / "fail-good").touch()
        self.run_guard(expected=1)
        self.assertEqual(self.service_data()["actions"], ["stop"])
        self.assertEqual(self.transaction()["phase"], "recovery_required")

    def test_recovery_only_unbound_current_refused(self):
        self.git("switch", "target")
        self.recovery_only()
        self.run_guard(expected=1)
        self.assertEqual(self.service_data()["actions"], [])

    def test_conflicting_same_branch_specs_refused(self):
        self.m["target"]["branch"] = "good"
        self.freeze()
        self.run_guard(expected=1)
        self.assertEqual(self.service_data()["actions"], [])

    def test_current_spec_is_not_allowed_for_forward_switch(self):
        self.m["current"] = dict(self.m["known_good"])
        self.freeze()
        self.run_guard(expected=1)
        self.assertEqual(self.service_data()["actions"], [])

    def test_preflight_import_failure(self):
        (self.root / "fail-good").touch()
        self.run_guard(expected=1)
        self.assertEqual(self.service_data()["actions"], [])
        self.assertFalse((self.state / "transaction.json").exists())

    def test_unsupported_target_is_refused_before_stop(self):
        self.git("switch", "target")
        (self.repo / "link").symlink_to("app.py")
        self.git("add", "link")
        self.git("commit", "-m", "unsupported target")
        self.m["target"] = self.spec("target")
        self.git("switch", "good")
        self.freeze()
        self.run_guard(expected=1)
        self.assertEqual(self.service_data()["actions"], [])
        self.assertFalse((self.state / "transaction.json").exists())

    def test_tracked_literal_names_are_not_shell_input(self):
        self.git("switch", "target")
        names = ["contributor+id@example.invalid", "'quoted name'", "résumé", "$(touch injected)"]
        for name in names:
            (self.repo / name).write_text("literal source bytes")
        self.git("add", ".")
        self.git("commit", "-m", "literal filenames")
        self.m["target"] = self.spec("target")
        self.git("switch", "good")
        self.freeze()
        self.run_guard()
        for name in names:
            self.assertEqual((self.repo / name).read_text(), "literal source bytes")
        self.assertFalse((self.repo / "injected").exists())

    def test_stale_baseline(self):
        self.configure(gateway={"active": "active", "pid": 456, "invocation": NEW})
        self.run_guard(expected=1)
        self.assertEqual(self.service_data()["actions"], [])

    def test_arm_covers_post_stop_exit_before_phase_update(self):
        self.configure(stop_fault="exit")
        self.run_guard(expected=33)
        self.assertEqual(self.transaction()["phase"], "armed")
        self.assertEqual(self.service_data()["gateway"]["active"], "inactive")
        self.run_guard("recover", expected=1)  # Lock alone is not fencing proof.
        self.recover()
        self.assert_good()

    def test_sigkill_then_hook(self):
        self.configure(stop_fault="sigkill")
        self.run_guard(expected=-signal.SIGKILL)
        self.recover()
        self.assert_good()

    def test_external_executor_kill_then_hook(self):
        self.configure(stop_fault="wait")
        p = subprocess.Popen(self.argv("run"), env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            end = time.monotonic() + 10
            while not (self.root / "waiting").exists():
                self.assertIsNone(p.poll(), "forward exited before stop")
                self.assertLess(time.monotonic(), end, "stop fixture not reached")
                time.sleep(0.02)
            p.kill()
            p.communicate(timeout=5)
            self.assertEqual(p.returncode, -signal.SIGKILL)
            self.recover()
            self.assert_good()
        finally:
            if p.poll() is None:
                p.kill()
            p.communicate(timeout=5)

    def test_observed_cap_after_switch_and_smoke_before_start(self):
        # Reproduce the recorded external-executor gap, with real Git/imports:
        # stopped, exact target switched, smoke passed, then cap error, no start.
        self.configure(stop_fault="exit")
        self.run_guard(expected=33)
        self.git("switch", "target")
        p = subprocess.run(self.m["smoke_argv"], env=self.environment, capture_output=True)
        self.assertEqual(p.returncode, 0)
        p = subprocess.run([sys.executable, "-I", "-c",
                            "import sys; sys.stderr.write('error_max_turns: Reached maximum number of turns (32), num_turns 33'); sys.exit(33)"],
                           env=self.environment, capture_output=True)
        self.assertEqual(p.returncode, 33)
        self.assertEqual(self.service_data()["actions"], ["stop"])
        self.recover()
        self.assert_good()

    def test_partial_stop_failure_recovers(self):
        self.configure(stop_fault="partial_failure")
        self.run_guard(expected=2)
        self.assert_good()

    def test_partial_stop_still_deactivating_never_starts(self):
        self.configure(stop_fault="deactivating")
        self.run_guard(expected=1)
        self.assertEqual(self.service_data()["actions"], ["stop"])
        self.assertEqual(self.transaction()["phase"], "recovery_required")

    def test_stop_failure_no_effect_bounded(self):
        self.configure(stop_failure="no_effect")
        self.run_guard(expected=1)
        self.assertEqual(self.transaction()["phase"], "recovery_required")
        self.recover(expected=1)
        self.assertEqual(self.service_data()["actions"], ["stop", "stop"])

    def test_target_import_failure_falls_back(self):
        (self.root / "fail-target").touch()
        self.run_guard(expected=2)
        self.assert_good()

    def test_target_start_failure_falls_back_once(self):
        self.configure(start_fault="failure")
        self.run_guard(expected=2)
        self.assert_good()
        self.assertEqual(self.service_data()["actions"], ["stop", "start", "start"])

    def test_switch_error_preserves_colliding_extra(self):
        self.git("switch", "target")
        (self.repo / "collision").write_text("target tracked")
        self.git("add", "collision")
        self.git("commit", "-m", "collision")
        self.m["target"] = self.spec("target")
        self.git("switch", "good")
        (self.repo / "collision").write_text("local extra")
        self.freeze()
        self.run_guard(expected=2)
        self.assert_good()
        self.assertEqual((self.repo / "collision").read_text(), "local extra")

    def test_switch_error_preserves_colliding_ignored_extra(self):
        self.git("switch", "target")
        (self.repo / "ignored").write_text("target tracked")
        self.git("add", "--force", "ignored")
        self.git("commit", "-m", "ignored collision")
        self.m["target"] = self.spec("target")
        self.git("switch", "good")
        (self.repo / "ignored").write_text("local ignored extra")
        self.freeze()
        self.run_guard(expected=2)
        self.assert_good()
        self.assertEqual((self.repo / "ignored").read_text(), "local ignored extra")

    def test_new_invocation_after_ambiguous_start_never_stopped(self):
        self.configure(start_fault="sigkill")
        self.run_guard(expected=-signal.SIGKILL)
        self.recover(expected=1)
        self.assertEqual(self.service_data()["actions"], ["stop", "start"])
        self.assertEqual(self.service_data()["gateway"]["invocation"], NEW)
        self.assertEqual(self.transaction()["phase"], "recovery_required")

    def test_recovery_wrong_executor_does_not_touch_state(self):
        self.configure(stop_fault="exit")
        self.run_guard(expected=33)
        before = (self.state / "transaction.json").read_bytes()
        self.configure(executor="d" * 32)
        self.recover(expected=1)
        self.assertEqual((self.state / "transaction.json").read_bytes(), before)
        self.assertEqual(self.service_data()["actions"], ["stop"])

    def test_recovery_dirty_never_starts_or_cleans(self):
        self.configure(stop_fault="exit")
        self.run_guard(expected=33)
        (self.repo / "app.py").write_text("preserve dirt")
        self.recover(expected=1)
        self.assertEqual(self.service_data()["actions"], ["stop"])
        self.assertEqual((self.repo / "app.py").read_text(), "preserve dirt")
        self.assertEqual(self.transaction()["phase"], "recovery_required")

    def test_recovery_ref_drift_never_starts(self):
        self.configure(stop_fault="exit")
        self.run_guard(expected=33)
        self.git("branch", "unexpected")
        self.recover(expected=1)
        self.assertEqual(self.service_data()["actions"], ["stop"])

    def test_recovery_git_config_drift_never_starts(self):
        self.configure(stop_fault="exit")
        self.run_guard(expected=33)
        self.git("config", "filter.bad.smudge", "/bin/false")
        self.recover(expected=1)
        self.assertEqual(self.service_data()["actions"], ["stop"])

    def test_unknown_head_never_started(self):
        self.configure(stop_fault="exit")
        self.run_guard(expected=33)
        self.git("switch", "--detach")
        self.recover(expected=1)
        self.assertEqual(self.service_data()["actions"], ["stop"])

    def test_failed_switch_dirty_source_is_preserved(self):
        self.configure(stop_fault="dirty")
        self.run_guard(expected=1)
        self.assertEqual(self.service_data()["actions"], ["stop"])
        self.assertEqual((self.repo / "app.py").read_text(), "dirty\n")

    def test_recovery_failed_import_never_starts(self):
        self.configure(stop_fault="exit")
        self.run_guard(expected=33)
        (self.root / "fail-good").touch()
        self.recover(expected=1)
        self.assertEqual(self.service_data()["actions"], ["stop"])
        self.assertEqual(self.transaction()["phase"], "recovery_required")

    def test_recovery_failed_start_is_not_retried_by_another_hook(self):
        self.configure(stop_fault="exit", start_fault="failure")
        self.run_guard(expected=33)
        self.recover(expected=1)
        self.assertEqual(self.transaction()["phase"], "recovery_required")
        self.recover(expected=1)
        self.assertEqual(self.service_data()["actions"], ["stop", "start"])

    def test_unknown_journal_phase_never_starts(self):
        self.configure(stop_fault="exit")
        self.run_guard(expected=33)
        state = self.transaction()
        state["phase"] = "unknown"
        write_json(self.state / "transaction.json", state)
        self.recover(expected=1)
        self.assertEqual(self.service_data()["actions"], ["stop"])

    def test_manifest_digest_and_private_file_guards(self):
        self.manifest.chmod(0o644)
        self.run_guard(expected=1)
        self.manifest.chmod(0o600)
        self.manifest.write_text(self.manifest.read_text() + "\n")
        self.run_guard(expected=1)
        self.assertEqual(self.service_data()["actions"], [])

    def test_symlink_state_is_not_written(self):
        victim = self.root / "victim"
        victim.write_text("untouched")
        (self.state / "transaction.json").symlink_to(victim)
        self.run_guard(expected=1)
        self.assertEqual(victim.read_text(), "untouched")
        self.assertEqual(self.service_data()["actions"], [])

    def test_lock_contention_is_not_success_or_completion(self):
        with (self.state / "lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            p = self.run_guard("recover", expected=1)
            self.assertIn("lock unavailable", p.stderr)
        self.assertFalse((self.state / "transaction.json").exists())

    def test_completed_receipt_is_verified_not_blindly_trusted(self):
        self.run_guard()
        self.configure(gateway={"active": "active", "pid": 999, "invocation": "d" * 32})
        self.recover(expected=1)
        self.assertEqual(self.service_data()["actions"], ["stop", "start"])

    def test_busy_arriving_during_preflight_defers_without_arm(self):
        self.smoke.write_text(self.smoke.read_text() +
                              "(repo.parent / 'busy').touch()\n")
        self.m["smoke_sha256"] = hashlib.sha256(self.smoke.read_bytes()).hexdigest()
        self.freeze()
        self.assertEqual(self.run_guard(expected=1).stdout.strip(), "deferred_busy_or_unknown")
        self.assertFalse((self.state / "transaction.json").exists())
        self.assertEqual(self.service_data()["actions"], [])
        self.assertEqual(self.service_data()["gateway"]["active"], "active")
        self.assertEqual(self.recover(expected=0).stdout.strip(), "not_armed")

    def test_timeout_defers_until_fence(self):
        # Exercise the timeout branch without a 60-second sleep. The real Git
        # preflight and durable arm run before this isolated adapter times out.
        class TimeoutService(FixtureService):
            def stop(self):
                super().stop()
                raise subprocess.TimeoutExpired(["fixture-stop"], 60)

        result = self.module.execute("run", str(self.manifest), self.sha, TimeoutService)
        self.assertEqual(result, "awaiting_fence")
        self.assertEqual(self.service_data()["actions"], ["stop"])
        self.run_guard("recover", expected=1)
        self.recover()
        self.assert_good()

    def test_systemd_fence_requires_exact_hook_and_no_surviving_processes(self):
        # Actual production validator, with kernel/systemd observations supplied
        # by disposable files. Never invoke systemctl, even for read-only show.
        adapter = self.module.Systemd(self.m, str(self.manifest), self.sha)
        expected = " ".join([self.m["interpreter"], "-I", "-S", "-B", str(self.guard),
                             "recover", str(self.manifest), "--sha256", self.sha])
        properties = {"Id": self.m["executor"], "InvocationID": EXECUTOR, "Transient": "yes", "KillMode": "control-group",
                      "SendSIGKILL": "yes", "Restart": "no", "ControlGroup": "/fixture",
                      "Type": "exec", "ExitType": "main", "RemainAfterExit": "no",
                      "FinalKillSignal": "9", "TimeoutStopFailureMode": "terminate",
                      "RuntimeMaxUSec": "15min", "RuntimeRandomizedExtraUSec": "0",
                      "TimeoutStopUSec": "5min",
                      "ExecStopPost": "{ path=" + self.m["interpreter"] + " ; argv[]=" + expected + " ; ignore_errors=no ; }",
                      "SubState": "stop-post", "MainPID": "0", "ControlPID": str(os.getpid())}
        proc = self.root / "self-cgroup"
        proc.write_text("0::/fixture\n")
        members = self.root / "cgroup.procs"
        members.write_text(str(os.getpid()))
        read_text = Path.read_text

        def read_observation(p, *args, **kwargs):
            if str(p) == "/proc/self/cgroup":
                return read_text(proc)
            if str(p) == "/sys/fs/cgroup/fixture/cgroup.procs":
                return read_text(members)
            raise AssertionError(f"unexpected kernel observation: {p}")

        with patch.object(adapter, "show", return_value=properties), \
             patch.dict(os.environ, {"INVOCATION_ID": EXECUTOR}), \
             patch.object(Path, "read_text", read_observation), \
             patch.object(Path, "glob", return_value=[]):
            self.assertEqual(adapter.executor(True), EXECUTOR)
            for key, bad in (("MainPID", "100"), ("ControlPID", "100"), ("SubState", "running"),
                             ("KillMode", "process"), ("SendSIGKILL", "no"), ("Transient", "no"),
                             ("Restart", "always"), ("InvocationID", NEW), ("ExecStopPost", ""),
                             ("Id", "other-executor.service")):
                with self.subTest(property=key):
                    original = properties[key]
                    properties[key] = bad
                    with self.assertRaises(self.module.Refused):
                        adapter.executor(True)
                    properties[key] = original
            # A different MainPID (the independent model parent) is valid.
            properties.update(SubState="running", MainPID="123456")
            self.assertEqual(adapter.executor(False), EXECUTOR)
            invalid = {
                "RuntimeMaxUSec": (None, "", "infinity", "0", "-1s", "NaN", "901s", "16min", "junk"),
                "TimeoutStopUSec": (None, "infinity", "0", "299s", "601s", "garbage"),
                "RuntimeRandomizedExtraUSec": (None, "infinity", "1s"),
                "ExitType": (None, "cgroup"), "Type": ("oneshot",),
                "RemainAfterExit": ("yes",), "FinalKillSignal": ("15",),
                "TimeoutStopFailureMode": ("abort",),
            }
            for key, values in invalid.items():
                original = properties[key]
                for bad in values:
                    with self.subTest(property=key, value=bad):
                        if bad is None:
                            properties.pop(key, None)
                        else:
                            properties[key] = bad
                        with self.assertRaises(self.module.Refused):
                            adapter.executor(False)
                properties[key] = original
            properties.update(SubState="stop-post", MainPID="0")
            members.write_text(f"{os.getpid()}\n999999\n")
            with self.assertRaisesRegex(self.module.Refused, "descendants still alive"):
                adapter.executor(True)

    def test_executor_rechecked_after_smoke_before_durable_arm(self):
        outer = self
        class ChangedExecutor(FixtureService):
            calls = 0
            def executor(self, recovering):
                self.calls += 1
                if self.calls == 2:
                    outer.assertTrue((outer.root / "smokes").exists())
                    raise outer.module.Refused("executor lifecycle changed")
                return super().executor(recovering)
        with self.assertRaisesRegex(self.module.Refused, "lifecycle changed"):
            self.module.execute("run", str(self.manifest), self.sha, ChangedExecutor)
        self.assertFalse((self.state / "transaction.json").exists())
        self.assertEqual(self.service_data()["actions"], [])

    def test_systemd_gateway_alias_is_refused(self):
        adapter = self.module.Systemd(self.m, str(self.manifest), self.sha)
        observed = {"Id": "other.service", "LoadState": "loaded", "ActiveState": "active",
                    "MainPID": "123", "InvocationID": BASE}
        with patch.object(adapter, "show", return_value=observed):
            with self.assertRaises(self.module.Refused):
                adapter.gateway()
            observed["Id"] = self.m["service"]
            self.assertEqual(adapter.gateway(), self.service_data()["gateway"])


# The canonical runner's live-system guard requires this explicit marker for
# real child signaling when psutil is unavailable. Only this test kills a child
# from the pytest process, using its own Popen handle. Keep stdlib runs independent
# of pytest and leave the guard enabled for every other test.
if "pytest" in sys.modules:
    GuardTests.test_external_executor_kill_then_hook = sys.modules["pytest"].mark.live_system_guard_bypass(
        GuardTests.test_external_executor_kill_then_hook)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        sys.exit(worker())
    unittest.main()
