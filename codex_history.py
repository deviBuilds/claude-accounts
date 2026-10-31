"""Non-destructive local history import into a shared Codex runtime home.

Copies local history only. Authentication, cloud conversations, automation jobs,
settings, and account entitlements are not migrated by this module.
"""
import contextlib
import fcntl
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import tempfile


_DATABASE = re.compile(r'(?:state|thread_history)_\d+\.sqlite')
_HISTORY_TABLES = ('thread_turns', 'thread_items', 'thread_realtime_items',
                   'thread_history_projection_state')


class _IncompatibleHistory(ValueError):
    pass


def _quoted(name):
    return '"'+name.replace('"', '""')+'"'


def _table_info(connection, table, schema='main'):
    return connection.execute(f'PRAGMA {_quoted(schema)}.table_info({_quoted(table)})').fetchall()


def _common_columns(source, destination, table, schema='main'):
    left = {row[1]: row for row in _table_info(source, table)}
    right = _table_info(destination, table, schema)
    if not left or not right:
        raise _IncompatibleHistory('Missing history table')
    common = [row[1] for row in right if row[1] in left
              and row[2].upper() == left[row[1]][2].upper()]
    # Never invent values for a required field or drop any part of a row key.
    if any((row[5] or (row[3] and row[4] is None)) and row[1] not in common for row in right):
        raise _IncompatibleHistory('Incompatible history schema')
    return common


def _rows(connection, table, columns, where='', values=(), schema='main'):
    names = ','.join(map(_quoted, columns))
    return connection.execute(f'SELECT {names} FROM {_quoted(schema)}.{_quoted(table)} {where}', values)


def _insert(connection, table, columns, row, schema='main'):
    names = ','.join(map(_quoted, columns))
    placeholders = ','.join('?' for _ in columns)
    connection.execute(f'INSERT INTO {_quoted(schema)}.{_quoted(table)} ({names}) VALUES ({placeholders})', row)


def _merge_state_database(snapshot, target, history_snapshots, destination_home, blocked_rollouts=()):
    """Add absent threads and their related rows in one rollback-safe transaction.

    Existing thread IDs, titles, history projections and project names are never
    updated. Attach the history stores so a failed thread import rolls back its
    metadata and history together. Source connections read staged online backups.
    """
    imported, conflict = 0, False
    added = set()
    with contextlib.ExitStack() as stack:
        source = stack.enter_context(contextlib.closing(sqlite3.connect(snapshot)))
        destination = stack.enter_context(contextlib.closing(sqlite3.connect(target, timeout=5)))
        columns = _common_columns(source, destination, 'threads')
        if 'id' not in columns or 'rollout_path' not in columns:
            raise _IncompatibleHistory('Incompatible thread schema')
        histories = []
        for index, (name, history_snapshot) in enumerate(history_snapshots):
            history_target = destination_home/name
            if history_target.is_symlink():
                raise _IncompatibleHistory('Unsafe history store')
            history_source = stack.enter_context(contextlib.closing(sqlite3.connect(history_snapshot)))
            if not history_target.exists():
                # Bootstrap compatible schema without transplanting any other
                # thread's content into an already-used runtime.
                empty = history_snapshot.with_name(history_snapshot.name+'.empty')
                with contextlib.closing(sqlite3.connect(empty)) as created:
                    history_source.backup(created)
                    for table in _HISTORY_TABLES:
                        if _table_info(created, table):
                            created.execute(f'DELETE FROM {_quoted(table)}')
                    created.commit()
                    created.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                empty.chmod(0o600)
                try:
                    os.link(empty, history_target)
                except FileExistsError:
                    pass
            schema = f'history_{index}'
            destination.execute(f'ATTACH DATABASE ? AS {_quoted(schema)}', (str(history_target),))
            tables = []
            for table in _HISTORY_TABLES:
                if _table_info(history_source, table):
                    history_columns = _common_columns(history_source, destination, table, schema)
                    if 'thread_id' not in history_columns:
                        raise _IncompatibleHistory('Incompatible thread history schema')
                    tables.append((table, history_columns))
            histories.append((schema, history_source, tables))
        destination.execute('PRAGMA foreign_keys=ON')
        destination.execute('BEGIN IMMEDIATE')
        try:
            for row in _rows(source, 'threads', columns):
                data = dict(zip(columns, row))
                thread_id = data['id']
                existing = _rows(destination, 'threads', columns, 'WHERE id=?', (thread_id,)).fetchone()
                if existing is not None:
                    conflict = conflict or tuple(row) != tuple(existing)
                    continue
                rollout = Path(data['rollout_path'])
                if (not rollout.is_relative_to(destination_home) or not rollout.is_file()
                        or rollout.is_symlink()
                        or any(p.is_symlink() for p in rollout.parents if p != destination_home.parent)):
                    conflict = True
                    continue
                if str(rollout.relative_to(destination_home)) in blocked_rollouts:
                    conflict = True
                    continue
                if data.get('history_mode') == 'paginated' and not histories:
                    conflict = True
                    continue
                destination.execute('SAVEPOINT import_thread')
                try:
                    for field, table in (('project_id', 'projects'), ('thread_section_id', 'thread_sections')):
                        if not data.get(field):
                            continue
                        related_columns = _common_columns(source, destination, table)
                        related = _rows(source, table, related_columns, 'WHERE id=?', (data[field],)).fetchone()
                        kept = _rows(destination, table, related_columns, 'WHERE id=?', (data[field],)).fetchone()
                        if kept is None:
                            if related is None:
                                raise _IncompatibleHistory('Missing thread relation')
                            _insert(destination, table, related_columns, related)
                        elif related is not None and tuple(kept) != tuple(related):
                            conflict = True
                    for schema, history_source, tables in histories:
                        for table, history_columns in tables:
                            if _rows(destination, table, ['thread_id'], 'WHERE thread_id=? LIMIT 1',
                                     (thread_id,), schema).fetchone():
                                raise _IncompatibleHistory('Existing thread history differs')
                            for history_row in _rows(history_source, table, history_columns,
                                                     'WHERE thread_id=?', (thread_id,)):
                                _insert(destination, table, history_columns, history_row, schema)
                    _insert(destination, 'threads', columns, row)
                    for table in ('thread_dynamic_tools', 'thread_attachments', 'thread_read_receipts'):
                        if not _table_info(source, table):
                            continue
                        related_columns = _common_columns(source, destination, table)
                        for related in _rows(source, table, related_columns, 'WHERE thread_id=?', (thread_id,)):
                            _insert(destination, table, related_columns, related)
                    destination.execute('RELEASE import_thread')
                    imported += 1
                    added.add(thread_id)
                except (_IncompatibleHistory, sqlite3.IntegrityError):
                    destination.execute('ROLLBACK TO import_thread')
                    destination.execute('RELEASE import_thread')
                    conflict = True
            if added and _table_info(source, 'thread_spawn_edges'):
                try:
                    edge_columns = _common_columns(source, destination, 'thread_spawn_edges')
                    for edge in _rows(source, 'thread_spawn_edges', edge_columns):
                        fields = dict(zip(edge_columns, edge))
                        if fields['child_thread_id'] not in added:
                            continue
                        if not _rows(destination, 'threads', ['id'], 'WHERE id=?', (fields['parent_thread_id'],)).fetchone():
                            conflict = True
                            continue
                        try:
                            _insert(destination, 'thread_spawn_edges', edge_columns, edge)
                        except sqlite3.IntegrityError:
                            conflict = True
                except _IncompatibleHistory:
                    conflict = True
            destination.commit()
        except BaseException:
            destination.rollback()
            raise
    return imported, conflict


