"""Stdio probe for the event-harvester MCP server.

Spawns `pixi run event-harvester mcp`, runs the MCP handshake, then either
lists tools/resources or calls a specific tool. Used as a manual smoke test.

Usage:
    pixi run python scripts/mcp_probe.py                   # list tools/resources
    pixi run python scripts/mcp_probe.py call <tool_name> [json_args]
        e.g. scripts/mcp_probe.py call config_summary
             scripts/mcp_probe.py call list_latest_events '{"status":"all"}'
"""

import json
import subprocess
import sys
import threading


def reader(stream, sink):
    for line in stream:
        sink.append(line.rstrip())


def main() -> int:
    proc = subprocess.Popen(
        ["pixi", "run", "event-harvester", "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    stderr_lines: list = []
    threading.Thread(target=reader, args=(proc.stderr, stderr_lines), daemon=True).start()

    def send(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def recv():
        return json.loads(proc.stdout.readline())

    try:
        send({
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0.1"},
            },
        })
        init = recv()
        print(f"server: {init['result']['serverInfo']['name']} v{init['result']['serverInfo']['version']}")

        send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        if len(sys.argv) >= 3 and sys.argv[1] == "call":
            tool = sys.argv[2]
            args = json.loads(sys.argv[3]) if len(sys.argv) >= 4 else {}
            print(f"\ncalling tool: {tool} with args {args}\n")
            send({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": args},
            })
            resp = recv()
            if "result" in resp:
                content = resp["result"].get("content", [])
                for block in content:
                    if block.get("type") == "text":
                        try:
                            parsed = json.loads(block["text"])
                            print(json.dumps(parsed, indent=2))
                        except json.JSONDecodeError:
                            print(block["text"])
            else:
                print(json.dumps(resp, indent=2))
        else:
            send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            tools = recv()["result"]["tools"]
            print(f"\ntools ({len(tools)}):")
            for t in tools:
                print(f"  - {t['name']}: {t['description'].splitlines()[0]}")

            send({"jsonrpc": "2.0", "id": 2, "method": "resources/list"})
            resources = recv()["result"]["resources"]
            print(f"\nresources ({len(resources)}):")
            for r in resources:
                print(f"  - {r['uri']}: {r['description']}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    return 0


if __name__ == "__main__":
    sys.exit(main())
