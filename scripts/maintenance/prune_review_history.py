"""Explicit offline SQLite history cleanup. Stop the application and back up first.

Dry-run by default. Only tenant-owned Govern runs are removed; policy snapshots,
release history and unrelated tenants/tasks are never rewritten.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def prune(db, keep, tenant='default', apply=False, expected_deleted=None):
    keep = set(keep)
    if not keep:raise ValueError('Explicit keep IDs are required')
    path=Path(db).resolve()
    if not path.is_file():raise ValueError('Existing SQLite file required')
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE')
        try:
            rows=conn.execute('SELECT id,state,input_json FROM tasks WHERE tenant_id=?',(tenant,)).fetchall()
            owned={identifier:(state,json.loads(payload)) for identifier,state,payload in rows}
            if not keep.issubset(owned) or any(not owned[k][1].get('govern') for k in keep):
                raise ValueError('Keep IDs must all exist as tenant-owned Govern reviews')
            targets=sorted(k for k,(state,payload) in owned.items() if payload.get('govern') and k not in keep)
            if any(owned[k][0] not in {'SUCCESS','FAILED','CANCELLED'} for k in targets):
                raise ValueError('Refusing to remove active review tasks')
            if expected_deleted is not None and len(targets)!=expected_deleted:
                raise ValueError('Delete count changed; inspect again before applying')
            summary={'kept_ids':sorted(keep),'deleted_ids':targets,'deleted_count':len(targets),'applied':apply}
            if not apply or not targets:
                conn.rollback();return summary
            marks=','.join('?' for _ in targets)
            # The append-only guard is restored inside the same atomic transaction.
            # No candidate/audit trigger is disabled by this offline operation.
            guards=conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='runtime_journal'").fetchall()
            for name,_ in guards:conn.execute('DROP TRIGGER "'+name.replace('"','""')+'"')
            tables=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            summary['removed_rows']={}
            for table in tables:
                quoted='"'+table.replace('"','""')+'"'
                columns={r[1] for r in conn.execute('PRAGMA table_info('+quoted+')')}
                if 'task_id' not in columns or table=='agent_memories':continue
                summary['removed_rows'][table]=conn.execute('DELETE FROM '+quoted+' WHERE task_id IN ('+marks+')',targets).rowcount
            # Keep shared memory facts, detach only the removed review ownership.
            if 'agent_memories' in tables:
                conn.execute('UPDATE agent_memories SET task_id=\'\' WHERE task_id IN ('+marks+')',targets)
            conn.execute('DELETE FROM tasks WHERE id IN ('+marks+')',targets)
            for _,sql in guards:conn.execute(sql)
            if conn.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('Foreign key validation failed')
            if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Integrity validation failed')
            conn.commit();return summary
        except BaseException:
            conn.rollback();raise


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True)
    parser.add_argument('--keep',action='append',required=True)
    parser.add_argument('--expected-deleted',type=int)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    print(json.dumps(prune(args.db,args.keep,apply=args.apply,expected_deleted=args.expected_deleted),ensure_ascii=False))
