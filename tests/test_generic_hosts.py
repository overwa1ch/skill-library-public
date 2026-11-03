"""Hosts other than Codex: Claude-style policy, generic MCP wording, JSON configs and CLAUDE.md."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import bootstrap
import configure
from configure import JSON_SERVER, apply, make_plan, rollback
from library import CODEX_INSTRUCTIONS, Library, LibraryError


ROOT = Path(__file__).resolve().parents[1]


def initialize(registry, *extra):
    """Run one initialize round trip and return the server instructions."""
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "generic-host-test", "version": "1"}}}
    out = subprocess.run([sys.executable, str(ROOT / "library.py"), "--registry", str(registry),
                          "serve", *extra], input=(json.dumps(request) + "\n").encode(),
                         capture_output=True, timeout=10, check=True).stdout
    return json.loads(out.decode().splitlines()[0])["result"]["instructions"]


class GenericHostTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.registry = self.root / "registry.json"
        self.lib = Library(self.registry)
        self.lib.init()
        self.path = self.root / "one" / "SKILL.md"
        self.path.parent.mkdir()
        self.write_skill("")
        self.entry = self.lib.register(self.path, "fixture:one")
        self.program = ROOT / "library.py"

    def write_skill(self, extra_frontmatter):
        self.path.write_text("---\nname: named-workflow\ndescription: Fixture workflow.\n"
                             + extra_frontmatter + "---\nBody\n")

    # Policy

    def test_claude_frontmatter_makes_skill_explicit_only(self):
        self.write_skill("disable-model-invocation: true\n")
        self.assertEqual([t["name"] for t in self.lib.tools()], ["load_explicit_skill"])
        self.assertFalse(self.lib.load_explicit("named-workflow")["allow_implicit_invocation"])

    def test_frontmatter_false_keeps_codex_policy_in_charge(self):
        self.write_skill("disable-model-invocation: false\n")
        self.assertEqual([t["name"] for t in self.lib.tools()], ["load_" + self.entry["id"]])
        policy = self.path.parent / "agents" / "openai.yaml"
        policy.parent.mkdir()
        policy.write_text("policy:\n  allow_implicit_invocation: false\n")
        self.assertEqual([t["name"] for t in self.lib.tools()], ["load_explicit_skill"])

    def test_non_boolean_frontmatter_policy_is_rejected(self):
        self.write_skill("disable-model-invocation: yes please\n")
        with self.assertRaisesRegex(LibraryError, "disable-model-invocation must be boolean"):
            self.lib.load(self.entry["id"])

    # Server instructions

    def test_codex_wording_is_the_default(self):
        self.assertEqual(initialize(self.registry), CODEX_INSTRUCTIONS)

    def test_generic_wording_has_no_codex_tool_names(self):
        implicit = initialize(self.registry, "--host", "generic")
        self.assertIn("load tools", implicit)
        self.assertIn("current", implicit)
        self.assertIn("only when the user names a skill", implicit)
        self.assertIn("If load_explicit_skill is available", implicit)
        self.assertNotIn("ALL_TOOLS", implicit)
        self.assertNotIn("tool_search", implicit)

    def test_generic_wording_survives_catalog_changes(self):
        # initialize instructions cannot be replaced by tools/list_changed.
        # They must cover implicit, explicit-only, empty and broken catalogs.
        implicit = initialize(self.registry, "--host", "generic")
        self.write_skill("disable-model-invocation: true\n")
        self.assertEqual(initialize(self.registry, "--host", "generic"), implicit)
        self.path.unlink()
        self.assertEqual(initialize(self.registry, "--host", "generic"), implicit)
        empty = self.root / "empty.json"
        Library(empty).init()
        self.assertEqual(initialize(empty, "--host", "generic"), implicit)
        self.registry.write_text("invalid registry")
        self.assertEqual(initialize(self.registry, "--host", "generic"), implicit)

    # JSON host configuration

    def json_plan(self, config):
        return make_plan(config, self.registry, sys.executable, self.program, fmt="mcp-json")

    def test_json_config_roundtrip_preserves_unrelated_keys(self):
        config = self.root / "claude.json"
        original = {"theme": "dark", "mcpServers": {"other": {"type": "http", "url": "https://example.test"}}}
        config.write_text(json.dumps(original, indent=2))
        plan = self.json_plan(config)
        self.assertEqual([c["kind"] for c in plan["changes"]], ["server"])
        server = plan["changes"][0]["after"]
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["args"][-3:], ["serve", "--host", "generic"])
        result = apply(plan, self.root / "transactions")
        data = json.loads(config.read_text())
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["mcpServers"]["other"], original["mcpServers"]["other"])
        self.assertEqual(data["mcpServers"][JSON_SERVER], server)
        self.assertFalse(config.read_text().endswith("\n"))
        self.assertEqual(self.json_plan(config)["changes"], [])
        rollback(result["transaction"])
        self.assertEqual(json.loads(config.read_text()), original)

    def test_json_apply_tolerates_host_rewrites_but_not_owned_conflicts(self):
        config = self.root / "claude.json"
        config.write_text(json.dumps({"numStartups": 1}))
        plan = self.json_plan(config)
        config.write_text(json.dumps({"numStartups": 2}))  # the host rewrote its file
        result = apply(plan, self.root / "transactions")
        self.assertEqual(json.loads(config.read_text())["numStartups"], 2)
        data = json.loads(config.read_text())
        data["numStartups"] = 3
        config.write_text(json.dumps(data))
        rollback(result["transaction"])
        self.assertEqual(json.loads(config.read_text()), {"numStartups": 3})

        plan = self.json_plan(config)
        config.write_text(json.dumps({"mcpServers": {JSON_SERVER: {"command": "someone-else"}}}))
        with self.assertRaisesRegex(LibraryError, "target conflict"):
            apply(plan, self.root / "transactions")

    def test_json_noop_checks_owned_server_and_accepts_unrelated_changes(self):
        config = self.root / "claude.json"
        apply(self.json_plan(config), self.root / "transactions")
        plan = self.json_plan(config)
        self.assertEqual(plan["changes"], [])
        installed = json.loads(config.read_text())
        installed["theme"] = "dark"
        installed["mcpServers"]["other"] = {"command": "other-service"}
        config.write_text(json.dumps(installed))
        before = config.read_bytes()
        self.assertEqual(apply(plan, self.root / "transactions"),
                         {"changed": False, "transaction": None})
        self.assertEqual(config.read_bytes(), before)
        for changed in ({"theme": "dark"},
                        {"mcpServers": {JSON_SERVER: {"command": "other-service"}}},
                        {"mcpServers": []}, "invalid JSON"):
            with self.subTest(changed=changed):
                config.write_text(changed if isinstance(changed, str) else json.dumps(changed))
                before = config.read_bytes()
                with self.assertRaises(LibraryError):
                    apply(plan, self.root / "transactions")
                self.assertEqual(config.read_bytes(), before)

    def test_json_noop_checks_config_after_input_verification(self):
        config = self.root / "claude.json"
        apply(self.json_plan(config), self.root / "transactions")
        plan = self.json_plan(config)
        verify = configure.verify_inputs

        def host_write(proposal):
            verify(proposal)
            config.write_text('{"theme":"dark"}')

        with patch.object(configure, "verify_inputs", side_effect=host_write):
            with self.assertRaisesRegex(LibraryError, "target conflict"):
                apply(plan, self.root / "transactions")
        self.assertEqual(json.loads(config.read_text()), {"theme": "dark"})

    def test_legacy_json_noop_uses_whole_file_checksum(self):
        config = self.root / "claude.json"
        apply(self.json_plan(config), self.root / "transactions")
        plan = self.json_plan(config)
        del plan["unchanged_server"]
        self.assertFalse(apply(plan, self.root / "transactions")["changed"])
        doc = json.loads(config.read_text())
        doc["theme"] = "dark"
        config.write_text(json.dumps(doc))
        with self.assertRaisesRegex(LibraryError, "Configuration changed"):
            apply(plan, self.root / "transactions")

    def test_json_apply_preserves_host_write_during_final_input_verification(self):
        config = self.root / "claude.json"
        config.write_text('{"theme":"light"}')
        plan = self.json_plan(config)
        verify = configure.verify_inputs
        calls = []

        def host_write(proposal):
            verify(proposal)
            calls.append(True)
            if len(calls) == 2:
                config.write_text('{"theme":"dark"}')

        transactions = self.root / "transactions"
        with patch.object(configure, "verify_inputs", side_effect=host_write):
            with self.assertRaisesRegex(LibraryError, "changed during apply"):
                apply(plan, transactions)
        self.assertEqual(json.loads(config.read_text()), {"theme": "dark"})
        record_path, = transactions.glob("*/transaction.json")
        self.assertEqual(json.loads(record_path.read_text())["status"], "prepared")
        self.assertEqual((record_path.parent / "before.json").read_bytes(), b'{"theme":"light"}')

    def test_json_apply_and_rollback_check_after_staging_output(self):
        config = self.root / "claude.json"
        config.write_text('{"theme":"light"}')
        transactions = self.root / "transactions"
        mkstemp, fsync = configure.tempfile.mkstemp, configure.os.fsync
        config_fds = set()
        host_bytes = b'{"theme":"host-change"}'

        def stage(*args, **kwargs):
            fd, name = mkstemp(*args, **kwargs)
            if Path(name).parent == config.parent:
                config_fds.add(fd)
            return fd, name

        def host_write(fd):
            fsync(fd)
            if fd in config_fds:
                config_fds.remove(fd)
                config.write_bytes(host_bytes)

        plan = self.json_plan(config)
        with patch.object(configure.tempfile, "mkstemp", side_effect=stage), \
                patch.object(configure.os, "fsync", side_effect=host_write):
            with self.assertRaisesRegex(LibraryError, "changed during apply"):
                apply(plan, transactions)
        self.assertEqual(config.read_bytes(), host_bytes)
        self.assertEqual(list(config.parent.glob(".skill-library-*")), [])

        result = apply(self.json_plan(config), transactions)
        with patch.object(configure.tempfile, "mkstemp", side_effect=stage), \
                patch.object(configure.os, "fsync", side_effect=host_write):
            with self.assertRaisesRegex(LibraryError, "changed during rollback"):
                rollback(result["transaction"])
        self.assertEqual(config.read_bytes(), host_bytes)
        self.assertEqual(list(config.parent.glob(".skill-library-*")), [])
        record = json.loads((Path(result["transaction"]) / "transaction.json").read_text())
        self.assertEqual(record["status"], "applied")

    def test_json_rollback_preserves_original_container_and_external_server_edits(self):
        config = self.root / "claude.json"
        cases = (({}, {}, {}),
                 ({"mcpServers": {}}, {}, {"mcpServers": {}}),
                 ({"mcpServers": {"deleted": {"command": "old"}}}, {}, {"mcpServers": {}}),
                 ({"mcpServers": {}}, {"added": {"command": "new"}},
                  {"mcpServers": {"added": {"command": "new"}}}))
        for original, other_servers, expected in cases:
            with self.subTest(original=original, other_servers=other_servers):
                config.write_text(json.dumps(dict(original, theme="light")))
                result = apply(self.json_plan(config), self.root / "transactions")
                installed = json.loads(config.read_text())["mcpServers"][JSON_SERVER]
                changed = {"theme": "dark", "mcpServers": dict(other_servers, **{JSON_SERVER: installed})}
                config.write_text(json.dumps(changed))
                rollback(result["transaction"])
                self.assertEqual(json.loads(config.read_text()), dict(expected, theme="dark"))

    def test_json_rollback_refuses_changed_owned_server(self):
        config = self.root / "claude.json"
        result = apply(self.json_plan(config), self.root / "transactions")
        config.write_text(json.dumps({"mcpServers": {JSON_SERVER: {"command": "external"}}}))
        before = config.read_bytes()
        with self.assertRaisesRegex(LibraryError, "Rollback conflict"):
            rollback(result["transaction"])
        self.assertEqual(config.read_bytes(), before)

    def test_json_ignores_unserved_global_sources_but_binds_whole_library(self):
        global_path = self.root / "global" / "SKILL.md"
        global_path.parent.mkdir()
        global_path.write_text("---\nname: global-fixture\ndescription: Global fixture.\n---\nBody\n")
        global_entry = self.lib.register(global_path, "fixture:global", scope="system")
        global_path.unlink()
        config = self.root / "claude.json"
        for ids in (None, [global_entry["id"]]):
            with self.subTest(ids=ids):
                plan = make_plan(config, self.registry, sys.executable, self.program, ids,
                                 fmt="mcp-json")
                self.assertEqual([s["id"] for s in plan["sources"]], [self.entry["id"]])
                self.assertTrue(apply(plan, self.root / "transactions")["changed"])
                config.unlink()
        with self.assertRaises(LibraryError):
            make_plan(self.root / "config.toml", self.registry, sys.executable, self.program)
        self.path.unlink()
        with self.assertRaises(LibraryError):
            self.json_plan(config)

    def test_json_refuses_to_overwrite_a_different_server(self):
        config = self.root / "claude.json"
        config.write_text(json.dumps({"mcpServers": {JSON_SERVER: {"command": "other"}}}))
        with self.assertRaisesRegex(LibraryError, "skill-library server differs"):
            self.json_plan(config)

    def test_missing_json_config_is_created_and_removed(self):
        config = self.root / "new" / "mcp.json"
        result = apply(self.json_plan(config), self.root / "transactions")
        self.assertIn(JSON_SERVER, json.loads(config.read_text())["mcpServers"])
        rollback(result["transaction"])
        self.assertFalse(config.exists())

    # Instructions file bootstrap

    def test_claude_md_bootstrap_uses_generic_rule(self):
        target = self.root / "CLAUDE.md"
        target.write_text("# Mine\n")
        plan = bootstrap.make_plan(target, "generic")
        result = bootstrap.apply(plan, self.root / "bootstrap-transactions")
        text = target.read_text()
        self.assertIn(bootstrap.GENERIC_BLOCK, text)
        self.assertNotIn("ALL_TOOLS", text)
        self.assertFalse(bootstrap.apply(bootstrap.make_plan(target, "generic"),
                                         self.root / "bootstrap-transactions")["changed"])
        with self.assertRaisesRegex(LibraryError, "owned rule differs"):
            bootstrap.make_plan(target, "codex")
        bootstrap.rollback(result["transaction"])
        self.assertEqual(target.read_text(), "# Mine\n")

    def test_bootstrap_still_rejects_other_file_names(self):
        with self.assertRaisesRegex(LibraryError, "Bootstrap requires a regular"):
            bootstrap.make_plan(self.root / "NOTES.md", "generic")


if __name__ == "__main__":
    unittest.main()
