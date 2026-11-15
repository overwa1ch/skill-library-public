"""Folder scans and relocation out of host skill folders, for hosts without a per-skill switch."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from library import Library, LibraryError, tree_manifest


class RelocateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.lib = Library(self.root / "runtime" / "registry.json")
        self.lib.init()
        self.host = self.root / "host-skills"
        self.one = self.make_skill("one")
        (self.one / "scripts").mkdir()
        (self.one / "scripts" / "run.sh").write_text("echo hi\n")
        os.symlink("scripts/run.sh", self.one / "run")
        self.make_skill("two")
        (self.host / ".system").mkdir()
        (self.host / ".system" / "SKILL.md").write_text("---\nname: hidden\ndescription: x\n---\n")

    def make_skill(self, name):
        folder = self.host / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\nname: " + name + "\ndescription: Fixture " + name + ".\n---\nBody\n")
        return folder

    def relocate(self, sid, into=None):
        return self.lib.relocate(sid, into, self_contained=True)

    def scanned(self):
        return {e["source"]: e for e in self.lib.scan(self.host, "fixture:host", tags=["t"])}

    # Scan

    def test_scan_registers_visible_folders_once(self):
        first = self.scanned()
        self.assertEqual(sorted(first), ["fixture:host/one", "fixture:host/two"])
        self.assertEqual({e["placement"] for e in first.values()}, {"library"})
        self.assertEqual(first["fixture:host/one"]["tags"], ["t"])
        self.lib.edit(first["fixture:host/one"]["id"], tags=["kept"])
        again = self.scanned()
        self.assertEqual(again["fixture:host/one"]["tags"], ["kept"])
        self.assertEqual(len(self.lib.read()["skills"]), 2)

    def test_scan_rejects_empty_folders(self):
        (self.root / "empty").mkdir()
        with self.assertRaisesRegex(LibraryError, "No skill folders"):
            self.lib.scan(self.root / "empty", "fixture:empty")

    # Relocate

    def test_relocate_moves_folder_keeps_id_and_archives_original(self):
        entry = self.scanned()["fixture:host/one"]
        before = tree_manifest(self.one)
        result = self.relocate(entry["id"])
        target = self.root / "runtime" / "sources" / "personal" / "one"
        self.assertEqual(Path(result["to"]), target)
        self.assertFalse(self.one.exists())
        self.assertEqual(tree_manifest(target), before)
        self.assertEqual(tree_manifest(result["archive"]), before)
        self.assertEqual(os.readlink(target / "run"), "scripts/run.sh")
        loaded = self.lib.load(entry["id"], tool=True)
        self.assertEqual(loaded["skill_root"], str(target))
        self.assertEqual([t["name"] for t in self.lib.tools()].count("load_" + entry["id"]), 1)
        record = json.loads((Path(result["relocation"]) / "relocation.json").read_text())
        self.assertEqual(record["status"], "applied")

        self.lib.relocate_rollback(result["relocation"])
        self.assertEqual(tree_manifest(self.one), before)
        self.assertFalse(target.exists())
        self.assertEqual(tree_manifest(Path(result["relocation"]) / "library-copy"), before)
        self.assertEqual(self.lib.load(entry["id"])["skill_root"], str(self.one))
        self.assertEqual(self.lib.relocate_rollback(result["relocation"])["status"], "already_rolled_back")

    def test_relocate_refuses_occupied_destination_and_repeat(self):
        entry = self.scanned()["fixture:host/one"]
        (self.root / "runtime" / "sources" / "personal" / "one").mkdir(parents=True)
        with self.assertRaisesRegex(LibraryError, "Destination already exists"):
            self.relocate(entry["id"])
        self.assertTrue(self.one.exists())
        other = self.root / "elsewhere"
        self.relocate(entry["id"], other)
        with self.assertRaisesRegex(LibraryError, "already inside"):
            self.relocate(entry["id"], other)

    def test_relocate_refuses_if_source_changes_during_copy(self):
        entry = self.scanned()["fixture:host/one"]
        original_copytree = shutil.copytree

        def changed_source(*args, **kwargs):
            result = original_copytree(*args, **kwargs)
            (self.one / "scripts" / "run.sh").write_text("echo updated\n")
            return result

        with patch("library.shutil.copytree", side_effect=changed_source):
            with self.assertRaisesRegex(LibraryError, "Relocation directory changed"):
                self.relocate(entry["id"])
        self.assertEqual((self.one / "scripts" / "run.sh").read_text(), "echo updated\n")
        self.assertEqual(self.lib.load(entry["id"])["skill_root"], str(self.one))
        self.assertFalse((self.root / "runtime" / "sources" / "personal" / "one").exists())

    def test_relocate_refuses_global_disabled_and_nested_skills(self):
        entries = self.scanned()
        self.lib.edit(entries["fixture:host/two"]["id"], placement="global")
        with self.assertRaisesRegex(LibraryError, "Only enabled Library-managed"):
            self.relocate(entries["fixture:host/two"]["id"])
        self.lib.edit(entries["fixture:host/two"]["id"], placement="library", enabled=False)
        with self.assertRaisesRegex(LibraryError, "Only enabled Library-managed"):
            self.relocate(entries["fixture:host/two"]["id"])
        nested = self.one / "references" / "inner"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_text("---\nname: inner\ndescription: Nested.\n---\n")
        inner = self.lib.register(nested / "SKILL.md", "fixture:inner")
        for sid in (entries["fixture:host/one"]["id"], inner["id"]):
            with self.assertRaisesRegex(LibraryError, "overlaps registered skill"):
                self.relocate(sid)

    def test_relocate_refuses_relative_links_leaving_the_folder(self):
        entry = self.scanned()["fixture:host/one"]
        os.symlink("../two/SKILL.md", self.one / "sibling")
        with self.assertRaisesRegex(LibraryError, "Relative link would break"):
            self.relocate(entry["id"])
        self.assertTrue(self.one.exists())

    def test_relocate_refuses_absolute_link_to_source_tree(self):
        entry = self.scanned()["fixture:host/one"]
        os.symlink(str(self.one / "scripts" / "run.sh"), self.one / "absolute-run")
        with self.assertRaisesRegex(LibraryError, "Absolute link would break"):
            self.relocate(entry["id"])
        self.assertTrue((self.one / "absolute-run").exists())
        self.assertEqual(self.lib.load(entry["id"])["skill_root"], str(self.one))

    def test_relocate_refuses_absolute_link_resolving_via_source_tree(self):
        entry = self.scanned()["fixture:host/one"]
        external = self.root / "external-file"
        external.write_text("external\n")
        os.symlink(str(external), self.one / "inside-to-outside")
        alias = self.root / "alias"
        os.symlink(str(self.one / "inside-to-outside"), alias)
        os.symlink(str(alias), self.one / "via-alias")
        with self.assertRaisesRegex(LibraryError, "Absolute link would break"):
            self.relocate(entry["id"])
        self.assertTrue((self.one / "via-alias").exists())
        self.assertEqual(self.lib.load(entry["id"])["skill_root"], str(self.one))

    def test_relocate_refuses_relative_link_returning_via_external_directory(self):
        entry = self.scanned()["fixture:host/one"]
        external = self.root / "external-directory"
        external.mkdir()
        os.symlink(str(self.one / "scripts" / "run.sh"), external / "back")
        os.symlink(str(external), self.one / "external-directory")
        os.symlink("external-directory/back", self.one / "transitive")
        self.assertTrue((self.one / "transitive").exists())
        with self.assertRaisesRegex(LibraryError, "Relative link would break"):
            self.relocate(entry["id"])
        self.assertTrue((self.one / "transitive").exists())
        self.assertEqual(self.lib.load(entry["id"])["skill_root"], str(self.one))

    def test_relocate_refuses_double_slash_absolute_link_to_source_tree(self):
        entry = self.scanned()["fixture:host/one"]
        target = "/" + str(self.one / "scripts" / "run.sh")
        os.symlink(target, self.one / "absolute-run")
        with self.assertRaisesRegex(LibraryError, "Absolute link would break"):
            self.relocate(entry["id"])
        self.assertTrue((self.one / "absolute-run").exists())

    def test_relocate_preserves_absolute_link_to_external_file(self):
        entry = self.scanned()["fixture:host/one"]
        external = self.root / "external-file"
        external.write_text("external\n")
        os.symlink(str(external), self.one / "external")
        result = self.relocate(entry["id"])
        self.assertEqual((Path(result["to"]) / "external").read_text(), "external\n")
        self.lib.relocate_rollback(result["relocation"])
        self.assertEqual((self.one / "external").read_text(), "external\n")

    def test_relocate_refuses_folders_inside_a_git_checkout(self):
        entry = self.scanned()["fixture:host/two"]
        (self.host / ".git").mkdir()
        with self.assertRaisesRegex(LibraryError, "belongs to the git checkout"):
            self.relocate(entry["id"])
        self.assertTrue((self.host / "two").exists())

    def test_rollback_refuses_when_library_copy_was_edited_or_origin_reused(self):
        entry = self.scanned()["fixture:host/one"]
        result = self.relocate(entry["id"])
        target = Path(result["to"])
        (target / "SKILL.md").write_text((target / "SKILL.md").read_text() + "edited\n")
        with self.assertRaisesRegex(LibraryError, "Relocation directory changed"):
            self.lib.relocate_rollback(result["relocation"])
        self.one.mkdir()
        with self.assertRaisesRegex(LibraryError, "Original location is occupied"):
            self.lib.relocate_rollback(result["relocation"])
        self.assertEqual(self.lib.load(entry["id"])["skill_root"], str(target))


if __name__ == "__main__":
    unittest.main()
