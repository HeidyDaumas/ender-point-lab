"""Audit the staged tree and all reachable Git history without printing secrets."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {
    '.gitignore', '.gitattributes', 'README.md', 'requirements.txt',
    'printer.example.json', 'server.py', 'index.html', 'measurements.js',
    'test_panel.py', 'test_measurements.js', 'test_config.py',
    'scripts/audit_public_tree.py',
}
SYNTHETIC_UUID = '00000000-0000-4000-8000-000000000000'
RULES = {
    'private absolute home path': re.compile(r'/(?:Users|home)/[^\s/]+|[A-Za-z]:\\Users\\[^\s\\]+'),
    'private temporary or workspace path': re.compile(r'/private/(?:tmp|var)/|/[^\s]*\.' + 'codex/'),
    'credential token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|sk-(?:proj-)?[A-Za-z0-9_-]{30,})\b'),
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'embedded printer identifier': re.compile(r'\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b'),
    'email address': re.compile(r'\b[A-Za-z0-9.!#$%&*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
    'serial device path': re.compile(r'/dev/(?:cu\.|tty\.|ttyUSB|ttyACM)[^\s\"\']+'),
}


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)


def audit_text(name, blob, where):
    errors = []
    try:
        text = blob.decode('utf-8')
    except UnicodeDecodeError:
        return [f'{where}: unexpected binary data in {name}']
    for label, pattern in RULES.items():
        for match in pattern.finditer(text):
            value = match.group()
            if label == 'embedded printer identifier' and value == SYNTHETIC_UUID:
                continue
            if label == 'email address' and value.endswith('@users.noreply.github.com'):
                continue
            errors.append(f'{where}: {label} in {name}')
            break
    return errors


def main():
    errors, seen = [], set()
    staged = git('ls-files', '--stage', '-z').split(b'\0')
    entries = []
    for item in staged:
        if not item:
            continue
        spec, name = item.split(b'\t', 1)
        mode, object_id, stage = spec.decode().split()
        if stage != '0' or mode not in {'100644', '100755'}:
            errors.append('index: unresolved conflict or non-regular file')
        entries.append(('index', name.decode(), object_id))
    if not entries:
        errors.append('No staged source files to audit.')
    commits = git('rev-list', '--all').decode().splitlines()
    for commit in commits:
        for item in git('ls-tree', '-r', '-z', commit).split(b'\0'):
            if not item:
                continue
            spec, name = item.split(b'\t', 1)
            mode, kind, object_id = spec.decode().split()
            if kind != 'blob' or mode not in {'100644', '100755'}:
                errors.append(f'{commit[:12]}: non-regular tracked file')
            entries.append((commit[:12], name.decode(), object_id))
        author, committer = git('show', '-s', '--format=%ae%n%ce', commit).decode().strip().splitlines()
        if not all(email.endswith('@users.noreply.github.com') for email in (author, committer)):
            errors.append(f'{commit[:12]}: commit email is not a GitHub no-reply address')
    for where, name, object_id in entries:
        if name not in ALLOWED:
            errors.append(f'{where}: unexpected tracked file {name}')
        if object_id not in seen:
            seen.add(object_id)
            errors.extend(audit_text(name, git('cat-file', 'blob', object_id), where))
    if errors:
        print('\n'.join(sorted(set(errors))), file=sys.stderr)
        return 1
    print(f'Privacy audit passed: {len(staged)-1} staged files, {len(commits)} commits, {len(seen)} unique blobs.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
