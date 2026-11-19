# idrac-ilo-mcp — let Claude troubleshoot Dell iDRAC & HPE iLO servers

A small **read-only** MCP server that runs on your laptop and gives Claude direct access to a server's
iDRAC or iLO through the **Redfish API**. No web console and no browser involved: you say *"check the server"* and
Claude pulls health, logs, sensors, storage and firmware by itself.

It comes in two versions that do exactly the same thing. Pick **one**:

| Folder    | Needs                        | Use if…                                  |
|-----------|------------------------------|------------------------------------------|
| `python/` | Python 3.10+                 | you already have Python, or prefer it    |
| `node/`   | Node.js 18.17+               | you already have Node, or prefer it      |

## What Claude gets

| Tool | What it returns |
|------|-----------------|
| `bmc_health_summary` | **Start here.** Overall rollups (Dell rollup statuses / HPE AggregateHealthStatus) + every component that is not OK |
| `bmc_get_event_logs` | Event logs, newest first. Dell **SEL + Lifecycle Log**, HPE **IML + iLO Event Log**. Filter by log and severity |
| `bmc_list_logs` | Which logs this BMC has |
| `bmc_get_thermal` | Temperatures with thresholds, fans |
| `bmc_get_power` | PSUs (model, FW, input voltage, status), consumption, redundancy, bad voltage rails |
| `bmc_get_storage` | RAID controllers, drives (predictive failure, SSD wear), virtual disks / logical drives |
| `bmc_get_inventory` | CPUs, populated DIMMs (incl. HPE DIMM status), NICs |
| `bmc_get_firmware` | All installed firmware versions |
| `bmc_identify` | Vendor, iDRAC/iLO generation & FW, model, serial / service tag, BIOS, power state |
| `bmc_redfish_get` | Raw read of any `/redfish/...` path, for anything else |
| `bmc_set_target` / `bmc_list_targets` | Switch to another server during a chat |

**Generation-agnostic.** Nothing is hard-coded to a model. Every path is discovered from the Redfish service root, with
fallbacks for older layouts: legacy `Thermal`/`Power` vs newer `ThermalSubsystem`/`PowerSubsystem`, HPE `Hp` (iLO 4)
vs `Hpe` (iLO 5+) OEM data, HPE SmartStorage, and SimpleStorage. It should work with any Redfish-capable BMC:
roughly **iDRAC 7/8 (FW 2.40+), iDRAC 9, iDRAC 10** and **iLO 4 (FW 2.30+), iLO 5, iLO 6, iLO 7**.
Tested against simulated iDRAC 9, iLO 5 and iLO 4 responses. Real hardware always has quirks, so tell Claude if a tool
comes back empty and it can use `bmc_redfish_get` to look around.

**Read-only by design.** The only non-GET requests it ever sends are a log-in and log-out against the Redfish
SessionService. There are no power, reset, BIOS, RAID, firmware or clear-log tools. For extra safety, use a
read-only BMC account anyway:
- **Dell iDRAC:** a user with role **Read Only** (Login privilege only).
- **HPE iLO:** a user with only the **Login** privilege.

---

## Quick setup (Windows, Python version)

1. Copy this folder to e.g. `C:\Tools\bmc-mcp` (Python 3.10+ must be installed).
2. Double-click **`setup.bat`**. It installs the dependencies, asks for the iDRAC/iLO IP, username and password
   (typed in its own window, never in chat), tests the connection, and registers `bmc` in Claude desktop's config.
   It backs the config up first and keeps your other settings.
3. Fully quit Claude desktop (tray icon → Quit) and reopen it.

Rerun `setup.bat` any time to change the server or credentials. The manual steps below do the same thing by hand.

---

## Manual setup (Windows; macOS notes at the end)

