#!/usr/bin/env python3
"""Plan, apply and roll back only Skill Library's host configuration changes.

codex-toml edits Codex config.toml (MCP server plus native skill display).
mcp-json edits the mcpServers object used by Claude Code (~/.claude.json),
Claude Desktop, Cursor and other JSON-configured MCP clients.
"""
import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

import tomlkit
from library import Library, LibraryError, canonical_path


SERVER = "skill_library"
JSON_SERVER = "skill-library"
FORMATS = ("codex-toml", "mcp-json")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def plan_format(plan):
    # Plans and transactions written before JSON hosts existed are Codex plans.
    return plan.get("format", "codex-toml")


def server_name(plan):
    return plan.get("server_name") or (JSON_SERVER if plan_format(plan) == "mcp-json" else SERVER)


def backup_names(fmt):
    return ("before.json", "after.json") if fmt == "mcp-json" else ("before.toml", "after.toml")


def parse(data, fmt="codex-toml"):
    if fmt == "mcp-json":
        try:
            doc = json.loads(data.decode("utf-8")) if data.strip() else {}
        except (UnicodeError, ValueError) as exc:
            raise LibraryError("Invalid JSON configuration: " + str(exc)) from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("mcpServers", {}), dict):
            raise LibraryError("JSON configuration must be an object whose mcpServers is an object")
        return doc
    try:
        doc = tomlkit.parse(data.decode("utf-8"))
        for name in ("skills", "mcp_servers"):
            if name in doc and not isinstance(plain(doc[name]), dict):
                raise LibraryError(name + " must be a table")
        return doc
    except Exception as exc:
        raise LibraryError("Invalid TOML configuration: " + str(exc)) from exc


def dumps(doc, fmt="codex-toml", original=b""):
    if fmt == "mcp-json":
        newline = "\n" if not original or original.endswith(b"\n") else ""
        return (json.dumps(doc, ensure_ascii=False, indent=2) + newline).encode()
    return tomlkit.dumps(doc).encode()


def plain(item):
    return item.unwrap() if hasattr(item, "unwrap") else item


def skill_entries(doc):
    items = doc.get("skills", {}).get("config", [])
    if not isinstance(plain(items), list):
        raise LibraryError("skills.config must be an array of tables")
    seen = set()
    for item in items:
        if not isinstance(plain(item), dict) or not isinstance(item.get("path"), str):
            raise LibraryError("Invalid skills.config entry")
        path = str(canonical_path(item["path"]))
        if path in seen:
            raise LibraryError("Duplicate skills.config path")
        seen.add(path)
    return items


def target_value(doc, change, name=SERVER, fmt="codex-toml"):
    if change["kind"] == "server":
        table = "mcpServers" if fmt == "mcp-json" else "mcp_servers"
        return copy.deepcopy(plain(doc.get(table, {}).get(name)))
    for item in skill_entries(doc):
        if str(canonical_path(item["path"])) == change["path"]:
            return copy.deepcopy(plain(item))
    return None


def set_target(doc, change, value, name=SERVER, fmt="codex-toml"):
    if change["kind"] == "server":
        table = "mcpServers" if fmt == "mcp-json" else "mcp_servers"
        if value is None:
            if table in doc:
                doc[table].pop(name, None)
                if not doc[table]:
                    del doc[table]
        else:
            if table not in doc:
                doc[table] = {} if fmt == "mcp-json" else tomlkit.table()
            doc[table][name] = value
        return
    if "skills" not in doc:
        doc["skills"] = tomlkit.table()
    if "config" not in doc["skills"]:
        doc["skills"]["config"] = tomlkit.aot()
    items = doc["skills"]["config"]
    index = next((i for i, s in enumerate(items)
                  if str(canonical_path(s["path"])) == change["path"]), None)
    if value is None:
        if index is not None:
            del items[index]
        if not items:
            del doc["skills"]["config"]
        if not doc["skills"]:
            del doc["skills"]
    elif index is not None:
        items[index] = value
    else:
        table = tomlkit.table()
        table.update(value)
        items.append(table)


