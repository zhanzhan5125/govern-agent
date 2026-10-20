"""Check syntax, imports, CLI entry points, resources and portable Markdown links.

Run from any directory: python /path/to/repo/scripts/maintenance/check_layout.py
Does not call model providers, start the application or modify business data.
"""
import ast
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    failures = []
    counts = {}
    sources = sorted(p for folder in ('evoagent', 'tests', 'scripts')
                     for p in (ROOT / folder).rglob('*.py'))
    for path in sources:
        try:
            ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
        except (SyntaxError, UnicodeError) as exc:
            failures.append(f'syntax: {path.relative_to(ROOT)}: {exc}')
    counts['python_sources'] = len(sources)

    modules = []
    for path in sorted((ROOT / 'evoagent').rglob('*.py')):
        if path.name == '__main__.py':
            continue
        parts = list(path.relative_to(ROOT).with_suffix('').parts)
        if parts[-1] == '__init__':
            parts.pop()
        name = '.'.join(parts)
        try:
            importlib.import_module(name)
            modules.append(name)
        except Exception as exc:
            failures.append(f'import: {name}: {type(exc).__name__}: {exc}')
    counts['imported_modules'] = len(modules)

    try:
        from evoagent.application.api import WEB_ROOT
        if Path(WEB_ROOT).resolve() != ROOT / 'web':
            failures.append('resource: application WEB_ROOT is not repository web/')
    except Exception as exc:
        failures.append(f'resource: cannot resolve WEB_ROOT: {exc}')
    for relative in ('web/index.html', 'web/app.js', 'web/app.css', 'web/login.css',
                     'data/benchmarks/pr_diff_100.jsonl', 'skills/security-review/SKILL.md'):
        if not (ROOT / relative).is_file():
            failures.append(f'resource: missing {relative}')

    commands = sorted(p for folder in ('benchmarks', 'evaluation')
                      for p in (ROOT / 'scripts' / folder).glob('*.py')
                      if p.name != '__init__.py')
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    for path in commands:
        result = subprocess.run([sys.executable, '-B', str(path), '--help'],
                                cwd=ROOT.parent, env=env, capture_output=True, text=True)
        if result.returncode:
            failures.append(f'entry: {path.relative_to(ROOT)}: {result.stderr[-1200:]}')
    counts['cli_help_checks'] = len(commands)

    # Fenced examples can contain illustrative non-repository links.
    link_pattern = re.compile(r'\]\((?:<([^>]+)>|([^\s)]+))(?:\s+"[^"]*")?\)')
    link_count = 0
    documents = [ROOT / 'README.md', *(ROOT / 'docs').rglob('*.md'),
                 ROOT / 'evolution-data/README.md']
    for path in documents:
        text = re.sub(r'```.*?```', '', path.read_text(encoding='utf-8'), flags=re.S)
        for match in link_pattern.finditer(text):
            target = match[1] or match[2]
            if target.startswith(('#', 'http://', 'https://', 'mailto:', 'app:')):
                continue
            link_count += 1
            target = unquote(target.split('#', 1)[0])
            if re.match(r'^[A-Za-z]:', target):
                failures.append(f'link: nonportable absolute path in {path.relative_to(ROOT)}')
                continue
            if not (path.parent / target).exists():
                failures.append(f'link: {path.relative_to(ROOT)} -> {target}')
    counts['local_document_links'] = link_count
    print(json.dumps({'passed': not failures, **counts, 'failures': failures},
                     ensure_ascii=True, indent=2))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