### 1. Copy the folder
Unzip to a stable location, e.g. `C:\Tools\bmc-mcp\`.

### 2. Install the dependencies (one version only)

**Python**
```powershell
cd C:\Tools\bmc-mcp\python
py -m pip install -r requirements.txt
```

**Node**
```powershell
cd C:\Tools\bmc-mcp\node
npm install
```

### 3. Test the connection from your laptop (without Claude)
Your laptop must be able to reach the iDRAC/iLO IP (management network or VPN).

```powershell
$env:BMC_HOST="192.168.10.21"; $env:BMC_USERNAME="claude_ro"; $env:BMC_PASSWORD="your-password"
py C:\Tools\bmc-mcp\python\bmc_mcp.py --check        # Python
node C:\Tools\bmc-mcp\node\bmc-mcp.mjs --check        # or Node
```
You should get a JSON health report. If not, see **Troubleshooting** below.

### 4. Add it to the Claude desktop app
Open **Claude desktop → Settings → Developer → Edit Config**. That opens `claude_desktop_config.json`
(`%APPDATA%\Claude\claude_desktop_config.json`). Add **one** of these blocks inside `"mcpServers"`:

**Python**
```json
{
  "mcpServers": {
    "bmc": {
      "command": "py",
      "args": ["C:\\Tools\\bmc-mcp\\python\\bmc_mcp.py"],
      "env": {
        "BMC_HOST": "192.168.10.21",
        "BMC_USERNAME": "claude_ro",
        "BMC_PASSWORD": "your-password"
      }
    }
  }
}
```

**Node**
```json
{
  "mcpServers": {
    "bmc": {
      "command": "node",
      "args": ["C:\\Tools\\bmc-mcp\\node\\bmc-mcp.mjs"],
      "env": {
        "BMC_HOST": "192.168.10.21",
        "BMC_USERNAME": "claude_ro",
        "BMC_PASSWORD": "your-password"
      }
    }
  }
}
```
If `py` isn't found, use the full path to `python.exe` instead. Fully quit and reopen Claude desktop.
The `bmc_*` tools then appear in any chat linked to this computer.

### 5. Use it
> "Check the server's health and tell me what's wrong."
> "Show me critical and warning events from the last few days."
> "Is any drive predicted to fail? What's the RAID status?"

To look at a different server, either change `BMC_HOST` in the config and restart Claude desktop, or just tell Claude
*"switch to 192.168.10.35, user claude_ro, password …"* (it calls `bmc_set_target` for that chat only).

---

## Settings

| Variable | Default | Meaning |
|----------|---------|---------|
| `BMC_HOST` | — | IP or hostname, optionally with `https://` |
| `BMC_USERNAME` / `BMC_PASSWORD` | — | BMC account (read-only recommended) |
| `BMC_VERIFY_TLS` | `false` | `true` to verify the certificate (most iDRAC/iLO certificates are self-signed) |
| `BMC_TIMEOUT` | `30` | Seconds per request (raise for slow, older iLOs) |
| `BMC_USE_PROXY` | `false` | BMC traffic ignores system/corporate proxies by default. Set `true` only if you must go through one |
| `BMC_TARGETS_FILE` | — | Path to a JSON file of named servers (see below) |

### Future: several servers / the whole network
Prepared for this already. Instead of `BMC_HOST`, point `BMC_TARGETS_FILE` at a file like `targets.example.json`:
```json
{ "targets": {
    "r750-dcs-01":  { "host": "192.168.10.21", "username": "claude_ro", "password": "..." },
    "dl380-his-02": { "host": "192.168.10.35", "username": "claude_ro", "password": "..." } } }
```
The first entry is the default. Say *"switch to dl380-his-02"* and Claude calls `bmc_set_target(name=...)`.
A later version can add a fleet-wide scan tool on top of this same file.

**Credentials:** the config/targets file stores the password in plain text on your laptop. Keep it in your user profile,
use a read-only BMC account, and don't put the targets file in a shared folder.

---

## Troubleshooting

| Message | Fix |
|---------|-----|
| `Cannot reach … Is this computer on the management network/VPN?` | Ping the IP; connect to the OOB VLAN/VPN; check the firewall allows TCP 443 |
| `Login rejected (401)` | Wrong user/password, or the account is locked or has no remote/Redfish login |
| `Access denied (403)` | Account lacks Login privilege, or a proxy/firewall blocked it (try `BMC_USE_PROXY` if your site forces a proxy) |
| `GET /redfish/v1/ -> HTTP 404` | Firmware too old for Redfish (iDRAC 7/8 < 2.40, iLO 4 < 2.30). Update the BMC firmware |
| Timeouts on iLO 4 | Set `BMC_TIMEOUT` to `60` |
| Storage comes back empty (HPE) | The server must have finished POST since the last iLO reset; some HBAs/NVMe aren't reported |
| Tools don't show up in Claude | Check the JSON is valid, then fully quit and reopen Claude desktop. See *Settings → Developer* for errors |

## macOS
Same steps. Use `python3` instead of `py`, paths like `/Users/you/Tools/bmc-mcp/python/bmc_mcp.py`, and the config
file at `~/Library/Application Support/Claude/claude_desktop_config.json`.

## Files
```
bmc-mcp/
  setup.bat / setup.py     one-click installer + Claude desktop registration (Python version)
  python/bmc_mcp.py        Python server     (+ requirements.txt)
  node/bmc-mcp.mjs         Node server       (+ package.json)
  targets.example.json     template for named servers
  test/mock_redfish.py     simulated iDRAC 9 / iLO 5 / iLO 4 for testing
  test/mcp_client_test.py  calls every tool:  python test/mock_redfish.py dell 8001  then
                           python test/mcp_client_test.py py python/bmc_mcp.py 8001
```
