"""
bmc-mcp one-click setup (Python version).
  1. checks Python version
  2. installs dependencies (mcp, httpx)
  3. asks for the iDRAC/iLO address and credentials (typed here, not in chat)
  4. tests the connection with  bmc_mcp.py --check
  5. adds "bmc" to Claude desktop's claude_desktop_config.json (backup kept)
Writes setup-log.txt next to this file (no password in it).
"""
import getpass
import glob
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "python", "bmc_mcp.py")
REQS = os.path.join(HERE, "python", "requirements.txt")
LOG = os.path.join(HERE, "setup-log.txt")
log_lines = []


def say(msg=""):
    print(msg)
    log_lines.append(msg)


def save_log():
    with open(LOG, "w", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + "\n".join(log_lines) + "\n")


def step(n, title):
    say("")
    say(f"[{n}/5] {title}")
    say("-" * 60)


def config_dirs():
    """Claude desktop config folders (normal installer and Microsoft Store/MSIX)."""
    dirs = []
    appdata = os.environ.get("APPDATA")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs += glob.glob(os.path.join(local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude"))
        dirs += glob.glob(os.path.join(local, "Packages", "AnthropicPBC.Claude*", "LocalCache", "Roaming", "Claude"))
    if appdata and os.path.isdir(os.path.join(appdata, "Claude")):
        dirs.append(os.path.join(appdata, "Claude"))
    if sys.platform == "darwin":
        mac = os.path.expanduser("~/Library/Application Support/Claude")
        if os.path.isdir(mac):
            dirs.append(mac)
    if not dirs and appdata:
        dirs.append(os.path.join(appdata, "Claude"))
    return list(dict.fromkeys(dirs))


def load_config(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig") as f:
        text = f.read().strip()
    return json.loads(text) if text else {}


def existing_bmc_env():
    for d in config_dirs():
        try:
            env = load_config(os.path.join(d, "claude_desktop_config.json")).get("mcpServers", {}).get("bmc", {}).get("env", {})
            if env:
                return env
        except Exception:
            pass
    return {}


def main():
    say("bmc-mcp setup")
    say("=" * 60)

    step(1, "Checking Python")
    say(f"Python {sys.version.split()[0]} at {sys.executable}")
    if sys.version_info < (3, 10):
        say("ERROR: Python 3.10 or newer is required. Install it from python.org and run setup again.")
        return 1

    step(2, "Installing dependencies (mcp, httpx)")
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r", REQS]
    r = subprocess.run(cmd)
    if r.returncode != 0:
        say("Normal install failed, retrying as --user ...")
        r = subprocess.run(cmd + ["--user"])
    v = subprocess.run([sys.executable, "-c", "import mcp, httpx; print('ok')"], capture_output=True, text=True)
    if v.stdout.strip() != "ok":
        say("ERROR: dependencies did not install. pip needs internet access (check proxy settings).")
        say(v.stderr[-800:])
        return 1
    say("Dependencies installed OK.")

    step(3, "iDRAC / iLO details")
    prev = existing_bmc_env()
    def ask(label, key, default=""):
        d = prev.get(key, default)
        val = input(f"{label}{f' [{d}]' if d else ''}: ").strip()
        return val or d
    host = ask("iDRAC/iLO IP or hostname (e.g. 192.168.10.21)", "BMC_HOST")
    user = ask("Username (a read-only account is recommended)", "BMC_USERNAME")
    pw = getpass.getpass("Password (typing is hidden" + (", Enter = keep current" if prev.get("BMC_PASSWORD") else "") + "): ")
    pw = pw or prev.get("BMC_PASSWORD", "")
    if not host or not user or not pw:
        say("ERROR: IP, username and password are all required.")
        return 1
    say(f"Target: {host}  user: {user}")

    step(4, "Testing connection to the iDRAC/iLO")
    env = {**os.environ, "BMC_HOST": host, "BMC_USERNAME": user, "BMC_PASSWORD": pw}
    try:
        t = subprocess.run([sys.executable, SERVER, "--check"], env=env, capture_output=True, text=True, timeout=300)
        out = t.stdout
    except subprocess.TimeoutExpired:
        t, out = None, "ERROR: timed out after 5 minutes"
    ok = t is not None and t.returncode == 0
    if ok:
        try:
            data = json.loads(out[out.index("{"):])
            ident = data.get("identity", {})
            sysinfo = (ident.get("systems") or [{}])[0]
            bmc = ident.get("bmc", {})
            say(f"Connected: {ident.get('vendor')} {sysinfo.get('model')}  serial {sysinfo.get('serial')}")
            say(f"BMC: {bmc.get('generation') or bmc.get('model')}  firmware {bmc.get('firmware')}")
            say(f"Power: {sysinfo.get('power_state')}   Overall: {data.get('overall')}")
            for p in data.get("problems", [])[:15]:
                say(f"   - {p.get('area')}: {p.get('component', p.get('details'))}  "
                    f"{(p.get('status') or {}).get('Health', p.get('issue', ''))}")
        except Exception:
            say(out[-1500:])
    else:
        say("Connection test FAILED:")
        say((out or "").strip()[-1500:] or (t.stderr[-1500:] if t else ""))
        ans = input("Save the configuration anyway? (y/N): ").strip().lower()
        if ans != "y":
            say("Stopped without changing Claude's config.")
            return 1

    step(5, "Registering bmc-mcp in Claude desktop")
    entry = {"command": sys.executable, "args": [SERVER],
             "env": {"BMC_HOST": host, "BMC_USERNAME": user, "BMC_PASSWORD": pw}}
    for d in config_dirs():
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "claude_desktop_config.json")
        try:
            cfg = load_config(path)
        except json.JSONDecodeError as e:
            say(f"ERROR: {path} is not valid JSON ({e}). Fix or delete it, then rerun.")
            return 1
        if os.path.exists(path):
            bak = path + time.strftime(".bak-%Y%m%d-%H%M%S")
            shutil.copy2(path, bak)
            say(f"Backup: {bak}")
        cfg.setdefault("mcpServers", {})["bmc"] = entry
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(cfg, f, indent=2)
        say(f"Updated: {path}")

    say("")
    say("=" * 60)
    say("DONE. Now fully QUIT Claude desktop (system-tray icon -> Quit, not just")
    say("closing the window) and open it again. The bmc_* tools will then be available.")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except KeyboardInterrupt:
        say("Cancelled.")
        rc = 1
    except Exception as e:  # noqa: BLE001
        say(f"Unexpected error: {type(e).__name__}: {e}")
        rc = 1
    say(f"exit code {rc}")
    save_log()
    sys.exit(rc)