def make_plan(config, registry, executable, program, ids=None, unavailable_ids=(),
              fmt="codex-toml", name=None):
    if fmt not in FORMATS:
        raise LibraryError("Unknown configuration format: " + str(fmt))
    name = name or (JSON_SERVER if fmt == "mcp-json" else SERVER)
    config = canonical_path(config)
    before = config.read_bytes() if config.exists() else b""
    doc = parse(before, fmt)
    if fmt == "codex-toml":
        skill_entries(doc)
    lib = Library(registry)
    all_entries = lib.read()["skills"]
    approved_unavailable = set(unavailable_ids or ())
    eligible = {e["id"] for e in all_entries if e["enabled"] and e["placement"] == "library"
                and lib.source_current(e)}
    if approved_unavailable - eligible:
        raise LibraryError("Unavailable approval requires enabled Library IDs")
    entries = all_entries
    if ids is not None:
        unknown = set(ids) - {s["id"] for s in entries}
        if unknown:
            raise LibraryError("Unknown selected IDs")
        entries = [s for s in entries if s["id"] in ids]
    # The server serves the whole library even when native changes select a subset.
    selected = {entry["id"] for entry in entries}
    sources, unavailable = [], []
    for entry in all_entries:
        if lib.source_inactive(entry):
            continue
        if entry["enabled"] and (entry["placement"] == "library"
                                 or (fmt == "codex-toml" and entry["id"] in selected)):
            try:
                loaded = lib.load(entry["id"])
            except (LibraryError, OSError) as exc:
                if entry["id"] not in approved_unavailable:
                    raise
                unavailable.append({"id": entry["id"], "path": entry["path"],
                                    "error_sha256": sha(str(exc).encode()), "error": str(exc)})
                continue
            if entry["id"] in approved_unavailable:
                raise LibraryError("Unavailable approval names a readable source; regenerate plan")
            sources.append({"id": entry["id"], "path": loaded["path"], "sha256": loaded["sha256"],
                            "policy_sha256": loaded["policy_sha256"]})
    if approved_unavailable and not any(s["id"] in eligible for s in sources):
        raise LibraryError("Unavailable approvals require a readable Library source")
    # Preserve a virtualenv's interpreter path; resolving its symlink loses the venv.
    command = str(Path(executable).expanduser().absolute())
    args = [str(Path(program).resolve()), "--registry", str(lib.path), "serve"]
    if fmt == "mcp-json":
        server = {"type": "stdio", "command": command, "args": args + ["--host", "generic"], "env": {}}
    else:
        server = {"command": command, "args": args, "enabled": True}
    runtime = []
    for path in (server["command"], server["args"][0]):
        if not Path(path).is_file():
            raise LibraryError("Missing runtime file: " + path)
        runtime.append({"path": path, "sha256": sha(Path(path).read_bytes())})
    change = {"kind": "server", "before": target_value(doc, {"kind": "server"}, name, fmt), "after": server}
    if change["before"] not in (None, server):
        raise LibraryError("Existing " + name + " server differs; do not overwrite")
    changes = [change] if change["before"] != server else []
    # JSON hosts read their own skill folders; only Codex lets the Library hide native entries.
    for entry in (entries if fmt == "codex-toml" else ()):
        desired = entry["enabled"] and lib.source_current(entry) if entry["placement"] == "global" else False
        change = {"kind": "skill", "path": entry["path"]}
        previous = target_value(doc, change)
        if previous is None and desired:
            continue
        after = dict(previous or {"path": entry["path"]}, enabled=desired)
        if previous != after:
            changes.append(dict(change, before=previous, after=after))
    plan = {"version": 1, "format": fmt, "server_name": name,
            "config": str(config), "before_sha256": sha(before),
            "config_existed": config.exists(), "changes": changes,
            "registry": str(lib.path), "registry_sha256": sha(lib.path.read_bytes()),
            "sources": sources, "runtime": runtime, "unavailable_sources": unavailable,
            "inactive_sources": [{"id": e["id"], "path": e["path"], "source_state": e["source_state"]}
                                 for e in all_entries if lib.source_inactive(e)]}
    if fmt == "mcp-json" and not changes:
        # No mutation still requires the installed, owned server to match.
        plan["unchanged_server"] = server
    return plan


