"""Legacy registry aliases must never fork the central store on write."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from library import Library


class RegistryAliasTests(unittest.TestCase):
    def test_mutation_through_alias_preserves_link_and_updates_shared_store(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = root / 'central' / 'registry.json'
            canonical = Library(registry)
            canonical.init()
            alias = root / 'legacy.json'
            alias.symlink_to(registry)
            legacy = Library(alias)
            self.assertEqual(canonical.path, legacy.path)
            skill = root / 'skill' / 'SKILL.md'
            skill.parent.mkdir()
            skill.write_text('---\nname: alias-test\ndescription: A registry alias fixture.\n---\nBody\n')
            entry = legacy.register(skill, 'fixture:alias')
            self.assertTrue(alias.is_symlink())
            self.assertEqual(canonical.read()['skills'][0]['id'], entry['id'])
            canonical.edit(entry['id'], enabled=False)
            self.assertFalse(legacy.read()['skills'][0]['enabled'])
            self.assertTrue(alias.is_symlink())

    def test_parallel_cli_writers_share_store_through_alias(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = root / 'central.json'
            Library(registry).init()
            alias = root / 'legacy.json'
            alias.symlink_to(registry)
            program = Path(__file__).resolve().parents[1] / 'library.py'
            processes = []
            for index in range(8):
                skill = root / ('skill-' + str(index)) / 'SKILL.md'
                skill.parent.mkdir()
                skill.write_text('---\nname: parallel-fixture-' + str(index)
                                 + '\ndescription: Parallel registration fixture.\n---\nBody\n')
                target = alias if index % 2 else registry
                processes.append(subprocess.Popen(
                    [sys.executable, str(program), '--registry', str(target),
                     'register', str(skill), '--source', 'fixture:' + str(index)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            try:
                for process in processes:
                    stdout, stderr = process.communicate(timeout=15)
                    self.assertEqual(process.returncode, 0, stderr or stdout)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
            self.assertTrue(alias.is_symlink())
            self.assertEqual({s['source'] for s in Library(registry).read()['skills']},
                             {'fixture:' + str(i) for i in range(8)})


if __name__ == '__main__':
    unittest.main()
