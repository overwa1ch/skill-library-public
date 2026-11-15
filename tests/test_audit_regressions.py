"""Reproduce transaction races and interrupted recovery with isolated fixtures."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import bootstrap
import configure
from library import Library, LibraryError, _CatalogWatchState


class AuditRegressionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.lib = Library(self.root / 'registry.json')
        self.lib.init()
        self.skill = self.root / 'skill' / 'SKILL.md'
        self.skill.parent.mkdir()
        self.skill.write_text('---\nname: audit-fixture\ndescription: Audit fixture.\n---\nBody\n')
        self.entry = self.lib.register(self.skill, 'fixture:audit')
        self.program = Path(configure.__file__).with_name('library.py')

    def plan(self, config, fmt='codex-toml'):
        return configure.make_plan(config, self.lib.path, sys.executable, self.program, fmt=fmt)

    def test_plan_rejects_registry_changed_while_loading_sources(self):
        config = self.root / 'config.toml'
        load = Library.load
        def concurrent_move(lib, sid, *args, **kwargs):
            lib.edit(sid, placement='global')
            return load(lib, sid, *args, **kwargs)
        with patch.object(Library, 'load', concurrent_move):
            with self.assertRaisesRegex(LibraryError, 'Registry changed during plan'):
                self.plan(config)
        self.assertFalse(config.exists())
        self.assertEqual(self.lib.read()['skills'][0]['placement'], 'global')

    def test_plan_rejects_mutation_between_snapshot_and_entry_read(self):
        validate = Library.validate
        changed = []
        def concurrent_move(state):
            validate(state)
            if not changed:
                changed.append(True)
                live = json.loads(self.lib.path.read_bytes())
                live['skills'][0]['placement'] = 'global'
                self.lib.path.write_text(json.dumps(live))
        with patch.object(Library, 'validate', staticmethod(concurrent_move)):
            with self.assertRaisesRegex(LibraryError, 'Registry changed during plan'):
                self.plan(self.root / 'config.toml')

    def test_toml_plan_distinguishes_missing_and_empty_config(self):
        for existed in (False, True):
            with self.subTest(existed=existed):
                config = self.root / 'config.toml'
                config.unlink(missing_ok=True)
                if existed:
                    config.touch()
                plan = self.plan(config)
                if existed:
                    config.unlink()
                else:
                    config.touch()
                with self.assertRaisesRegex(LibraryError, 'Configuration changed after plan'):
                    configure.apply(plan, self.root / 'transactions')
                self.assertEqual(config.exists(), not existed)

    def interrupt_final_journal(self, transaction):
        atomic = configure.atomic
        record_path = Path(transaction) / 'transaction.json'
        def interrupted(path, data, *args, **kwargs):
            if Path(path) == record_path and json.loads(data).get('status') == 'rolled_back':
                raise OSError('simulated final journal interruption')
            return atomic(path, data, *args, **kwargs)
        with patch.object(configure, 'atomic', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'journal interruption'):
                configure.rollback(transaction)

    def test_interrupted_rollback_retries_and_preserves_later_unrelated_edits(self):
        for fmt in configure.FORMATS:
            for existed in (False, True):
                with self.subTest(fmt=fmt, existed=existed):
                    config = self.root / ('config.json' if fmt == 'mcp-json' else 'config.toml')
                    config.unlink(missing_ok=True)
                    original = b'{"theme":"light"}\n' if fmt == 'mcp-json' else b'# original\n'
                    if existed:
                        config.write_bytes(original)
                    plan = self.plan(config, fmt)
                    transaction = configure.apply(plan, self.root / 'transactions')['transaction']
                    self.interrupt_final_journal(transaction)
                    self.assertEqual(config.exists(), existed)
                    if existed:
                        self.assertEqual(config.read_bytes(), original)
                    later = b'{"theme":"latest"}\n' if fmt == 'mcp-json' else b'# latest user edit\n'
                    config.write_bytes(later)
                    self.assertFalse(configure.rollback(transaction)['changed'])
                    self.assertEqual(config.read_bytes(), later)
                    self.assertEqual(configure.rollback(transaction)['status'], 'already_rolled_back')

    def test_rollback_checkpoint_recovers_interruption_before_target_write(self):
        for fmt in configure.FORMATS:
            with self.subTest(fmt=fmt):
                config = self.root / ('before.json' if fmt == 'mcp-json' else 'before.toml')
                original = b'{"theme":"light"}' if fmt == 'mcp-json' else b'# original\n'
                config.write_bytes(original)
                transaction = configure.apply(self.plan(config, fmt), self.root / 'transactions')['transaction']
                atomic = configure.atomic
                def interrupted(path, *args, **kwargs):
                    if Path(path) == config:
                        raise OSError('simulated target interruption')
                    return atomic(path, *args, **kwargs)
                with patch.object(configure, 'atomic', side_effect=interrupted):
                    with self.assertRaisesRegex(OSError, 'target interruption'):
                        configure.rollback(transaction)
                configure.rollback(transaction)
                self.assertEqual(config.read_bytes(), original)

    def test_legacy_transaction_recovers_after_byte_exact_restore(self):
        config = self.root / 'legacy.toml'
        config.write_bytes(b'# original\n')
        transaction = configure.apply(self.plan(config), self.root / 'transactions')['transaction']
        config.write_bytes(b'# original\n')  # Old rollback restored bytes before its final journal failed.
        self.assertFalse(configure.rollback(transaction)['changed'])

    def test_json_rollback_accepts_host_omitting_stdio_defaults(self):
        config = self.root / 'defaults.json'
        config.write_text('{"theme":"light"}')
        transaction = configure.apply(self.plan(config, 'mcp-json'), self.root / 'transactions')['transaction']
        doc = json.loads(config.read_bytes())
        server = doc['mcpServers'][configure.JSON_SERVER]
        del server['type']
        del server['env']
        doc['theme'] = 'latest'
        config.write_text(json.dumps(doc))
        configure.rollback(transaction)
        self.assertEqual(json.loads(config.read_bytes()), {'theme': 'latest'})

    def test_apply_preserves_empty_config_created_during_staging(self):
        config = self.root / 'created.toml'
        plan = self.plan(config)
        fsync = configure.os.fsync
        def concurrent_create(fd):
            fsync(fd)
            # Only the staged configuration lives alongside the config file.
            if list(config.parent.glob('.skill-library-*')):
                config.touch()
        with patch.object(configure.os, 'fsync', side_effect=concurrent_create):
            with self.assertRaisesRegex(LibraryError, 'changed during apply'):
                configure.apply(plan, self.root / 'transactions')
        self.assertEqual(config.read_bytes(), b'')

    def test_bootstrap_rechecks_target_after_staging_for_apply_and_rollback(self):
        for undo in (False, True):
            with self.subTest(undo=undo):
                target = self.root / 'AGENTS.md'
                target.write_text('# Original\n')
                plan = bootstrap.make_plan(target)
                if undo:
                    transaction = bootstrap.apply(plan, self.root / 'bootstrap-transactions')['transaction']
                fsync = bootstrap.os.fsync
                def concurrent_write(fd):
                    fsync(fd)
                    if list(target.parent.glob('.skill-library-*')):
                        target.write_text('# Concurrent user instructions\n')
                with patch.object(bootstrap.os, 'fsync', side_effect=concurrent_write):
                    with self.assertRaisesRegex(LibraryError, 'changed before replacement'):
                        if undo:
                            bootstrap.rollback(transaction)
                        else:
                            bootstrap.apply(plan, self.root / 'bootstrap-transactions')
                self.assertEqual(target.read_text(), '# Concurrent user instructions\n')
                self.assertEqual(list(target.parent.glob('.skill-library-*')), [])

    def test_invalid_registry_path_does_not_break_watcher_recovery(self):
        watcher = _CatalogWatchState(self.lib)
        healthy_stamp = watcher.stamp()
        original = self.lib.path.read_bytes()
        invalid = json.loads(original)
        invalid['skills'][0]['path'] = '/tmp/\x00/SKILL.md'
        self.lib.path.write_text(json.dumps(invalid))
        with self.assertRaisesRegex(LibraryError, 'Cannot resolve path'):
            self.lib.status()
        self.assertNotEqual(watcher.stamp(), healthy_stamp)
        self.assertEqual(watcher.paths, ())
        self.lib.path.write_bytes(original)
        watcher.stamp()
        self.assertEqual(watcher.paths, (self.skill,))
        self.assertEqual(self.lib.status()['available_implicit'], 1)


if __name__ == '__main__':
    unittest.main()
