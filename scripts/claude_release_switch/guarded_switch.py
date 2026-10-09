#!/usr/bin/env python3
"""Single-use, source-only transaction. See guarded_switch.md before adoption.

The CLI has no fixture/service override. Tests inject an adapter through execute().
No model, ref updates, forced checkout, cleanup, installation or release acceptance.
"""
import argparse
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


class Refused(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise Refused(message)


def literal(value, pattern):
    require(isinstance(value, str) and re.fullmatch(pattern, value), "invalid literal")
    return value


def path(value, private=False, directory=False, literal_only=True):
    p = Path(literal(value, r"/[A-Za-z0-9_./-]+") if literal_only else value)
    require(str(p) == value and p.resolve(strict=True) == p, "noncanonical/symlink path")
    for part in (p, *p.parents):
        require(not part.is_symlink(), "symlink path")
    s = p.stat()
    require(stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode), "wrong file type")
    if private:
        require(s.st_uid == os.getuid() and stat.S_IMODE(s.st_mode) == (0o700 if directory else 0o600),
                "expected owner-private path")
        if not directory:
            require(s.st_nlink == 1, "hardlinked private file")
    return p


def private_read(p):
    path(str(p), private=True)
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        return f.read()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def env():
    # No inherited Git/Python controls or credentials. Smoke supplies its own
    # reviewed offline config parser; it receives disposable HOME/HERMES_HOME.
    return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}


def command(argv, cwd, environ=None):
    p = subprocess.run(argv, cwd=cwd, env=environ or env(), capture_output=True, timeout=60)
    require(p.returncode == 0, f"command failed ({p.returncode}): {argv[0]} {argv[1:3]}")
    return p.stdout.decode()


def git(repo, *args):
    return command(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                    "-c", "submodule.recurse=false", "-c", "core.untrackedCache=false",
                    "-C", str(repo), *args], str(repo))


def load_manifest(filename, sha):
    literal(sha, r"[0-9a-f]{64}")
    raw = private_read(Path(filename))
    require(digest(raw) == sha, "manifest digest changed")
    m = json.loads(raw)
    require(set(m) - {"current"} == {"repo", "target", "known_good", "service", "baseline_invocation",
                       "executor", "state_dir", "interpreter", "smoke_argv", "smoke_sha256"},
            "unexpected manifest fields")
    repo = path(m["repo"], directory=True)
    state = path(m["state_dir"], private=True, directory=True)
    path(str(Path(filename).parent), private=True, directory=True)
    for p in (state, Path(filename), Path(__file__).resolve()):
        require(not p.is_relative_to(repo), "guard/manifest/state must be outside checkout")
    path(str(Path(__file__).absolute()), private=True)
    path(str(Path(__file__).absolute().parent), private=True, directory=True)
    for spec in source_specs(m):
        require(set(spec) == {"branch", "sha", "tree"}, "invalid source specification")
        literal(spec["branch"], r"[A-Za-z0-9][A-Za-z0-9_/-]*")
        for field in ("sha", "tree"):
            literal(spec[field], r"[0-9a-f]{40}")
    require(m["target"]["branch"] != m["known_good"]["branch"] or
            m["target"] == m["known_good"], "conflicting specifications for same branch")
    if "current" in m:
        require(m["target"] == m["known_good"] and
                m["current"]["branch"] != m["target"]["branch"],
                "current is only supported for recovery-only from another branch")
    for key in ("service", "executor"):
        literal(m[key], r"[A-Za-z0-9][A-Za-z0-9_.@-]*\.service")
    require(m["service"] != m["executor"], "executor cannot be gateway")
    literal(m["baseline_invocation"], r"[0-9a-f]{32}")
    interpreter = path(m["interpreter"])
    require(os.access(interpreter, os.X_OK), "interpreter not executable")
    argv = m["smoke_argv"]
    require(isinstance(argv, list) and len(argv) == 6 and
            argv[:4] == [str(interpreter), "-I", "-S", "-B"] and argv[5] == str(repo),
            "smoke must be isolated Python script with repository argument")
    smoke = path(argv[4], private=True)
    require(not smoke.is_relative_to(repo), "smoke must be outside checkout")
    path(str(smoke.parent), private=True, directory=True)
    literal(m["smoke_sha256"], r"[0-9a-f]{64}")
    require(digest(private_read(smoke)) == m["smoke_sha256"], "smoke changed")
    return m


