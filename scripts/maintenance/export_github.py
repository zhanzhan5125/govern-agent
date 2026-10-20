"""Create a clean source copy without local environments, credentials or state.

The destination must not already exist. Nothing is pushed to a remote service.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_DIRS = ('evoagent', 'tests', 'scripts', 'docs', 'data', 'skills', 'web')
PUBLIC_FILES = ('README.md', 'requirements.txt', 'Dockerfile', 'docker-compose.yml',
                '.env.example', '.gitignore', '.dockerignore', 'evolution-data/README.md')
EXCLUDED_DIRS = {'.venv', 'venv', '__pycache__', '.git', '.idea', '.vscode',
                 'node_modules', '.pytest_cache', '.mypy_cache', '.ruff_cache'}


def allowed(path):
    if any(part in EXCLUDED_DIRS for part in path.parts):
        return False
    name = path.name.lower()
    if name == '.env' or (name.startswith('.env.') and name != '.env.example'):
        return False
    return not (path.suffix.lower() in {'.pyc', '.pyo', '.db', '.sqlite', '.sqlite3',
                                      '.log', '.pem', '.key', '.zip'}
                or '.db-' in name or '.sqlite3-' in name)


def write_manifest(destination):
    entries = []
    for path in sorted(destination.rglob('*')):
        if path.is_file() and path.name != 'EXPORT_MANIFEST.json':
            entries.append({'path': path.relative_to(destination).as_posix(),
                            'bytes': path.stat().st_size,
                            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest = {'format': 1, 'purpose': 'Clean source copy for a user-managed GitHub upload',
                'excluded': ['virtual environments', 'actual dotenv files', 'databases',
                             'private keys', 'caches', 'IDE settings', 'runtime outputs',
                             'external evaluation datasets'],
                'file_count': len(entries), 'files': entries}
    (destination / 'EXPORT_MANIFEST.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest


def write_archive(destination):
    archive = destination.with_suffix('.zip')
    if archive.exists():
        raise FileExistsError(f'Archive already exists: {archive}')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(destination.rglob('*')):
            if path.is_file():
                bundle.write(path, destination.name + '/' + path.relative_to(destination).as_posix())
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', nargs='?', default=str(ROOT.parent / (ROOT.name + '-GitHub')))
    parser.add_argument('--zip', action='store_true', help='Also create a sibling ZIP archive')
    args = parser.parse_args()
    destination = Path(args.destination).resolve()
    if destination == ROOT or destination.is_relative_to(ROOT) or ROOT.is_relative_to(destination):
        parser.error('destination must be outside the source repository and not an ancestor')
    if destination.exists():
        parser.error('destination already exists; choose a new directory')
    if args.zip and destination.with_suffix('.zip').exists():
        parser.error('archive already exists; choose a new destination')
    candidates = [ROOT / name for name in PUBLIC_FILES]
    candidates += [path for name in PUBLIC_DIRS for path in (ROOT / name).rglob('*')
                   if path.is_file() and allowed(path.relative_to(ROOT))]
    for path in candidates:
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            parser.error(f'refusing an external or symlinked file: {path.relative_to(ROOT)}')
        if not path.is_file():
            parser.error(f'missing public file: {path.relative_to(ROOT)}')
    destination.mkdir(parents=True)
    for source in candidates:
        target = destination / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    manifest = write_manifest(destination)
    result = {'destination': str(destination), 'files': manifest['file_count']}
    if args.zip:
        result['archive'] = str(write_archive(destination))
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