def validate_plan(plan, require_inputs=True):
    if not isinstance(plan, dict) or plan.get("version") != 1:
        raise LibraryError("Invalid configuration plan version")
    for name in ("config", "registry"):
        if not isinstance(plan.get(name), str) or not Path(plan[name]).is_absolute():
            raise LibraryError("Plan requires absolute " + name)
    for name in ("before_sha256", "registry_sha256"):
        if not isinstance(plan.get(name), str) or not re.fullmatch(r"[0-9a-f]{64}", plan[name]):
            raise LibraryError("Invalid plan checksum: " + name)
    if type(plan.get("config_existed")) is not bool or not isinstance(plan.get("changes"), list):
        raise LibraryError("Invalid configuration plan fields")
    fmt = plan_format(plan)
    if fmt not in FORMATS:
        raise LibraryError("Unknown configuration format")
    if "server_name" in plan and (not isinstance(plan["server_name"], str) or not plan["server_name"].strip()):
        raise LibraryError("Invalid plan server name")
    if "unchanged_server" in plan and (fmt != "mcp-json" or plan["changes"]
                                       or not isinstance(plan["unchanged_server"], dict)):
        raise LibraryError("Invalid unchanged server binding")
    seen = set()
    for change in plan["changes"]:
        if not isinstance(change, dict) or change.get("kind") not in ("server", "skill"):
            raise LibraryError("Invalid plan target")
        if fmt == "mcp-json" and change["kind"] != "server":
            raise LibraryError("JSON host plans may only change the server entry")
        if change["kind"] == "skill":
            path = change.get("path")
            if not isinstance(path, str) or not Path(path).is_absolute() or Path(path).name != "SKILL.md":
                raise LibraryError("Invalid plan skill path")
        key = (change["kind"], change.get("path"))
        if key in seen:
            raise LibraryError("Duplicate plan target")
        seen.add(key)
        for field in ("before", "after"):
            if field not in change or (change[field] is not None and not isinstance(change[field], dict)):
                raise LibraryError("Invalid plan target value")
    if require_inputs:
        for name in ("sources", "runtime"):
            if not isinstance(plan.get(name), list):
                raise LibraryError("Plan lacks input checksums; regenerate plan")
            for item in plan[name]:
                if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                        or not Path(item["path"]).is_absolute()
                        or not isinstance(item.get("sha256"), str)
                        or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
                    raise LibraryError("Invalid plan input checksum")
                if name == "sources" and (not isinstance(item.get("id"), str)
                                          or not re.fullmatch(r"[0-9a-f]{32}", item["id"])):
                    raise LibraryError("Invalid plan source ID")
                if name == "sources" and (
                        "policy_sha256" not in item
                        or (item["policy_sha256"] is not None
                            and (not isinstance(item["policy_sha256"], str)
                                 or not re.fullmatch(r"[0-9a-f]{64}", item["policy_sha256"])))):
                    raise LibraryError("Plan lacks valid policy checksum; regenerate plan")
        if len(plan["runtime"]) != 2:
            raise LibraryError("Plan must bind interpreter and loader")
        unavailable = plan.get("unavailable_sources", [])
        if not isinstance(unavailable, list):
            raise LibraryError("Invalid unavailable source approvals")
        source_ids = {s["id"] for s in plan["sources"]}
        if len(source_ids) != len(plan["sources"]):
            raise LibraryError("Duplicate readable source binding")
        for item in unavailable:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                    or not re.fullmatch(r"[0-9a-f]{32}", item["id"])
                    or item["id"] in source_ids
                    or not isinstance(item.get("path"), str) or not Path(item["path"]).is_absolute()
                    or not isinstance(item.get("error"), str)
                    or item.get("error_sha256") != sha(item["error"].encode())):
                raise LibraryError("Invalid unavailable source approval")
            source_ids.add(item["id"])


