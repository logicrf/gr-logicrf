#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file check_delivery.py
@brief Check English delivery text, commit messages and local Markdown links.
@details Read one Git revision and all its ancestors, never unrelated branches.
         Reject Chinese text, undecodable delivery blobs and broken local links.
         This is a packaging check, not a software or hardware qualification test.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 1.0.0
@date 2026-10-20
"""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess


def check(repository, revision):
    """Return delivery counts and explicit violations without modifying the repository."""
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repository), *args])
    chinese = re.compile('[\u3400-\u9fff\uf900-\ufaff]')
    names = git('ls-tree', '-rz', '--name-only', revision).decode().strip('\0').split('\0')
    names = [name for name in names if name]
    failures = []
    markdown = 0
    for name in names:
        if chinese.search(name):
            failures.append(dict(file=name, reason='non-English path'))
        try:
            value = git('show', revision+':'+name).decode('utf-8')
        except UnicodeDecodeError:
            failures.append(dict(file=name, reason='binary content needs a separate reviewed asset manifest'))
            continue
        if chinese.search(value):
            failures.append(dict(file=name, reason='Chinese text remains'))
        if name.endswith('.md'):
            markdown += 1
            for target in re.findall(r'\]\(([^\s)]+)(?:\s+[^)]*)?\)', value):
                if target.startswith(('#', 'http:', 'https:', 'mailto:')):
                    continue
                target = target.split('#', 1)[0].split('?', 1)[0]
                path = str(PurePosixPath(name).parent/target)
                normalized = str(Path(path))
                if target and normalized not in names:
                    # Resolve lexical parent components without reading the working tree.
                    parts = []
                    for part in PurePosixPath(path).parts:
                        if part == '..' and parts:
                            parts.pop()
                        elif part != '.':
                            parts.append(part)
                    if '/'.join(parts) not in names:
                        failures.append(dict(file=name, reason='missing local link', target=target))
    commits = git('rev-list', revision).decode().splitlines()
    for commit in commits:
        message = git('show', '-s', '--format=%B', commit).decode()
        if chinese.search(message):
            failures.append(dict(commit=commit, reason='Chinese commit message'))
    return dict(status='FAIL' if failures else 'PASS', revision=revision,
                files=len(names), markdown_files=markdown, commits=len(commits), violations=failures)


def main():
    """Print an auditable result and return nonzero for any delivery violation."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--repository', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--revision', default='HEAD')
    args = parser.parse_args()
    result = check(args.repository, args.revision)
    print(json.dumps(result, indent=2))
    return int(result['status'] != 'PASS')


if __name__ == '__main__':
    raise SystemExit(main())
# End of file: check_delivery.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
