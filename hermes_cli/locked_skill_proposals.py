"""Single-existing-file proposal isolation, immutable revision and target qualification.

These primitives cannot mint approval or apply live writes. The native owner and
its authenticated Telegram handler own authorization; ordinary lock guards remain
unchanged. Git identity and full raw pre/postimages are bound, never fuzzy patches.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import uuid


class ProposalError(ValueError):
    """Fail-closed unsupported, drifted or unqualified proposal."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProposalError('duplicate JSON record')
        result[key] = value
    return result


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def regular(path):
    """Reject redirects in every existing component, not just the leaf."""
    path = Path(path).absolute()
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ProposalError('symlink target')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not info.st_mode & 0o444:
        raise ProposalError('readable single-link regular file required')
    return path.read_bytes()


def git(scope, *args, index_file=None):
    from tools.environments.local import served_profile_child_env
    child = served_profile_child_env(target_home=scope.get('profile_id', scope['home']), inherit_credentials=False)
    env = {key: value for key, value in child.items() if not key.startswith('GIT_')}
    env['GIT_OPTIONAL_LOCKS'] = '0'
    if index_file is not None:
        env['GIT_INDEX_FILE'] = str(index_file)
    command = ['git', '--no-replace-objects', '-C', scope['home'], '-c', 'core.fsmonitor=false',
               '-c', 'core.hooksPath=/dev/null']
    if scope.get('git_dir'):
        command += ['--git-dir=' + scope['git_dir'], '--work-tree=' + scope['home']]
    done = subprocess.run(command + list(args), env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=60, check=False)
    if done.returncode:
        raise ProposalError('Git qualification failed: ' + args[0])
    return done.stdout