def verify_inputs(plan):
    lib = Library(plan["registry"])
    if sha(lib.path.read_bytes()) != plan["registry_sha256"]:
        raise LibraryError("Registry changed after plan; regenerate plan")
    state = lib.read()
    expected = {entry["id"] for entry in state["skills"]
                if entry["enabled"] and entry["placement"] == "library" and not lib.source_inactive(entry)}
    inactive = [{"id": e["id"], "path": e["path"], "source_state": e["source_state"]}
                for e in state["skills"] if lib.source_inactive(e)]
    if plan.get("inactive_sources", []) != inactive:
        raise LibraryError("Plan must bind all historical plugin source states")
    bound = {item["id"] for item in plan["sources"]}
    bound.update(item["id"] for item in plan.get("unavailable_sources", []))
    if not expected.issubset(bound):
        raise LibraryError("Plan must bind every enabled Library source or its explicit unavailable approval")
    for item in plan.get("unavailable_sources", []):
        entry = lib.entry(state, item["id"])
        if (not entry["enabled"] or entry["placement"] != "library"
                or entry["path"] != item["path"]):
            raise LibraryError("Unavailable source state changed after plan")
        try:
            lib.load(item["id"])
        except (LibraryError, OSError) as exc:
            if sha(str(exc).encode()) != item["error_sha256"]:
                raise LibraryError("Unavailable source changed after plan; regenerate plan") from exc
        else:
            raise LibraryError("Unavailable source recovered after plan; regenerate plan")
    for item in plan["sources"]:
        loaded = lib.load(item.get("id"))
        if (loaded["path"], loaded["sha256"]) != (item["path"], item["sha256"]):
            raise LibraryError("Skill changed after plan; regenerate plan")
        if loaded["policy_sha256"] != item["policy_sha256"]:
            raise LibraryError("Skill policy changed after plan; regenerate plan")
    for item in plan["runtime"]:
        path = Path(item["path"])
        if not path.is_file() or sha(path.read_bytes()) != item["sha256"]:
            raise LibraryError("Runtime changed after plan; regenerate plan")
    if sha(lib.path.read_bytes()) != plan["registry_sha256"]:
        raise LibraryError("Registry changed during input verification; regenerate plan")


