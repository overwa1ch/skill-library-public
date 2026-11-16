"""Explicit package boundaries and crash-recovery invariants for relocation."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import bootstrap
import library
from library import Library, LibraryError
import relocation
from relocation import Relocator


class RelocationRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.lib = Library(self.root / 'runtime/registry.json')
        self.lib.init()
        self.package = self.root / 'package'
        self.one = self.skill('one')
        self.two = self.skill('two', enabled=False)
        (self.package / 'shared.txt').write_text('shared resource\n')
        script = Path(self.one['path']).parent / 'read.py'
        script.write_text("from pathlib import Path\nprint((Path(__file__).resolve().parent.parent / 'shared.txt').read_text())\n")

    def skill(self, name, enabled=True):
        path = self.package / name / 'SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text('---\nname: ' + name + '\ndescription: Fixture.\n---\nBody\n')
        return self.lib.register(path, 'plugin:fixture/demo/skills/' + name + '/SKILL.md',
                                 scope='plugin', enabled=enabled, tags=['kept'])

    def relocate(self):
        return self.lib.relocate(self.one['id'], package_root=self.package)

    def record(self):
        directory, = (self.lib.path.parent / 'relocations').iterdir()
        return directory

    def assert_restored(self, directory):
        fresh = Library(self.lib.path)
        fresh.relocate_rollback(directory)
        self.assertEqual(fresh.load(self.one['id'])['path'], self.one['path'])
        self.assertEqual(fresh.entry(fresh.read(), self.two['id'])['path'], self.two['path'])
        self.assertTrue((self.package / 'shared.txt').exists())
        self.assertEqual(fresh.relocate_rollback(directory)['status'], 'already_rolled_back')

    def test_default_boundary_and_single_plugin_are_refused_before_writes(self):
        before = self.lib.path.read_bytes()
        with self.assertRaisesRegex(LibraryError, 'explicit boundary'):
            self.lib.relocate(self.one['id'])
        with self.assertRaisesRegex(LibraryError, 'complete --package-root'):
            self.lib.relocate(self.one['id'], self_contained=True)
        self.assertEqual(before, self.lib.path.read_bytes())
        self.assertFalse((self.lib.path.parent / 'relocations').exists())

    def test_package_preserves_parent_resources_and_disabled_identity(self):
        result = self.relocate()
        loaded = self.lib.load(self.one['id'])
        script = Path(loaded['skill_root']) / 'read.py'
        run = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('shared resource', run.stdout)
        disabled = self.lib.entry(self.lib.read(), self.two['id'])
        self.assertFalse(disabled['enabled'])
        self.assertEqual(disabled['tags'], ['kept'])
        self.assertEqual(result['skills'], 2)
        self.assert_restored(result['relocation'])

    def test_global_package_member_refused_before_copy(self):
        self.lib.edit(self.two['id'], placement='global')
        with self.assertRaisesRegex(LibraryError, 'global skill'):
            self.relocate()
        self.assertTrue(self.package.exists())
        self.assertFalse((self.lib.path.parent / 'relocations').exists())

    def test_every_relocation_journal_checkpoint_can_be_rolled_back(self):
        for status in ('publishing', 'published', 'rebound', 'applied'):
            with self.subTest(status=status):
                # Each subcase gets an independent package/registry.
                with tempfile.TemporaryDirectory() as temp:
                    case = RelocationRecoveryTests()
                    case.setUp()
                    try:
                        write = library.write_record
                        def fail(path, record):
                            if record.get('status') == status:
                                raise OSError('journal interruption')
                            return write(path, record)
                        with patch.object(library, 'write_record', side_effect=fail):
                            with self.assertRaisesRegex(LibraryError, 'journal interruption'):
                                case.relocate()
                        case.assert_restored(case.record())
                    finally:
                        case.doCleanups()

    def test_operation_success_before_checkpoint_failure_is_recoverable(self):
        for phase in ('publish', 'archive', 'restore', 'save_copy'):
            with self.subTest(phase=phase):
                case = RelocationRecoveryTests(); case.setUp()
                try:
                    original = Relocator.move
                    hit = []
                    def fail(relocator, source, target):
                        original(relocator, source, target)
                        selected = ((phase == 'publish' and source.name == 'staging') or
                                    (phase == 'archive' and target.parent.name == 'original') or
                                    (phase == 'restore' and source.parent.name == 'original') or
                                    (phase == 'save_copy' and target.name == 'library-copy'))
                        if selected and not hit:
                            hit.append(True)
                            raise OSError('operation succeeded before interruption')
                    if phase in ('restore', 'save_copy'):
                        moved = case.relocate()
                        with patch.object(Relocator, 'move', fail):
                            with self.assertRaises(OSError):
                                case.lib.relocate_rollback(moved['relocation'])
                    else:
                        with patch.object(Relocator, 'move', fail):
                            with self.assertRaisesRegex(LibraryError, 'operation succeeded'):
                                case.relocate()
                    self.assertTrue(hit)
                    case.assert_restored(case.record())
                finally:
                    case.doCleanups()

    def test_rollback_terminal_journal_failure_retries_preserving_user_edits(self):
        moved = self.relocate()
        write = library.write_record
        def fail(path, record):
            if record.get('status') == 'rolled_back':
                raise OSError('terminal interruption')
            return write(path, record)
        with patch.object(library, 'write_record', side_effect=fail):
            with self.assertRaises(OSError):
                self.lib.relocate_rollback(moved['relocation'])
        (self.package / 'shared.txt').write_text('new user content\n')
        self.lib.edit(self.one['id'], tags=['new-user-tag'])
        self.assert_restored(moved['relocation'])
        self.assertEqual((self.package / 'shared.txt').read_text(), 'new user content\n')
        self.assertEqual(self.lib.load(self.one['id'])['tags'], ['new-user-tag'])

    def test_partial_copy_recovery_retries_after_terminal_journal_failure(self):
        def interrupted(source, target, **kwargs):
            target.mkdir()
            (target / 'partial').write_text('partial')
            raise OSError('partial copy')
        with patch.object(relocation.shutil, 'copytree', side_effect=interrupted):
            with self.assertRaisesRegex(LibraryError, 'partial copy'):
                self.relocate()
        write = library.write_record
        def fail(path, record):
            if record.get('status') == 'rolled_back':
                raise OSError('terminal interruption')
            return write(path, record)
        with patch.object(library, 'write_record', side_effect=fail):
            with self.assertRaises(OSError):
                self.lib.relocate_rollback(self.record())
        self.assert_restored(self.record())

    def test_changed_mode_or_replaced_directory_refuses_rollback(self):
        moved = self.relocate()
        script = Path(self.lib.load(self.one['id'])['skill_root']) / 'read.py'
        script.chmod(script.stat().st_mode ^ 0o100)
        with self.assertRaisesRegex(LibraryError, 'directory changed'):
            self.lib.relocate_rollback(moved['relocation'])
        self.assertFalse(self.package.exists())

    def test_third_path_rebind_and_other_registry_are_rejected(self):
        moved = self.relocate()
        other = self.root / 'other/SKILL.md'
        other.parent.mkdir(); other.write_text(Path(self.lib.load(self.one['id'])['path']).read_text())
        self.lib.edit(self.one['id'], path=other)
        with self.assertRaisesRegex(LibraryError, 'membership changed|rebound concurrently'):
            self.lib.relocate_rollback(moved['relocation'])
        self.assertFalse(self.package.exists())
        different = Library(self.root / 'different/registry.json'); different.init()
        with self.assertRaisesRegex(LibraryError, 'belong to this registry'):
            different.relocate_rollback(moved['relocation'])

    def test_equivalent_bootstrap_path_selects_same_host(self):
        home = self.root / 'codex-home'; home.mkdir()
        target = home / 'AGENTS.md'; target.write_text(bootstrap.BLOCK)
        with patch.dict(os.environ, {'CODEX_HOME': str(home)}):
            alternate = home / '..' / home.name / 'AGENTS.md'
            self.assertEqual(bootstrap.default_host(target), bootstrap.default_host(alternate))
            self.assertEqual(bootstrap.make_plan(alternate, bootstrap.default_host(alternate))['before'], bootstrap.BLOCK)

    def test_registering_package_member_during_copy_refuses_relocation(self):
        unregistered = self.package / 'third/SKILL.md'
        unregistered.parent.mkdir()
        unregistered.write_text('---\nname: third\ndescription: Fixture.\n---\nBody\n')
        copy = relocation.shutil.copytree
        added = []
        def register_during_copy(*args, **kwargs):
            result = copy(*args, **kwargs)
            if Path(args[0]) == self.package and not added:
                added.append(self.lib.register(unregistered, 'fixture:third'))
            return result
        with patch.object(relocation.shutil, 'copytree', side_effect=register_during_copy):
            with self.assertRaisesRegex(LibraryError, 'membership changed'):
                self.relocate()
        self.assertTrue(self.package.exists())
        self.assertEqual(self.lib.load(added[0]['id'])['path'], str(unregistered))
        self.lib.relocate_rollback(self.record())
        self.assertEqual(self.lib.load(added[0]['id'])['path'], str(unregistered))

    def test_new_registration_inside_relocated_package_blocks_rollback(self):
        unregistered = self.package / 'third/SKILL.md'
        unregistered.parent.mkdir()
        unregistered.write_text('---\nname: third\ndescription: Fixture.\n---\nBody\n')
        result = self.relocate()
        new_path = Path(result['to']) / 'third/SKILL.md'
        added = self.lib.register(new_path, 'fixture:third')
        with self.assertRaisesRegex(LibraryError, 'membership changed'):
            self.lib.relocate_rollback(result['relocation'])
        self.assertFalse(self.package.exists())
        self.assertEqual(self.lib.load(added['id'])['path'], str(new_path))

    def test_legacy_interrupted_records_are_validated_and_recovered(self):
        for status in ('rebound', 'applied'):
            with self.subTest(status=status):
                source = self.root / ('standalone-' + status)
                source.mkdir()
                path = source / 'SKILL.md'
                path.write_text('---\nname: standalone\ndescription: Fixture.\n---\nBody\n')
                entry = self.lib.register(path, 'fixture:' + status)
                moved = self.lib.relocate(entry['id'], self_contained=True)
                directory = Path(moved['relocation'])
                record = json.loads((directory / 'relocation.json').read_bytes())
                manifest = {k: ('dir' if v.startswith('dir:') else v.split(':', 2)[2]
                               if v.startswith('file:') else v)
                            for k, v in record['manifest'].items() if k}
                legacy = {'version': 1, 'id': entry['id'], 'source': entry['source'],
                          'from': moved['from'], 'to': moved['to'], 'archive': moved['archive'],
                          'manifest': manifest, 'status': status}
                (directory / 'relocation.json').write_text(json.dumps(legacy))
                self.lib.relocate_rollback(directory)
                self.assertEqual(self.lib.load(entry['id'])['path'], str(path))
                self.assertEqual(json.loads((directory / 'relocation.json').read_bytes())['version'], 2)

    def test_equivalent_record_parent_alias_is_supported(self):
        moved = self.relocate()
        alias = self.root / 'record-parent-alias'
        alias.symlink_to(Path(moved['relocation']).parent, target_is_directory=True)
        self.assert_restored(alias / Path(moved['relocation']).name)

    def test_post_registration_source_alias_requires_explicit_rebind(self):
        source = self.root / 'standalone'
        source.mkdir()
        path = source / 'SKILL.md'
        path.write_text('---\nname: standalone\ndescription: Fixture.\n---\nBody\n')
        entry = self.lib.register(path, 'fixture:alias')
        renamed = self.root / 'renamed'
        source.rename(renamed)
        source.symlink_to(renamed, target_is_directory=True)
        with self.assertRaisesRegex(LibraryError, 'canonical path'):
            self.lib.relocate(entry['id'], self_contained=True)
        self.assertTrue(source.is_symlink())
        self.assertEqual(self.lib.load(entry['id'])['skill_root'], str(renamed))

    def test_rollback_succeeds_when_destination_was_occupied_during_copy(self):
        source = self.root / 'occupied-case'
        source.mkdir()
        path = source / 'SKILL.md'
        path.write_text('---\nname: occupied\ndescription: Fixture.\n---\nBody\n')
        entry = self.lib.register(path, 'fixture:occupied')
        copy = relocation.shutil.copytree
        target = self.lib.path.parent / 'sources/personal/occupied-case'

        def occupy_during_copy(*args, **kwargs):
            res = copy(*args, **kwargs)
            target.mkdir(parents=True)
            (target / 'SKILL.md').write_text('---\nname: unrelated\ndescription: Foreign.\n---\nForeign\n')
            return res

        with patch.object(relocation.shutil, 'copytree', side_effect=occupy_during_copy):
            with self.assertRaisesRegex(LibraryError, 'occupied'):
                self.lib.relocate(entry['id'], self_contained=True)
        reloc_dir = max((self.lib.path.parent / 'relocations').iterdir())
        outcome = self.lib.relocate_rollback(reloc_dir)
        self.assertEqual(outcome['status'], 'rolled_back')
        self.assertIn('Foreign', (target / 'SKILL.md').read_text())
        self.assertEqual(self.lib.load(entry['id'])['path'], str(path))


if __name__ == '__main__':
    unittest.main()