def _owner(home):
    """Resolve an ordinary profile repo or the existing workspace ownership manifest."""
    home = Path(home).absolute()
    if str(home) != str(home.resolve()):
        raise ProposalError('redirected home')
    for parent in (home, *home.parents):
        manifest_path = parent / '.config-repo/workspace.json'
        if not manifest_path.exists():
            continue
        manifest = json.loads(regular(manifest_path), object_pairs_hook=_unique_object)
        root = (manifest_path.parent / manifest['root']).resolve()
        candidates = [spec for spec in manifest['repositories'].values()
                      if (root / spec['worktree']).resolve() == home and 'skills' in spec['paths']]
        if len(candidates) != 1:
            raise ProposalError('unique owning profile repository required')
        spec = candidates[0]
        roots = sorted({str((root / item[key]).resolve()) for item in manifest['repositories'].values()
                        for key in ('worktree', 'git_dir')})
        return {'home': str(home), 'git_dir': str((root / spec['git_dir']).resolve()),
                'repository': spec['repository'], 'owner': spec['owner'], 'live_roots': roots}
    scope = {'home': str(home)}
    if git(scope, 'rev-parse', '--show-toplevel').decode().strip() != str(home):
        raise ProposalError('profile home must be the owning worktree')
    # Ordinary repos bind to the common directory too; a remote is not required for prepare.
    return {**scope, 'repository': git(scope, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip(),
            'owner': str(home)}


def _skill_path(skill):
    if (not isinstance(skill, str) or not re.fullmatch(r'[a-z0-9][a-z0-9._/-]*', skill)
            or any(part in {'.', '..', ''} for part in skill.split('/'))):
        raise ProposalError('exact skills-relative package path required')
    return str(PurePosixPath('skills', skill, 'SKILL.md'))


def locked(home, skill):
    raw = regular(Path(home) / 'skills/.usage.json')
    usage = json.loads(raw, object_pairs_hook=_unique_object)
    record = usage.get(Path(skill).name) if isinstance(usage, dict) else None
    if (not isinstance(record, dict) or record.get('locked') is not True
            or type(record.get('pinned', False)) is not bool):
        raise ProposalError('strict readable locked=true record required')
    # Usage counters change during ordinary reads. Bind the target's protection
    # semantics, not telemetry belonging to this or any unrelated package.
    return canonical({'skill': skill, 'locked': True, 'pinned': record.get('pinned', False)}).encode()


def _blob(scope, revision, path):
    entry = git(scope, 'ls-tree', '-z', revision, '--', path).split(b'\0')
    if len(entry) != 2 or entry[1] != b'':
        raise ProposalError('one tracked regular file required')
    meta, name = entry[0].split(b'\t')
    mode, kind, oid = meta.decode().split()
    if name.decode() != path or kind != 'blob' or mode not in {'100644', '100755'}:
        raise ProposalError('one tracked regular file required')
    return mode, oid, git(scope, 'cat-file', 'blob', oid)


def qualify(home, skill):
    scope = _owner(home)
    path = _skill_path(skill)
    data = regular(Path(home) / path)
    lock = locked(home, skill)
    base = git(scope, 'rev-parse', 'HEAD').decode().strip()
    mode, blob, before = _blob(scope, base, path)
    if data != before or git(scope, 'diff', '--cached', '--name-only', '--', path).strip():
        raise ProposalError('live target/index differs from committed base')
    common = git(scope, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip()
    info = (Path(home) / path).stat()
    return {**scope, 'profile_id': scope['home'], 'skill': skill, 'path': path, 'base': base, 'mode': mode, 'pre_blob': blob,
            'pre_sha256': digest(data), 'common_dir': common, 'lock_sha256': digest(lock),
            'file_identity': [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)]}


def prepare(*, home, skill, destination):
    target = qualify(home, skill)
    if not Path(destination).is_absolute():
        raise ProposalError('absolute proposal destination required')
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink() or str(destination.parent) != str(destination.parent.resolve()):
        raise ProposalError('new non-redirected destination required')
    # Never create under a live home, repository metadata, or a registered worktree.
    roots = [Path(target['home']), Path(target['common_dir']), *map(Path, target.get('live_roots', []))]
    worktrees = git(target, 'worktree', 'list', '--porcelain').decode().splitlines()
    roots += [Path(line[9:]) for line in worktrees if line.startswith('worktree ')]
    if any(destination.is_relative_to(root) for root in roots):
        raise ProposalError('proposal must be outside live homes and worktrees')
    branch = 'locked-proposal/' + uuid.uuid4().hex
    git(target, 'worktree', 'add', '-b', branch, str(destination), target['base'])
    return {**target, 'version': 1, 'branch': branch, 'worktree': str(destination)}


def manifest_digest(manifest):
    return digest(canonical({key: value for key, value in manifest.items()
                             if key != 'manifest_sha256'}).encode())


def freeze(prepared):
    current = qualify(prepared['home'], prepared['skill'])
    if any(current[key] != prepared[key] for key in current):
        raise ProposalError('owning target changed; reconcile and prepare again')
    checkout = {'home': prepared['worktree'], 'profile_id': prepared['home']}
    common = git(checkout, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip()
    if common != current['common_dir'] or git(checkout, 'status', '--porcelain', '--untracked-files=all', '--ignore-submodules=none').strip():
        raise ProposalError('clean owned proposal checkout required')
    head = git(checkout, 'rev-parse', 'HEAD').decode().strip()
    branch_head = git(current, 'rev-parse', 'refs/heads/' + prepared['branch']).decode().strip()
    if head != branch_head or git(checkout, 'rev-list', '--parents', '-n', '1', head).decode().split() != [head, current['base']]:
        raise ProposalError('one proposal commit directly on the frozen base required')
    paths = git(checkout, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--ignore-submodules=none', '--no-relative', '--name-status', '-z', current['base'], head).split(b'\0')
    if paths != [b'M', current['path'].encode(), b'']:
        raise ProposalError('full diff must modify exactly the allowed existing file')
    mode, blob, data = _blob(checkout, head, current['path'])
    if mode != current['mode'] or regular(Path(checkout['home']) / current['path']) != data:
        raise ProposalError('mode or checkout content changed')
    diff = git(checkout, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--ignore-submodules=none',
               '--no-relative', '--binary',
               '--full-index', current['base'], head, '--')
    _, _, preimage = _blob(current, current['base'], current['path'])
    manifest = {**prepared, 'head': head, 'post_blob': blob, 'post_sha256': digest(data),
                'preimage_hex': preimage.hex(), 'postimage_hex': data.hex(), 'full_diff_hex': diff.hex()}
    manifest['manifest_sha256'] = manifest_digest(manifest)
    return manifest


def verify_revision(manifest, *, reconciling=False, reconciled=False):
    """Revalidate the whole frozen revision without assuming the live file is still preimage."""
    if manifest_digest(manifest) != manifest['manifest_sha256']:
        raise ProposalError('manifest changed')
    owner = _owner(manifest['home'])
    if any(owner[key] != manifest[key] for key in owner):
        raise ProposalError('owner changed')
    allowed_heads = {manifest['head']} if reconciled else {manifest['base'], manifest['head']} if reconciling else {manifest['base']}
    if git(owner, 'rev-parse', 'HEAD').decode().strip() not in allowed_heads:
        raise ProposalError('live base changed')
    checkout = {'home': manifest['worktree'], 'profile_id': manifest['home']}
    if (git(checkout, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip() != manifest['common_dir']
            or git(checkout, 'rev-parse', 'HEAD').decode().strip() != manifest['head']
            or git(owner, 'rev-parse', 'refs/heads/' + manifest['branch']).decode().strip() != manifest['head']
            or git(checkout, 'status', '--porcelain', '--untracked-files=all', '--ignore-submodules=none').strip()):
        raise ProposalError('proposal revision changed')
    if reconciling:
        entry = git(owner, 'ls-files', '--stage', '-z', '--', manifest['path'])
        blobs = ('post_blob',) if reconciled else ('pre_blob', 'post_blob')
        allowed = {f"{manifest['mode']} {manifest[key]} 0\t{manifest['path']}\0".encode()
                   for key in blobs}
        if entry not in allowed:
            raise ProposalError('target index changed')
    elif git(owner, 'diff', '--cached', '--name-only', '--', manifest['path']).strip():
        raise ProposalError('target index changed')
    if digest(locked(manifest['home'], manifest['skill'])) != manifest['lock_sha256']:
        raise ProposalError('lock changed')
    diff = git(checkout, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--ignore-submodules=none',
               '--no-relative', '--binary',
               '--full-index', manifest['base'], manifest['head'], '--')
    if diff.hex() != manifest['full_diff_hex']:
        raise ProposalError('full diff changed')
    for revision, prefix in ((manifest['base'], 'pre'), (manifest['head'], 'post')):
        mode, blob, content = _blob(checkout, revision, manifest['path'])
        if (mode != manifest['mode'] or blob != manifest[prefix + '_blob']
                or content.hex() != manifest[prefix + 'image_hex']
                or digest(content) != manifest[prefix + '_sha256']):
            raise ProposalError('approved content changed')