def atomic(path, data, mode=0o600, *, expected=None,
           conflict_message="Configuration changed during write"):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".skill-library-", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Stage and verify expensive inputs before this last content check.
        # A non-cooperating writer can still race between this check and replace.
        if expected is not None and (path.read_bytes() if path.exists() else b"") != expected:
            raise LibraryError(conflict_message)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def apply(plan, transactions):
    validate_plan(plan)
    fmt, name = plan_format(plan), server_name(plan)
    path = Path(plan["config"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".skill-library.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        verify_inputs(plan)
        before = path.read_bytes() if path.exists() else b""
        # JSON host files such as ~/.claude.json are rewritten by the running host,
        # so for them only the owned server entry has to match the plan.
        if fmt == "codex-toml" and sha(before) != plan["before_sha256"]:
            raise LibraryError("Configuration changed after plan; regenerate plan")
        doc = parse(before, fmt)
        if not plan["changes"]:
            if fmt == "mcp-json":
                if "unchanged_server" in plan:
                    if target_value(doc, {"kind": "server"}, name, fmt) != plan["unchanged_server"]:
                        raise LibraryError("Plan target conflict")
                elif sha(before) != plan["before_sha256"]:
                    # Legacy JSON no-op plans have no owned-target snapshot.
                    raise LibraryError("Configuration changed after plan; regenerate plan")
            return {"changed": False, "transaction": None}
        for change in plan["changes"]:
            if target_value(doc, change, name, fmt) != change["before"]:
                raise LibraryError("Plan target conflict")
            set_target(doc, change, change["after"], name, fmt)
        after = dumps(doc, fmt, before)
        parse(after, fmt)
        record = dict(plan, after_sha256=sha(after), status="prepared")
        if fmt == "mcp-json":
            record.update(before_sha256=sha(before), config_existed=path.exists())
        txn = Path(transactions).absolute() / uuid.uuid4().hex
        txn.mkdir(parents=True, mode=0o700)
        before_name, after_name = backup_names(fmt)
        atomic(txn / before_name, before)
        atomic(txn / after_name, after)
        atomic(txn / "transaction.json", (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode())
        verify_inputs(plan)
        atomic(path, after, (path.stat().st_mode & 0o777) if path.exists() else 0o600,
               expected=before, conflict_message="Configuration changed during apply; no overwrite")
        record["status"] = "applied"
        atomic(txn / "transaction.json", (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode())
        return {"changed": True, "transaction": str(txn), "after_sha256": sha(after)}


def rollback(txn):
    txn = Path(txn)
    record_path = txn / "transaction.json"
    record = json.loads(record_path.read_text())
    # Earlier transactions did not contain source/runtime fingerprints.
    validate_plan(record, require_inputs=False)
    if record.get("status") == "rolled_back":
        return {"changed": False, "status": "already_rolled_back"}
    fmt, name = plan_format(record), server_name(record)
    path = Path(record["config"])
    with open(str(path) + ".skill-library.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = path.read_bytes() if path.exists() else b""
        original = (txn / backup_names(fmt)[0]).read_bytes()
        if sha(original) != record["before_sha256"]:
            raise LibraryError("Backup checksum mismatch")
        if current == original and record.get("status") == "prepared":
            result = current
        elif sha(current) == record["after_sha256"]:
            result = original
        else:
            doc = parse(current, fmt)
            for change in reversed(record["changes"]):
                if target_value(doc, change, name, fmt) != change["after"]:
                    raise LibraryError("Rollback conflict: target changed externally")
                set_target(doc, change, change["before"], name, fmt)
            if fmt == "mcp-json" and record["changes"] and "mcpServers" in parse(original, fmt):
                # Preserve the original container without restoring other hosts' keys.
                doc.setdefault("mcpServers", {})
            result = dumps(doc, fmt, current)
        if not record["config_existed"] and not result:
            if (path.read_bytes() if path.exists() else b"") != current:
                raise LibraryError("Configuration changed during rollback")
            path.unlink(missing_ok=True)
        else:
            atomic(path, result, (path.stat().st_mode & 0o777) if path.exists() else 0o600,
                   expected=current, conflict_message="Configuration changed during rollback")
        record["status"] = "rolled_back"
        atomic(record_path, (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode())
        return {"changed": current != result, "status": "rolled_back"}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--config", required=True)
    plan.add_argument("--format", choices=FORMATS, default="codex-toml",
                      help="codex-toml for Codex config.toml; mcp-json for any mcpServers JSON file")
    plan.add_argument("--server-name", help="Server key to own (default skill_library for Codex, skill-library for JSON)")
    plan.add_argument("--registry", required=True)
    plan.add_argument("--python", default=sys.executable)
    plan.add_argument("--program", default=str(Path(__file__).with_name("library.py")))
    plan.add_argument("--id", action="append")
    plan.add_argument("--allow-unavailable", action="append", default=[], metavar="ID",
                      help="Explicitly preserve an unavailable enabled Library source; repeat per ID")
    plan.add_argument("--output", required=True)
    run = sub.add_parser("apply")
    run.add_argument("--plan", required=True)
    run.add_argument("--transactions", required=True)
    undo = sub.add_parser("rollback")
    undo.add_argument("transaction")
    args = p.parse_args()
    try:
        if args.command == "plan":
            result = make_plan(args.config, args.registry, args.python, args.program, args.id,
                               args.allow_unavailable, args.format, args.server_name)
            atomic(Path(args.output), (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode())
            print(json.dumps({"plan": args.output, "change_count": len(result["changes"]),
                              "before_sha256": result["before_sha256"]}))
        elif args.command == "apply":
            print(json.dumps(apply(json.loads(Path(args.plan).read_text()), args.transactions)))
        else:
            print(json.dumps(rollback(args.transaction)))
        return 0
    except (LibraryError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