def _copy_jsonl(source, destination):
    """Copy complete records from a bounded snapshot of an actively appended log."""
    with source.open('rb') as src, destination.open('wb') as dst:
        remaining = os.fstat(src.fileno()).st_size
        while remaining:
            block = src.read(min(1024*1024, remaining))
            if not block:
                break
            dst.write(block)
            remaining -= len(block)
    # A concurrent writer can leave a partial final record. Keep its complete
    # prefix; a subsequent import reports existing copies instead of overwriting.
    with destination.open('r+b') as dst:
        size = dst.seek(0, os.SEEK_END)
        position = size
        while position:
            start = max(0, position-65536)
            dst.seek(start)
            block = dst.read(position-start)
            index = block.rfind(b'\n')
            if index >= 0:
                dst.truncate(start+index+1)
                return
            position = start
        dst.truncate(0)


def _backup_database(source, destination, source_home, destination_home):
    with contextlib.closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True, timeout=5)) as src:
        with contextlib.closing(sqlite3.connect(destination)) as dst:
            src.backup(dst, pages=256, sleep=.01)
            if source.name.startswith('state_'):
                columns = {row[1] for row in dst.execute('PRAGMA table_info(threads)')}
                if 'rollout_path' in columns:
                    prefix = str(source_home)+'/'
                    for thread_id, rollout in dst.execute('SELECT id, rollout_path FROM threads').fetchall():
                        if isinstance(rollout, str) and rollout.startswith(prefix):
                            relative = Path(rollout[len(prefix):])
                            if '..' not in relative.parts:
                                dst.execute('UPDATE threads SET rollout_path=? WHERE id=?',
                                            (str(destination_home/relative), thread_id))
            dst.commit()
            dst.execute('PRAGMA wal_checkpoint(TRUNCATE)')


def _same_file(source, target):
    try:
        if source.stat().st_size != target.stat().st_size:
            return False
        digests = []
        for path in (source, target):
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024*1024), b''):
                    digest.update(block)
            digests.append(digest.digest())
        return digests[0] == digests[1]
    except OSError:
        return False


