"""Equivalent JSON stdio spellings remain maintainable without weakening guards."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from configure import apply, make_plan
from library import Library, LibraryError


class JsonServerDefaultsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.registry = self.root / 'registry.json'
        lib = Library(self.registry)
        lib.init()
        skill = self.root / 'skill' / 'SKILL.md'
        skill.parent.mkdir()
        skill.write_text('---\nname: defaults-fixture\ndescription: JSON defaults fixture.\n---\nBody\n')
        lib.register(skill, 'fixture:defaults')
        self.program = Path(__file__).resolve().parents[1] / 'library.py'
        self.server = {'command': sys.executable,
                       'args': [str(self.program), '--registry', str(self.registry),
                                'serve', '--host', 'generic']}
        self.config = self.root / 'config.json'

    def write_config(self, server):
        self.config.write_text(json.dumps({'theme': 'kept', 'mcpServers': {
            'unrelated': {'url': 'https://example.test/mcp'}, 'skill-library': server}}, indent=2))

    def plan(self):
        return make_plan(self.config, self.registry, sys.executable, self.program, fmt='mcp-json')

    def test_omitted_default_keys_produce_noop_and_preserve_bytes(self):
        for defaults in ({}, {'type': 'stdio'}, {'env': {}}, {'type': 'stdio', 'env': {}}):
            with self.subTest(defaults=defaults):
                self.write_config({**self.server, **defaults})
                before = self.config.read_bytes()
                plan = self.plan()
                self.assertEqual(plan['changes'], [])
                self.assertEqual(plan['unchanged_server'], {**self.server, **defaults})
                self.assertFalse(apply(plan, self.root / 'transactions')['changed'])
                self.assertEqual(self.config.read_bytes(), before)

    def test_launch_changes_and_extra_fields_remain_conflicts(self):
        mutations = [
            {'command': '/different/python'},
            {'args': self.server['args'] + ['--unexpected']},
            {'type': 'http'},
            {'env': {'MODE': 'different'}},
            {'env': None},
            {'cwd': str(self.root)},
            {'extra': True},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.write_config({**self.server, **mutation})
                before = self.config.read_bytes()
                with self.assertRaisesRegex(LibraryError, 'differs'):
                    self.plan()
                self.assertEqual(self.config.read_bytes(), before)

    def test_noop_accepts_host_materializing_defaults_and_unrelated_edits(self):
        self.write_config(self.server)
        plan = self.plan()
        doc = json.loads(self.config.read_text())
        doc['theme'] = 'new-theme'
        doc['mcpServers']['skill-library'].update({'type': 'stdio', 'env': {}})
        self.config.write_text(json.dumps(doc))
        before = self.config.read_bytes()
        self.assertFalse(apply(plan, self.root / 'transactions')['changed'])
        self.assertEqual(self.config.read_bytes(), before)

    def test_noop_rejects_material_launch_change_after_plan(self):
        self.write_config(self.server)
        plan = self.plan()
        for mutation in ({'command': '/changed/python'}, {'env': {'MODE': 'new'}},
                         {'cwd': str(self.root)}, {'type': 'http'}):
            with self.subTest(mutation=mutation):
                server = copy.deepcopy(self.server)
                server.update(mutation)
                self.write_config(server)
                before = self.config.read_bytes()
                with self.assertRaisesRegex(LibraryError, 'conflict'):
                    apply(plan, self.root / 'transactions')
                self.assertEqual(self.config.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