def duration(value):
    """Parse finite systemctl show timespans; refuse unknown/infinite output."""
    if value == "0":
        return Decimal(0)
    literal(value, r"(?:[0-9]+(?:\.[0-9]+)?(?:us|ms|s|min|h)(?: |$))+")
    units = {"us": Decimal("0.000001"), "ms": Decimal("0.001"),
             "s": Decimal(1), "min": Decimal(60), "h": Decimal(3600)}
    return sum(Decimal(n) * units[u] for n, u in
               re.findall(r"([0-9]+(?:\.[0-9]+)?)(us|ms|s|min|h)", value))


class Systemd:
    """Only exact user-service show/stop/start; never creates or changes a unit."""
    def __init__(self, m, filename, sha):
        self.m, self.filename, self.sha = m, filename, sha

    def ctl(self, verb, unit):
        e = env()
        # User bus addressing only; never import the calling model's environment.
        e["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
        return command(["/usr/bin/systemctl", "--user", verb, unit], "/", e)

    def show(self, unit):
        return dict(line.split("=", 1) for line in self.ctl("show", unit).splitlines() if "=" in line)

    def gateway(self):
        s = self.show(self.m["service"])
        require(s.get("LoadState") == "loaded" and s.get("Id") == self.m["service"],
                "gateway not loaded under exact service identity")
        return {"invocation": s.get("InvocationID", ""), "active": s.get("ActiveState"),
                "pid": int(s.get("MainPID", "0"))}

    def stop(self):
        self.ctl("stop", self.m["service"])

    def start(self):
        self.ctl("start", self.m["service"])

    def executor(self, recovering):
        s = self.show(self.m["executor"])
        require(s.get("Id") == self.m["executor"], "wrong executor service identity")
        invocation = literal(s.get("InvocationID"), r"[0-9a-f]{32}")
        require(invocation == os.environ.get("INVOCATION_ID"), "wrong executor invocation")
        require(s.get("Transient") == "yes" and s.get("KillMode") == "control-group" and
                s.get("SendSIGKILL") == "yes" and s.get("Restart") == "no", "unsafe executor lifecycle")
        require(s.get("Type") in ("exec", "simple") and s.get("ExitType") == "main" and
                s.get("RemainAfterExit") == "no" and s.get("FinalKillSignal") == "9" and
                s.get("TimeoutStopFailureMode") == "terminate", "unsafe executor termination")
        require(0 < duration(s.get("RuntimeMaxUSec")) <= 900,
                "executor runtime must be finite and at most 15 minutes")
        require(duration(s.get("RuntimeRandomizedExtraUSec")) == 0,
                "executor runtime must not be randomized")
        require(300 <= duration(s.get("TimeoutStopUSec")) <= 600,
                "executor stop/recovery budget must be 5 to 10 minutes")
        group = s.get("ControlGroup", "")
        require(group.startswith("/") and ".." not in group.split("/"), "invalid executor cgroup")
        require(f"0::{group}" in Path("/proc/self/cgroup").read_text().splitlines(), "outside executor cgroup")
        expected = " ".join([self.m["interpreter"], "-I", "-S", "-B", str(Path(__file__).resolve()),
                             "recover", self.filename, "--sha256", self.sha])
        hook = s.get("ExecStopPost", "")
        require(hook.count("argv[]=") == 1 and
                hook.startswith(f"{{ path={self.m['interpreter']} ; argv[]={expected} ; ignore_errors=no ;"),
                "exact mandatory ExecStopPost missing")
        if recovering:
            require(s.get("SubState") == "stop-post" and s.get("MainPID") == "0" and
                    s.get("ControlPID") == str(os.getpid()), "not fenced ExecStopPost")
            root = Path("/sys/fs/cgroup") / group.lstrip("/")
            files = [root / "cgroup.procs", *root.glob("**/*/cgroup.procs")]
            members = {int(pid) for f in files for pid in f.read_text().split()}
            require(members == {os.getpid()}, "executor descendants still alive")
        else:
            require(s.get("SubState") == "running" and int(s.get("MainPID", "0")) > 0,
                    "executor not running")
        return invocation


def identity(m):
    repo = path(m["repo"], directory=True)
    require(git(repo, "rev-parse", "--show-toplevel").strip() == str(repo), "wrong repository root")
    gd = path(git(repo, "rev-parse", "--absolute-git-dir").strip(), directory=True)
    return [[str(p), p.stat().st_dev, p.stat().st_ino] for p in (repo, gd)]


def source_specs(m):
    return [m[k] for k in ("known_good", "target", "current") if k in m]


def refs(m):
    repo = m["repo"]
    for s in source_specs(m):
        git(repo, "check-ref-format", "refs/heads/" + s["branch"])
        require(git(repo, "rev-parse", "refs/heads/" + s["branch"]).strip() == s["sha"], "branch drift")
        require(git(repo, "rev-parse", s["sha"] + "^{tree}").strip() == s["tree"], "tree mismatch")
    return git(repo, "for-each-ref", "--format=%(refname) %(objectname)")


def git_config(m):
    config = git(m["repo"], "config", "--list")
    require(not re.search(r"(?mi)^(filter\.|core\.(sparsecheckout|worktree)=)", config),
            "unsupported git filters/sparse/worktree config")
    return config


def source_entries(repo, spec):
    entries = []
    for record in git(repo, "ls-tree", "-rz", spec["sha"]).split("\0"):
        if not record:
            continue
        meta, name = record.split("\t", 1)
        mode, kind, sha = meta.split()
        require(mode in ("100644", "100755") and kind == "blob", "non-regular source unsupported")
        require(not name.startswith("/") and str(Path(name)) == name and
                not {"..", ".git"}.intersection(name.split("/")),
                "unsafe source path")
        entries.append((mode, sha, name))
    return entries


def source(m):
    repo = m["repo"]
    # hash-object below may apply built-in EOL conversion, never a custom driver.
    git_config(m)
    branch = git(repo, "symbolic-ref", "--short", "HEAD").strip()
    specs = [s for s in source_specs(m) if s["branch"] == branch]
    require(specs and all(s == specs[0] for s in specs), "unknown/ambiguous branch")
    s = specs[0]
    require(git(repo, "rev-parse", "HEAD").strip() == s["sha"], "unknown HEAD")
    # Byte hashes bypass assume-unchanged, sparse-index and stat-cache shortcuts.
    expected = []
    for mode, sha, name in source_entries(repo, s):
        # Tracked names may contain spaces, quotes, Unicode or '@' (contributors
        # and desktop assets do). They are filesystem data, never shell input.
        p = path(str(Path(repo) / name), literal_only=False)
        data = p.read_bytes()
        actual = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        if actual != sha:
            # Read/hash the real file, not Git's cached clean/stat result. Let
            # Git handle text/auto/binary and eol/core.autocrlf semantics, but
            # refuse every non-EOL conversion (including ident and encodings).
            attrs = git(repo, "check-attr", "-z", "filter", "ident", "working-tree-encoding",
                        "--", name).split("\0")
            require(all(v in ("unspecified", "unset") for v in attrs[2::3]),
                    "unsupported non-EOL source conversion")
            actual = git(repo, "hash-object", "--path=" + name, "--", name).strip()
        require(actual == sha, "dirty source")
        require(bool(p.stat().st_mode & 0o111) == (mode == "100755"), "source mode drift")
        expected.append(f"{mode} {sha} 0\t{name}")
    require(git(repo, "ls-files", "--stage", "-z").split("\0")[:-1] == expected, "dirty index")
    return s


def smoke(m):
    require(digest(private_read(Path(m["smoke_argv"][4]))) == m["smoke_sha256"], "smoke drift")
    with tempfile.TemporaryDirectory(prefix="guarded-smoke-") as home:
        e = env()
        e.update(HOME=home, HERMES_HOME=home, PYTHONDONTWRITEBYTECODE="1")
        command(m["smoke_argv"], m["repo"], e)


def atomic(state_dir, state):
    destination = state_dir / "transaction.json"
    if destination.exists() or destination.is_symlink():
        path(str(destination), private=True)
    fd, name = tempfile.mkstemp(prefix=".transaction-", dir=state_dir)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, destination)
        fd = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def active(g):
    return g["active"] == "active" and g["pid"] > 0