def import_history(source_home, destination_home):
    """Import missing local history without replacing existing runtime state.

    Call before starting the managed app server for the first time. SQLite's
    backup API reads a consistent source snapshot even when its WAL is active.
    Existing thread IDs and duplicate logs are preserved. New threads are added
    to compatible existing SQLite stores so database-only views can find them.
    The caller must hold the managed runtime's exclusive lease while importing.
    Returns counts and relative conflict paths, never content or credentials.
    """
    source_home = Path(source_home).expanduser().resolve()
    raw_destination = Path(destination_home).expanduser()
    if raw_destination.is_symlink():
        raise RuntimeError('The shared Codex history directory must not be a symlink.')
    destination_home = raw_destination.resolve()
    if source_home == destination_home:
        return {'copied': 0, 'existing': 0, 'skipped': 0, 'merged_threads': 0, 'conflicts': [], 'errors': []}
    if not source_home.is_dir():
        raise RuntimeError('The source Codex home does not exist.')
    destination_home.mkdir(parents=True, mode=0o700, exist_ok=True)
    lock = destination_home/'.history-import.lock'
    if lock.is_symlink():
        raise RuntimeError('The Codex history import lock must not be a symlink.')
    counts = {'copied': 0, 'existing': 0, 'skipped': 0, 'merged_threads': 0, 'conflicts': [], 'errors': []}
    with lock.open('a') as held:
        os.chmod(lock, 0o600)
        fcntl.flock(held, fcntl.LOCK_EX)
        sources = []
        for name in ('sessions', 'archived_sessions'):
            root = source_home/name
            if root.is_symlink():
                counts['skipped'] += 1
            elif root.is_dir():
                sources.extend(p for p in root.rglob('*.jsonl') if p.is_file())
        sources.extend(source_home/name for name in ('history.jsonl', 'session_index.jsonl')
                       if (source_home/name).is_file())
        databases = sorted(p for p in source_home.iterdir() if _DATABASE.fullmatch(p.name) and p.is_file())
        # Publish logs before their metadata database. No active managed writers
        # should exist on the initial import; exclusive links also avoid racing
        # with an unexpected creator and can never overwrite an existing file.
        with tempfile.TemporaryDirectory(prefix='.history-staging-', dir=destination_home) as temporary:
            snapshots = {}
            # Snapshot metadata first, projections second, then rollout files.
            # A live append can advance the imported log beyond its projection,
            # but must not publish a projection ahead of the imported log.
            for source in databases:
                if source.is_symlink():
                    continue
                snapshot = Path(temporary)/source.name
                try:
                    _backup_database(source, snapshot, source_home, destination_home)
                    snapshots[source.name] = snapshot
                except (OSError, sqlite3.Error):
                    counts['errors'].append({'path': source.name, 'reason': 'The source history snapshot could not be read.'})
            history_snapshots = [(name, path) for name, path in snapshots.items() if name.startswith('thread_history_')]
            merged_histories = False
            for source in [*sources, *databases]:
                relative = source.relative_to(source_home)
                target = destination_home/relative
                if source.is_symlink() or any(p.is_symlink() for p in target.parents if p != destination_home.parent):
                    counts['skipped'] += 1
                    continue
                if target.exists() or target.is_symlink():
                    counts['existing'] += 1
                    if (not target.is_symlink() and source.name.startswith('state_')
                            and source.name in snapshots and not counts['errors']):
                        try:
                            imported, conflict = _merge_state_database(snapshots[source.name], target,
                                                                       history_snapshots, destination_home,
                                                                       counts['conflicts'])
                            counts['merged_threads'] += imported
                            merged_histories = True
                            if conflict:
                                counts['conflicts'].append(str(relative))
                        except _IncompatibleHistory:
                            counts['conflicts'].append(str(relative))
                        except (OSError, sqlite3.Error):
                            counts['errors'].append({'path': str(relative), 'reason': 'New thread metadata could not be merged; existing threads were kept.'})
                    elif source.name.startswith('thread_history_') and merged_histories:
                        pass  # Merged only in the transaction for absent threads.
                    elif target.is_symlink() or _DATABASE.fullmatch(source.name) or not _same_file(source, target):
                        counts['conflicts'].append(str(relative))
                    continue
                target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                staging = Path(temporary)/'pending'
                try:
                    if _DATABASE.fullmatch(source.name):
                        if counts['errors']:
                            counts['errors'].append({'path': str(relative), 'reason': 'Metadata import deferred until history files can be copied.'})
                            continue
                        if source.name not in snapshots:
                            continue
                        staging = snapshots[source.name]
                    else:
                        _copy_jsonl(source, staging)
                    staging.chmod(0o600)
                    try:
                        os.link(staging, target)
                        counts['copied'] += 1
                    except FileExistsError:
                        counts['existing'] += 1
                        counts['conflicts'].append(str(relative))
                except (OSError, sqlite3.Error):
                    counts['errors'].append({'path': str(relative), 'reason': 'History could not be copied; existing files were kept.'})
                finally:
                    for path in (staging, Path(str(staging)+'-wal'), Path(str(staging)+'-shm')):
                        path.unlink(missing_ok=True)
    return counts
