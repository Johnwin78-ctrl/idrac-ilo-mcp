"""Drive an MCP server (python or node) over stdio and call every tool."""
import asyncio, json, sys, os
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession

cmd, args, port = sys.argv[1], sys.argv[2:-1], sys.argv[-1]
env = {**os.environ, "BMC_HOST": f"http://127.0.0.1:{port}", "BMC_USERNAME": "monitor", "BMC_PASSWORD": "secret"}

async def main():
    async with stdio_client(StdioServerParameters(command=cmd, args=args, env=env)) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = [t.name for t in (await s.list_tools()).tools]
            print("TOOLS:", tools)
            calls = [("bmc_identify", {}), ("bmc_health_summary", {}), ("bmc_list_logs", {}),
                     ("bmc_get_event_logs", {"max_entries": 5}),
                     ("bmc_get_event_logs", {"log": "IML", "min_severity": "critical", "max_entries": 3}),
                     ("bmc_get_thermal", {}), ("bmc_get_power", {}), ("bmc_get_storage", {}),
                     ("bmc_get_inventory", {}), ("bmc_get_firmware", {}),
                     ("bmc_redfish_get", {"path": "/redfish/v1/"}), ("bmc_redfish_get", {"path": "/etc/passwd"}),
                     ("bmc_list_targets", {})]
            for name, a in calls:
                res = await s.call_tool(name, a)
                txt = "".join(getattr(c, "text", "") for c in res.content)
                try:
                    d = json.loads(txt)
                except Exception:
                    d = txt
                summary = json.dumps(d)[:380]
                flag = "ERR " if (isinstance(d, dict) and "error" in d) or getattr(res, "is_error", getattr(res, "isError", False)) else "ok  "
                print(f"{flag}{name}{a}: {summary}")
asyncio.run(main())