def stopped(g):
    return g["active"] in ("inactive", "failed") and g["pid"] == 0


class Transaction:
    def __init__(self, m, sha, service, state):
        self.m, self.sha, self.service, self.state = m, sha, service, state

    def save(self, phase, **values):
        self.state.update(values, phase=phase)
        atomic(Path(self.m["state_dir"]), self.state)

    def check(self):
        require(self.state["identity"] == identity(self.m), "repository identity changed")
        require(self.state["refs"] == refs(self.m), "ref drift")
        require(self.state["git_config"] == git_config(self.m), "git config drift")
        return source(self.m)

    def baseline(self):
        g = self.service.gateway()
        require(g["invocation"] in ("", self.m["baseline_invocation"]),
                "unrelated/new gateway invocation; no service action")
        return g

    def switch(self, spec):
        self.check()
        git(self.m["repo"], "switch", "--no-guess", "--no-overwrite-ignore", spec["branch"])
        require(self.check() == spec, "switch did not reach exact source")
        smoke(self.m)
        require(self.check() == spec, "source changed during smoke")

    def launch(self, spec, outcome):
        require(stopped(self.baseline()), "gateway not stopped before start")
        self.save("starting", intended=outcome)
        self.service.start()
        g = self.service.gateway()
        require(active(g) and re.fullmatch(r"[0-9a-f]{32}", g["invocation"] or "") and
                g["invocation"] != self.m["baseline_invocation"], "start not verified")
        require(self.check() == spec, "source changed during start")
        self.save("complete", outcome=outcome, gateway_invocation=g["invocation"])
        return outcome

    def recover(self):
        require(self.state["phase"] in {"armed", "stopped", "forward_failed", "awaiting_fence",
                                        "starting", "recovering", "complete", "recovery_required"},
                "unknown transaction phase")
        if self.state["phase"] == "complete":
            require(self.state["outcome"] in ("switched", "recovered"), "unknown completed outcome")
            spec = self.m["target" if self.state["outcome"] == "switched" else "known_good"]
            require(self.check() == spec, "completed source drift")
            g = self.service.gateway()
            require(active(g) and g["invocation"] == self.state["gateway_invocation"],
                    "completed gateway drift; no service action")
            return self.state["outcome"]
        require(self.state["phase"] != "recovery_required", "previous recovery failed; operator required")
        self.check()
        g = self.baseline()
        # The durable arm precedes stop. A failed/no-effect stop may leave the
        # original service running; stop ONLY that same invocation before restore.
        if not stopped(g):
            require(active(g) and g["invocation"] == self.m["baseline_invocation"], "unknown gateway state")
            self.save("recovering")
            self.service.stop()
        require(stopped(self.baseline()), "stop not verified")
        self.save("recovering")
        self.switch(self.m["known_good"])
        return self.launch(self.m["known_good"], "recovered")


