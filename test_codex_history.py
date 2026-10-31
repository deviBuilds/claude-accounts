import contextlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

import codex_history


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='codex-history-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root/'source'
        self.target = self.root/'runtime'
        self.source.mkdir()

    def test_import_wal_snapshot_rebases_rollouts_and_excludes_credentials(self):
        relative = Path('sessions/2026/10/31/thread.jsonl')
        rollout = self.source/relative
        rollout.parent.mkdir(parents=True)
        rollout.write_bytes(b'{"type":"session_meta"}\n{"incomplete":')
        (self.source/'auth.json').write_text('secret')
        (self.source/'config.toml').write_text('sensitive setting')
        (self.source/'history.jsonl').write_text('{"id":1}\n')
        db = sqlite3.connect(self.source/'state_5.sqlite')
        self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT)')
        db.execute('INSERT INTO threads VALUES (?,?)', ('one', str(rollout)))
        db.commit()
        summary = codex_history.import_history(self.source, self.target)
        self.assertEqual(summary['copied'], 3)
        self.assertEqual((self.target/relative).read_bytes(), b'{"type":"session_meta"}\n')
        with contextlib.closing(sqlite3.connect(self.target/'state_5.sqlite')) as copied:
            self.assertEqual(copied.execute('SELECT rollout_path FROM threads').fetchone()[0], str(self.target/relative))
        self.assertEqual(db.execute('SELECT rollout_path FROM threads').fetchone()[0], str(rollout))
        self.assertFalse((self.target/'auth.json').exists())
        self.assertFalse((self.target/'config.toml').exists())

    def test_existing_history_and_database_are_never_replaced(self):
        (self.source/'history.jsonl').write_text('{"source":1}\n')
        self.target.mkdir()
        (self.target/'history.jsonl').write_text('{"managed":2}\n')
        for directory, value in ((self.source, 'source'), (self.target, 'managed')):
            with contextlib.closing(sqlite3.connect(directory/'state_5.sqlite')) as db:
                db.execute('CREATE TABLE keep (value TEXT)')
                db.execute('INSERT INTO keep VALUES (?)', (value,))
                db.commit()
        summary = codex_history.import_history(self.source, self.target)
        self.assertEqual(summary['existing'], 2)
        self.assertEqual(set(summary['conflicts']), {'history.jsonl', 'state_5.sqlite'})
        self.assertEqual((self.target/'history.jsonl').read_text(), '{"managed":2}\n')
        with contextlib.closing(sqlite3.connect(self.target/'state_5.sqlite')) as db:
            self.assertEqual(db.execute('SELECT value FROM keep').fetchone()[0], 'managed')

    def test_symlink_source_and_destination_do_not_escape(self):
        outside = self.root/'outside'
        outside.mkdir()
        (outside/'secret.jsonl').write_text('secret\n')
        (self.source/'sessions').symlink_to(outside, target_is_directory=True)
        counts = codex_history.import_history(self.source, self.target)
        self.assertEqual(counts['skipped'], 1)
        self.assertFalse((self.target/'sessions').exists())
        alias = self.root/'alias'
        alias.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            codex_history.import_history(self.source, alias)
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ['secret.jsonl'])

    def create_thread_store(self, directory, rows):
        directory.mkdir(exist_ok=True)
        connection = sqlite3.connect(directory/'state_5.sqlite')
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.executescript('''
          CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL);
          CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL,
            title TEXT NOT NULL, history_mode TEXT NOT NULL DEFAULT 'legacy',
            project_id TEXT REFERENCES projects(id));
          CREATE TABLE thread_attachments (id TEXT PRIMARY KEY, thread_id TEXT REFERENCES threads(id), payload TEXT);
        ''')
        for thread_id, title, mode, project in rows:
            rollout = directory/'sessions'/(thread_id+'.jsonl')
            rollout.parent.mkdir(exist_ok=True)
            rollout.write_text('{"id":"'+thread_id+'"}\n')
            if project:
                connection.execute('INSERT OR IGNORE INTO projects VALUES (?,?)', (project, project))
            connection.execute('INSERT INTO threads VALUES (?,?,?,?,?)', (thread_id, str(rollout), title, mode, project))
        connection.commit()
        history = sqlite3.connect(directory/'thread_history_1.sqlite')
        self.addCleanup(history.close)
        history.execute('PRAGMA journal_mode=WAL')
        history.execute('CREATE TABLE thread_items (thread_id TEXT, item_id TEXT, item_json TEXT NOT NULL, PRIMARY KEY(thread_id,item_id))')
        for thread_id, title, _mode, _project in rows:
            history.execute('INSERT INTO thread_items VALUES (?,?,?)', (thread_id, 'item', title))
        history.commit()
        return connection, history

    def test_subsequent_import_adds_wal_threads_and_preserves_existing_titles(self):
        source, source_history = self.create_thread_store(self.source, [
            ('existing', 'Native title', 'paginated', None),
            ('new', 'New native thread', 'paginated', 'new-project')])
        target, target_history = self.create_thread_store(self.target, [
            ('existing', 'Managed title', 'paginated', None)])
        # Readers remain open while committed data lives in the source WAL.
        self.assertTrue(Path(str(self.source/'state_5.sqlite')+'-wal').exists())
        result = codex_history.import_history(self.source, self.target)
        self.assertEqual(result['merged_threads'], 1)
        self.assertFalse(result['errors'])
        self.assertIn('state_5.sqlite', result['conflicts'])
        self.assertEqual(target.execute('SELECT id,title FROM threads ORDER BY id').fetchall(),
                         [('existing', 'Managed title'), ('new', 'New native thread')])
        self.assertEqual(target.execute('SELECT rollout_path FROM threads WHERE id="new"').fetchone()[0],
                         str(self.target/'sessions/new.jsonl'))
        self.assertEqual(target.execute('SELECT name FROM projects').fetchone()[0], 'new-project')
        self.assertEqual(target_history.execute('SELECT thread_id,item_json FROM thread_items ORDER BY thread_id').fetchall(),
                         [('existing', 'Managed title'), ('new', 'New native thread')])
        self.assertEqual(source.execute('SELECT title FROM threads WHERE id="existing"').fetchone()[0], 'Native title')
        self.assertEqual(source_history.execute('SELECT COUNT(*) FROM thread_items').fetchone()[0], 2)
        self.assertEqual(codex_history.import_history(self.source, self.target)['merged_threads'], 0)

    def test_thread_import_rolls_back_metadata_and_history_on_related_row_conflict(self):
        source, _ = self.create_thread_store(self.source, [('new', 'Native', 'paginated', 'new-project')])
        target, target_history = self.create_thread_store(self.target, [('existing', 'Managed', 'paginated', None)])
        source.execute('INSERT INTO thread_attachments VALUES (?,?,?)', ('collision', 'new', 'new payload'))
        source.commit()
        target.execute('INSERT INTO thread_attachments VALUES (?,?,?)', ('collision', 'existing', 'kept payload'))
        target.commit()
        result = codex_history.import_history(self.source, self.target)
        self.assertEqual(result['merged_threads'], 0)
        self.assertIn('state_5.sqlite', result['conflicts'])
        self.assertEqual(target.execute('SELECT id FROM threads').fetchall(), [('existing',)])
        self.assertEqual(target.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 0)
        self.assertEqual(target_history.execute('SELECT thread_id FROM thread_items').fetchall(), [('existing',)])
        self.assertEqual(target.execute('SELECT payload FROM thread_attachments').fetchone()[0], 'kept payload')

    def test_missing_rollout_does_not_publish_new_thread_metadata(self):
        self.create_thread_store(self.source, [('new', 'Native', 'legacy', None)])
        target, _ = self.create_thread_store(self.target, [('existing', 'Managed', 'legacy', None)])
        (self.source/'sessions/new.jsonl').unlink()
        result = codex_history.import_history(self.source, self.target)
        self.assertEqual(result['merged_threads'], 0)
        self.assertIn('state_5.sqlite', result['conflicts'])
        self.assertEqual(target.execute('SELECT id FROM threads').fetchall(), [('existing',)])

    def test_conflicting_rollout_is_not_reassociated_with_new_metadata(self):
        self.create_thread_store(self.source, [('new', 'Native', 'legacy', None)])
        target, _ = self.create_thread_store(self.target, [('existing', 'Managed', 'legacy', None)])
        protected = self.target/'sessions/new.jsonl'
        protected.write_text('managed divergent content\n')
        result = codex_history.import_history(self.source, self.target)
        self.assertEqual(result['merged_threads'], 0)
        self.assertIn('sessions/new.jsonl', result['conflicts'])
        self.assertEqual(target.execute('SELECT id FROM threads').fetchall(), [('existing',)])
        self.assertEqual(protected.read_text(), 'managed divergent content\n')


if __name__ == '__main__':
    unittest.main()
