"""Offline/local SQLite backup before replacing the process with a container.

Copies credentials only into the Git-ignored backup directory; never prints them.
The application must be stopped (and no task running) before switching writers.
"""
import json
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone


def backup(root):
    root = Path(root).resolve()
    state = root / '.govern'
    source = state / 'govern.sqlite'
    if not source.is_file() or not (state / 'config.json').is_file():
        raise ValueError('Existing SQLite and config.json required; refusing an empty migration')
    target = state / 'migration-backup' / ('sqlite-container-' + uuid.uuid4().hex)
    target.mkdir(parents=True, exist_ok=False)
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
        if original.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite integrity check failed')
        with closing(sqlite3.connect(target / 'govern.sqlite')) as copied:
            original.backup(copied)
    shutil.copy2(state / 'config.json', target / 'config.json')
    shutil.copytree(root / 'policies', target / 'policies')
    with closing(sqlite3.connect(target / 'govern.sqlite')) as conn:
        summary = {table: conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                   for table in ('tasks', 'failure_cases', 'evolution_candidates', 'runtime_artifacts', 'checkpoints')}
        summary['release_heads'] = conn.execute('SELECT candidate_id,generation FROM evolution_release_heads').fetchall()
    summary['created_at'] = datetime.now(timezone.utc).isoformat()
    (target / 'manifest.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'backup_directory': str(target), **summary}


if __name__ == '__main__':
    print(json.dumps(backup(Path(__file__).resolve().parents[2]), ensure_ascii=False))
