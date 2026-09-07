"""Audited, read-only metadata boundary for offline research.

The safety scanner pins this module's exact AST. It exposes no general command
runner: changing a command or dependency requires another review and fingerprint.
"""


def read_provenance(root):
    import importlib.metadata
    import subprocess

    tracked = subprocess.check_output(
        ['git', '--no-optional-locks', 'ls-files', '-z'], cwd=root,
    ).decode('utf-8').split('\0')
    dirty = subprocess.check_output(
        ['git', '--no-optional-locks', 'status', '--porcelain', '--untracked-files=no'],
        cwd=root,
    ).decode('utf-8')
    if dirty:
        raise ValueError('commit tracked changes before recording an experiment')
    commit = subprocess.check_output(
        ['git', '--no-optional-locks', 'rev-parse', 'HEAD'], cwd=root,
    ).decode('utf-8').strip()
    dependencies = {
        package: importlib.metadata.version(package)
        for package in ('backtrader', 'pandas', 'numpy', 'pandas-ta', 'scipy', 'httpx')
    }
    return {'tracked': [name for name in tracked if name],
            'commit': commit, 'dependencies': dependencies}