def execute(mode, filename, sha, adapter=Systemd):
    m = load_manifest(filename, sha)
    service = adapter(m, filename, sha)
    directory = Path(m["state_dir"])
    saved = directory / "transaction.json"
    lock = directory / "lock"
    # Provision lock externally: preflight failure must not create any state.
    path(str(lock), private=True)
    fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("transaction lock unavailable; recovery NOT complete") from None
        state = json.loads(private_read(saved)) if saved.exists() or saved.is_symlink() else None
        if mode == "recover" and state is None:
            return "not_armed"
        invocation = service.executor(mode == "recover")
        if state is not None:
            require(state["manifest"] == sha and state["executor_invocation"] == invocation,
                    "stale/replayed manifest or executor")
            require(mode == "recover", "single-use transaction already armed")
        else:
            require(mode == "run", "unknown mode")
            ident, frozen_refs = identity(m), refs(m)
            # Git filters can run arbitrary commands during checkout; source-only
            # transactions intentionally refuse them, including LFS.
            config = git_config(m)
            for spec in source_specs(m):
                source_entries(m["repo"], spec)
            current = source(m)
            smoke(m)
            require(source(m) == current and refs(m) == frozen_refs and git_config(m) == config,
                    "preflight source/config changed")
            g = service.gateway()
            require(active(g) and g["invocation"] == m["baseline_invocation"], "stale baseline invocation")
            require(service.executor(False) == invocation,
                    "executor changed during preflight")
            state = {"manifest": sha, "executor_invocation": invocation, "identity": ident,
                     "refs": frozen_refs, "git_config": config, "phase": "armed"}
            atomic(directory, state)  # Durable BEFORE stop, even if stop fails/crashes.
        tx = Transaction(m, sha, service, state)
        try:
            if mode == "recover":
                return tx.recover()
            try:
                tx.baseline()
                service.stop()
                require(stopped(tx.baseline()), "stop not verified")
                tx.save("stopped")
                tx.switch(m["target"])
                outcome = "recovered" if m["target"] == m["known_good"] else "switched"
                return tx.launch(m["target"], outcome)
            except subprocess.TimeoutExpired:
                # run() reaps its direct child, but descendants may survive.
                # Only the fenced ExecStopPost may attempt recovery from here.
                tx.save("awaiting_fence", error="command timeout; executor must exit")
                return "awaiting_fence"
            except Exception as error:
                tx.save("forward_failed", error=str(error))
                return tx.recover()
        except Exception as error:
            tx.save("recovery_required", error=str(error))
            raise
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "recover"))
    parser.add_argument("manifest")
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    try:
        outcome = execute(args.mode, args.manifest, args.sha256)
        print(json.dumps({"transaction": outcome, "deployment_accepted": False}))
        return {"switched": 0, "not_armed": 0, "recovered": 2}.get(outcome, 1)
    except Exception as error:
        print(json.dumps({"transaction": "recovery_required_or_refused", "error": str(error),
                          "deployment_accepted": False}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
