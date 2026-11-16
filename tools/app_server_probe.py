#!/usr/bin/env python3
"""Read-only standalone Codex app-server inventory and MCP probe. Not Desktop acceptance."""
import argparse
import json
import os
from pathlib import Path
import queue
import select
import subprocess
import threading
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=str(Path.cwd()))
    parser.add_argument("--output", required=True)
    parser.add_argument("--config", action="append", default=[])
    parser.add_argument("--mcp-server")
    args = parser.parse_args()
    command = ["codex", "app-server"]
    for value in args.config:
        command += ["-c", value]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    inbox = queue.Queue()
    stopped = threading.Event()

    def reader():
        pending = b''
        def accept(line):
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    inbox.put(value)
            except ValueError:
                pass
        try:
            # Poll the raw pipe so cleanup can stop the reader even when an
            # app-server descendant inherits stdout and never closes it.
            while not stopped.is_set():
                if not select.select([proc.stdout], [], [], .1)[0]:
                    continue
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    if pending:
                        accept(pending)
                    return
                pending += chunk
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    accept(line)
        except OSError:
            pass
        finally:
            inbox.put(None)  # EOF must wake a pending request immediately.
    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    def send(data):
        proc.stdin.write(json.dumps(data) + "\n")
        proc.stdin.flush()

    def call(i, method, params):
        send({"id": i, "method": method, "params": params})
        end = time.monotonic() + 45
        while time.monotonic() < end:
            try:
                value = inbox.get(timeout=max(.01, end - time.monotonic()))
            except queue.Empty as exc:
                raise TimeoutError(method) from exc
            if value is None:
                raise RuntimeError('app-server closed stdout while waiting for ' + method)
            if value.get("id") == i:
                if "error" in value:
                    raise RuntimeError(json.dumps(value["error"]))
                return value["result"]
        raise TimeoutError(method)

    try:
        init = call(1, "initialize", {"clientInfo": {"name": "skill_library_probe", "version": "0.1"},
                                     "capabilities": {"experimentalApi": True}})
        send({"method": "initialized"})
        skills = call(2, "skills/list", {"cwds": [args.cwd], "forceReload": True})
        result = {"surface": "standalone-app-server-not-desktop", "host": init, "skills": skills}
        if args.mcp_server:
            result["mcp"] = call(3, "mcpServerStatus/list",
                                 {"serverName": args.mcp_server, "detail": "toolsAndAuthOnly"})
        items = skills["data"][0]["skills"]
        enabled_count = sum(s["enabled"] for s in items)
        mcp_count = len(result.get("mcp", {}).get("data", []))
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = out_path.with_name(f".probe-{os.getpid()}-{out_path.name}")
        try:
            tmp_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_path, out_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
        print(json.dumps({"output": args.output, "surface": result["surface"],
                          "skills": len(items), "enabled": enabled_count,
                          "mcp_servers": mcp_count}))
    finally:
        stopped.set()
        try:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        finally:
            reader_thread.join(timeout=1)
            try:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            finally:
                proc.stdout.close()


if __name__ == "__main__":
    main()
