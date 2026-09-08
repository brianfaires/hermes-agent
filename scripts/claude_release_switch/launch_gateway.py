#!/usr/bin/env python3
"""Installed gateway bootstrap with direct verified repository compilation.

Must run in final Python with -I -S -B -X pycache_prefix=/dev/null. Dependencies
and this installation are pinned externally; source/dependency holds are external.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile


GIT = "/usr/bin/git"
BASE_ENV = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
}


class LaunchRefusal(RuntimeError):
    pass


def require(ok: bool, message: str) -> None:
    if not ok:
        raise LaunchRefusal(message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def raw(argv: list[str], cwd: Path) -> str:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=BASE_ENV,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        text=True,
    )
    require(result.returncode == 0, "source selection command failed")
    require(len(result.stdout) < 4 * 1024 * 1024, "oversized source selection output")
    return result.stdout.strip()


def durable(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(tmp_fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def process_starttime() -> str:
    text = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="utf-8")
    return text[text.rindex(")") + 2 :].split()[19]


def tracked_inventory(repo: Path) -> list[dict[str, str]]:
    output = raw([GIT, "ls-files", "-z"], repo)
    files: list[dict[str, str]] = []
    for rel in sorted(name for name in output.split("\0") if name):
        path = repo / rel
        require(path.resolve().is_relative_to(repo), "tracked source path escapes")
        st = path.lstat()
        require(stat.S_ISREG(st.st_mode) and not path.is_symlink(), "only regular tracked source supported")
        mode = "100755" if st.st_mode & 0o111 else "100644"
        files.append({"path": rel, "sha256": digest(path.read_bytes()), "mode": mode})
    return files


def _inventory_by_path(files: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {item["path"]: item for item in files}


def dependency_files(roots, venv_config=""):
    """Closed inventory of dependency files; bytecode is never an input."""
    result = {}
    for root in roots:
        root = Path(root)
        require(root.is_absolute() and root.resolve() == root and root.is_dir(), 'dependency root path')
        for path in sorted(root.rglob('*')):
            if '__pycache__' in path.parts or path.suffix == '.pyc':
                continue
            if path.is_file():
                require(not path.is_symlink(), 'dependency symlink unsupported')
                result[str(path)] = digest(path.read_bytes())
    if venv_config:
        path = Path(venv_config)
        require(path.is_absolute() and path.resolve() == path and path.is_file(), 'venv config path')
        result[str(path)] = digest(path.read_bytes())
    return result


def isolate(repo, dependency_path, dependency_sha):
    # These flags must apply before even this script executes. In particular,
    # disabling user site after startup cannot undo a venv .pth/sitecustomize.
    require(sys.flags.isolated == 1 and sys.flags.no_site == 1, 'Python -I -S required')
    require(sys.flags.dont_write_bytecode == 1 and sys.pycache_prefix == '/dev/null'
            and stat.S_ISCHR(Path('/dev/null').stat().st_mode),
            'startup bytecode bypass required')
    require('sitecustomize' not in sys.modules and 'usercustomize' not in sys.modules,
            'startup customization loaded')
    data = dependency_path.read_bytes()
    require(digest(data) == dependency_sha, 'dependency manifest drift')
    dependencies = json.loads(data)
    require(set(dependencies) == {'paths', 'files', 'observer_sha256', 'venv_config'}, 'dependency manifest shape')
    paths = dependencies['paths']
    require(type(paths) is list and paths, 'reviewed dependency paths required')
    require(dependency_files(paths, dependencies['venv_config']) == dependencies['files'], 'dependency bytes drift')
    for path in sys.path:
        if Path(path).exists():
            require(any(Path(path).resolve().is_relative_to(Path(root)) for root in paths),
                    'standard library dependency not pinned')
    venv_config = dependencies['venv_config']
    selected_prefix = Path(sys.executable).parent.parent.resolve()
    if (selected_prefix / 'pyvenv.cfg').is_file():
        require(venv_config == str(selected_prefix / 'pyvenv.cfg'), 'selected venv config not pinned')
        sys.prefix = sys.exec_prefix = str(selected_prefix)
    else:
        require(venv_config == '', 'venv config does not match interpreter')
    # -S before Python 3.14 hides venv prefix detection. Keep the selected
    # venv executable; explicitly supply its reviewed site-packages, never
    # addsitedir() or site.main(), so editable-install .pth files do not run.
    sys.path = list(dict.fromkeys([str(repo)] + [p for p in sys.path if Path(p).exists()] + paths))
    for key in tuple(os.environ):
        if key.startswith('PYTHON'):
            del os.environ[key]
    sys.dont_write_bytecode = True
    importlib.invalidate_caches()
    return dependencies


class VerifiedSource:
    """Compile repository source directly, including dynamic/lazy file imports.

    Frozen external source/dependency hold is required. This is provenance,
    not a sandbox against code deliberately evading Python's import machinery.
    """
    def __init__(self, repo, files, dependencies=None):
        import importlib.machinery as machinery
        self.repo = repo
        self.expected = _inventory_by_path(files)
        self.dependencies = dict((dependencies or {}).get('files', {}))
        if dependencies is not None:
            self.dependencies[str(Path(__file__).resolve().with_name('runtime_observation.py'))] = dependencies['observer_sha256']
        require(not ({str(repo / rel) for rel in self.expected} & self.dependencies.keys()),
                'tracked source classified as dependency')
        self.loaded = {}
        self.modules = {}
        for module in list(sys.modules.values()):
            filename = getattr(module, '__file__', None)
            require(not filename or str(Path(filename).resolve()) in self.dependencies or not Path(filename).resolve().is_relative_to(repo),
                    'repository module predates verified loader')
        original_exec = machinery.SourceFileLoader.exec_module
        original_binary = machinery.ExtensionFileLoader.create_module
        owner = self

        def get_code(loader, fullname):
            path = Path(loader.path).resolve(strict=True)
            data = path.read_bytes()
            module_path = fullname.replace('.', '/')
            owned = next((rel for rel in (module_path + '.py', module_path + '/__init__.py')
                          if rel in owner.expected), None)
            require(owned is None or path == repo / owned, 'candidate-owned module resolved outside source')
            if str(path) in owner.dependencies:
                require(digest(data) == owner.dependencies[str(path)], 'dependency source drift')
            elif path.is_relative_to(repo):
                rel = path.relative_to(repo).as_posix()
                require(rel in owner.expected, 'untracked source import')
                require(digest(data) == owner.expected[rel]['sha256'], 'source bytes drift')
            else:
                raise LaunchRefusal('unreviewed import path')
            # Bypass timestamp/hash pyc in source AND dependencies.
            return compile(data, str(path), 'exec', dont_inherit=True)

        def exec_module(loader, module):
            original_exec(loader, module)
            path = Path(loader.path).resolve(strict=True)
            if path.is_relative_to(repo) and str(path) not in owner.dependencies:
                rel = path.relative_to(repo).as_posix()
                owner.modules[module.__name__] = module
                owner.loaded[module.__name__] = {'module': module.__name__, 'path': rel,
                                                'sha256': owner.expected[rel]['sha256']}

        def sourceless(loader, fullname):
            raise LaunchRefusal('bytecode-only import unsupported')

        def binary(loader, spec):
            # Native initialization happens in create_module, before exec_module.
            path = Path(loader.path).resolve(strict=True)
            require(str(path) in owner.dependencies, 'unreviewed native import')
            require(digest(path.read_bytes()) == owner.dependencies[str(path)], 'native dependency drift')
            return original_binary(loader, spec)

        machinery.SourceFileLoader.get_code = get_code
        machinery.SourceFileLoader.exec_module = exec_module
        machinery.SourcelessFileLoader.get_code = sourceless
        machinery.ExtensionFileLoader.create_module = binary

    def inventory(self):
        # Detect preloaded/replaced source modules, without mistaking hashing a
        # current __file__ for evidence of the code that was executed.
        for name, module in list(sys.modules.items()):
            filename = getattr(module, '__file__', None)
            if filename and Path(filename).resolve().is_relative_to(self.repo) and str(Path(filename).resolve()) not in self.dependencies:
                require(name in self.loaded and self.modules[name] is module, 'source module bypassed verified compilation')
        for name in ('hermes_cli.main', 'gateway.run'):
            require(name in self.loaded, 'gateway entrypoint not compiled')
        return sorted(self.loaded.values(), key=lambda item: item['module'])


def prove_source(repo: Path) -> tuple[str, str, list[dict[str, str]]]:
    require(repo.is_absolute() and repo.resolve() == repo and (repo / ".git").exists(), "canonical git repository required")
    sha = raw([GIT, "rev-parse", "--verify", "HEAD"], repo)
    tree = raw([GIT, "rev-parse", "HEAD^{tree}"], repo)
    require(raw([GIT, "write-tree"], repo) == tree, "index/tree mismatch")
    require(
        subprocess.run([GIT, "diff", "--quiet", "--no-ext-diff", "HEAD", "--"], cwd=repo, env=BASE_ENV).returncode == 0,
        "tracked working bytes differ from HEAD",
    )
    require(
        subprocess.run([GIT, "diff", "--cached", "--quiet", "--no-ext-diff", "HEAD", "--"], cwd=repo, env=BASE_ENV).returncode == 0,
        "staged bytes differ from HEAD",
    )
    return sha, tree, tracked_inventory(repo)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path, help="Canonical checkout to launch from")
    parser.add_argument("--startup-json", required=True, type=Path, help="Private startup evidence path")
    parser.add_argument("--dependencies", required=True, type=Path)
    parser.add_argument("--dependencies-sha256", required=True)
    parser.add_argument("argv", nargs=argparse.REMAINDER, help="Use -- then -m hermes_cli.main gateway run")
    args = parser.parse_args()

    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    try:
        repo = args.repo.resolve(strict=True)
        startup_json = args.startup_json.resolve(strict=False)
        require(startup_json.is_absolute() and not startup_json.is_relative_to(repo), "startup evidence must live outside source")
        require(argv == ["-m", "hermes_cli.main", "gateway", "run"], "exact gateway module argv required")
        os.chdir(repo)
        dependencies = isolate(repo, args.dependencies, args.dependencies_sha256)
        sha, _tree, files = prove_source(repo)
        verified = VerifiedSource(repo, files, dependencies)
        module = importlib.import_module("hermes_cli.main")
        gateway = importlib.import_module("gateway.run")
        observer_path = Path(__file__).resolve().with_name('runtime_observation.py')
        require(digest(observer_path.read_bytes()) == dependencies['observer_sha256'], 'observer bytes drift')
        spec = importlib.util.spec_from_file_location('release_runtime_observation', observer_path)
        observer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(observer)
        executable = Path(sys.executable).resolve(strict=True)
        evidence = {
            "pid": os.getpid(),
            "starttime": process_starttime(),
            "sha": sha,
            "source": str(repo),
            "bytes": files,
            "loaded": verified.inventory(),
            "executable_sha256": digest(executable.read_bytes()),
        }
        def attest():
            evidence['loaded'] = verified.inventory()
            durable(startup_json, evidence)
        observer.install(gateway, attest)
        attest()
        sys.argv = [str(Path(module.__file__).resolve(strict=True)), "gateway", "run"]
        module.main()
    except Exception as exc:
        reason = str(exc) if isinstance(exc, LaunchRefusal) else type(exc).__name__
        print("release launcher refused: " + reason, file=sys.stderr)
        return 2
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
