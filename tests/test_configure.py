import json
from pathlib import Path
import sys
import tempfile
import unittest
import tomlkit

from configure import make_plan, apply, rollback, SERVER
from library import Library, LibraryError


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.toml"
        self.original = b'# preserved\nmodel = "unchanged"\n[mcp_servers.existing]\nurl = "https://example.test/mcp"\n[plugins."existing@test"]\nenabled = true\n'
        self.config.write_bytes(self.original)
        self.registry = self.root / "registry.json"
        self.lib = Library(self.registry)
        self.lib.init()
        path = self.root / "SKILL.md"
        path.write_text("---\nname: example\ndescription: Example task.\n---\nExample\n")
        self.entry = self.lib.register(path, "test")
        self.program = Path(__file__).resolve().parents[1] / "library.py"

    def plan(self):
        return make_plan(self.config, self.registry, sys.executable, self.program)

    def apply(self):
        return apply(self.plan(), self.root / "transactions")

    def test_apply_only_owned_targets_and_byte_exact_rollback(self):
        p = self.plan()
        self.assertEqual(p["changes"][0]["after"]["command"], sys.executable)
        result = self.apply()
        new = tomlkit.parse(self.config.read_text()).unwrap()
        old = tomlkit.parse(self.original.decode()).unwrap()
        self.assertEqual(new["plugins"], old["plugins"])
        self.assertEqual(new["mcp_servers"]["existing"], old["mcp_servers"]["existing"])
        self.assertEqual(new["skills"]["config"][0]["enabled"], False)
        self.assertEqual(self.plan()["changes"], [])
        self.assertFalse(self.apply()["changed"])
        rollback(result["transaction"])
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(rollback(result["transaction"])["status"], "already_rolled_back")

    def test_preserves_unrelated_concurrent_edits(self):
        result = self.apply()
        doc = tomlkit.parse(self.config.read_text())
        doc["model"] = "new-external-model"
        doc["plugins"]["other@test"] = {"enabled": True}
        self.config.write_text(tomlkit.dumps(doc))
        rollback(result["transaction"])
        final = tomlkit.parse(self.config.read_text()).unwrap()
        self.assertEqual(final["model"], "new-external-model")
        self.assertTrue(final["plugins"]["other@test"]["enabled"])
        self.assertNotIn(SERVER, final["mcp_servers"])
        self.assertNotIn("skills", final)

    def test_conflicting_target_blocks_entire_rollback(self):
        result = self.apply()
        doc = tomlkit.parse(self.config.read_text())
        doc["skills"]["config"][0]["enabled"] = True
        self.config.write_text(tomlkit.dumps(doc))
        before = self.config.read_bytes()
        with self.assertRaisesRegex(LibraryError, "conflict"):
            rollback(result["transaction"])
        self.assertEqual(self.config.read_bytes(), before)

    def test_stale_plan_and_registry_refused(self):
        p = self.plan()
        self.config.write_bytes(self.original + b"\n# concurrent change\n")
        with self.assertRaisesRegex(LibraryError, "Configuration changed"):
            apply(p, self.root / "transactions")
        p = self.plan()
        self.lib.edit(self.entry["id"], tags=["changed"])
        with self.assertRaisesRegex(LibraryError, "Registry changed"):
            apply(p, self.root / "transactions")

    def test_existing_disabled_stays_disabled(self):
        doc = tomlkit.parse(self.original.decode())
        doc["skills"] = {"config": [{"path": self.entry["path"], "enabled": False}]}
        self.config.write_text(tomlkit.dumps(doc))
        self.lib.edit(self.entry["id"], enabled=False)
        p = self.plan()
        self.assertEqual(len(p["changes"]), 1)
        self.assertEqual(p["changes"][0]["kind"], "server")

    def test_conflicting_server_refused(self):
        doc = tomlkit.parse(self.original.decode())
        doc["mcp_servers"][SERVER] = {"command": "/unrelated/service"}
        self.config.write_text(tomlkit.dumps(doc))
        with self.assertRaisesRegex(LibraryError, "differs"):
            self.plan()

    def test_create_config_then_rollback(self):
        self.config.unlink()
        result = self.apply()
        self.assertTrue(self.config.exists())
        rollback(result["transaction"])
        self.assertFalse(self.config.exists())

    def test_legacy_codex_plan_and_transaction_keep_default_format(self):
        plan = self.plan()
        del plan["format"]
        del plan["server_name"]
        result = apply(plan, self.root / "transactions")
        record_path = Path(result["transaction"]) / "transaction.json"
        record = json.loads(record_path.read_text())
        for key in ("sources", "runtime", "unavailable_sources", "inactive_sources"):
            record.pop(key, None)
        record_path.write_text(json.dumps(record))
        doc = tomlkit.parse(self.config.read_text())
        doc["model"] = "external-model"
        self.config.write_text(tomlkit.dumps(doc))
        rollback(result["transaction"])
        final = tomlkit.parse(self.config.read_text()).unwrap()
        self.assertEqual(final["model"], "external-model")
        self.assertNotIn(SERVER, final["mcp_servers"])

    def test_global_system_not_disabled(self):
        self.lib.edit(self.entry["id"], placement="global")
        p = self.plan()
        self.assertEqual(len(p["changes"]), 1)
        self.assertEqual(p["changes"][0]["kind"], "server")

    def test_duplicate_native_config_is_rejected(self):
        doc = tomlkit.parse(self.original.decode())
        doc["skills"] = {"config": [{"path": self.entry["path"], "enabled": True},
                                    {"path": self.entry["path"], "enabled": False}]}
        self.config.write_text(tomlkit.dumps(doc))
        with self.assertRaisesRegex(LibraryError, "Duplicate"):
            self.plan()

    def test_subset_plan_checks_every_skill_the_server_serves(self):
        other = self.root / "other" / "SKILL.md"
        other.parent.mkdir()
        other.write_text("---\nname: other\ndescription: Other workflow.\n---\nBody\n")
        self.lib.register(other, "other")
        p = make_plan(self.config, self.registry, sys.executable, self.program, [self.entry["id"]])
        self.assertEqual(len(p["sources"]), 2)
        other.unlink()
        with self.assertRaises(LibraryError):
            apply(p, self.root / "transactions")
        self.assertEqual(self.config.read_bytes(), self.original)
        with self.assertRaises(LibraryError):
            make_plan(self.config, self.registry, sys.executable, self.program, [self.entry["id"]])

    def test_post_registration_compatibility_symlink_does_not_duplicate_skill_config(self):
        host_dir = self.root / "host-skill"
        host_dir.mkdir()
        skill_path = host_dir / "SKILL.md"
        skill_path.write_text("---\nname: compat\ndescription: Compat test.\n---\nBody\n")
        entry = self.lib.register(skill_path, "compat")
        first = self.apply()
        archived = self.root / "archive-skill"
        host_dir.rename(archived)
        host_dir.symlink_to(archived, target_is_directory=True)
        second_plan = self.plan()
        self.assertEqual(second_plan["changes"], [])
        self.lib.edit(entry["id"], placement="global")
        enable_plan = self.plan()
        self.assertEqual(len(enable_plan["changes"]), 1)
        enable_txn = apply(enable_plan, self.root / "transactions")
        parsed = tomlkit.parse(self.config.read_text()).unwrap()
        self.assertEqual(len(parsed["skills"]["config"]), 2)
        rollback(enable_txn["transaction"])
        rollback(first["transaction"])
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_deleted_config_during_rolling_back_is_conflict_not_restored(self):
        json_cfg = self.root / "mcp.json"
        json_cfg.write_text('{"theme": "precious"}\n')
        plan = make_plan(json_cfg, self.registry, sys.executable, self.program, fmt="mcp-json")
        result = apply(plan, self.root / "transactions")
        import configure, unittest.mock
        real_atomic = configure.atomic
        def fail_target(path, *args, **kwargs):
            if Path(path).resolve() == json_cfg.resolve():
                raise OSError("interrupted before target restore")
            return real_atomic(path, *args, **kwargs)
        with unittest.mock.patch.object(configure, "atomic", side_effect=fail_target):
            with self.assertRaisesRegex(OSError, "interrupted"):
                rollback(result["transaction"])
        json_cfg.unlink()
        with self.assertRaisesRegex(LibraryError, "conflict"):
            rollback(result["transaction"])

    def test_new_mcp_json_reformatted_by_host_unlinks_cleanly_on_rollback(self):
        json_cfg = self.root / "fresh-mcp.json"
        plan = make_plan(json_cfg, self.registry, sys.executable, self.program, fmt="mcp-json")
        result = apply(plan, self.root / "transactions")
        self.assertTrue(json_cfg.exists())
        compact = json.loads(json_cfg.read_text())
        json_cfg.write_text(json.dumps(compact, separators=(",", ":")))
        rollback(result["transaction"])
        self.assertFalse(json_cfg.exists())


if __name__ == "__main__":
    unittest.main()
