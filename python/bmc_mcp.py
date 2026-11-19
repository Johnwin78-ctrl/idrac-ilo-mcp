#!/usr/bin/env python3
"""
bmc-mcp (Python) - read-only MCP server for Dell iDRAC and HPE iLO via Redfish.

Generation-agnostic: every path is discovered by following links from the
Redfish service root (/redfish/v1), with fallbacks for older schemas
(legacy Thermal/Power vs ThermalSubsystem/PowerSubsystem, HPE "Hp" vs "Hpe"
OEM blocks, HPE SmartStorage, SimpleStorage).

Safety: the only non-GET requests ever sent are POST/DELETE to the Redfish
SessionService (to log in and log out). Nothing on the server is changed.

Configuration (environment variables):
  BMC_HOST         e.g. 192.168.0.120  or  https://idrac-r740.local
  BMC_USERNAME     a read-only iDRAC/iLO account is recommended
  BMC_PASSWORD
  BMC_VERIFY_TLS   "true" to verify the BMC certificate (default false:
                   most iDRAC/iLO use self-signed certificates)
  BMC_TIMEOUT      seconds per request (default 30)
  BMC_TARGETS_FILE optional JSON file with named targets (see README)

  BMC_USE_PROXY    "true" to route BMC traffic via HTTP(S)_PROXY (default false)

Self-test without Claude:  python bmc_mcp.py --check
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime
from typing import Any

import logging

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore

VERSION = "1.0.0"
OK_HEALTH = {None, "OK"}
SKIP_STATES = {"Absent", "Disabled", "StandbyOffline"}


def log(*a: Any) -> None:  # stdout is the MCP channel; log to stderr only
    print("[bmc-mcp]", *a, file=sys.stderr, flush=True)


def _bool(v: Any, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _clean(d: Any) -> Any:
    """Drop None/empty values so tool output stays compact."""
    if isinstance(d, dict):
        out = {k: _clean(v) for k, v in d.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(d, list):
        return [_clean(x) for x in d if x not in (None, "", [], {})]
    return d


def _get(d: Any, *path: str, default: Any = None) -> Any:
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return default
        d = d[p]
    return d


def _oem(d: dict) -> dict:
    """Return the vendor OEM block regardless of vendor/generation."""
    o = d.get("Oem") or {}
    for k in ("Dell", "Hpe", "Hp"):
        if k in o:
            return o[k] or {}
    return {}


def _status(d: dict) -> dict:
    s = d.get("Status") or {}
    return {"State": s.get("State"), "Health": s.get("Health"), "HealthRollup": s.get("HealthRollup")}


def _is_problem(d: dict) -> bool:
    s = d.get("Status") or {}
    if s.get("State") in SKIP_STATES:
        return False
    return s.get("Health") not in OK_HEALTH or s.get("HealthRollup") not in OK_HEALTH


def _odata(x: Any) -> str | None:
    return x.get("@odata.id") if isinstance(x, dict) else None


# --------------------------------------------------------------------------
# Target configuration
# --------------------------------------------------------------------------
class Target:
    def __init__(self, host: str, username: str, password: str,
                 verify_tls: bool = False, name: str | None = None):
        host = host.strip().rstrip("/")
        if not re.match(r"^https?://", host, re.I):
            host = "https://" + host
        self.base = host
        self.username = username
        self.password = password
        self.verify_tls = verify_tls
        self.name = name or host

    def public(self) -> dict:
        return {"name": self.name, "url": self.base, "username": self.username,
                "verify_tls": self.verify_tls}


def _load_targets_file() -> dict[str, dict]:
    path = os.environ.get("BMC_TARGETS_FILE")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("targets", data)
    except Exception as e:  # noqa: BLE001
        log(f"could not read BMC_TARGETS_FILE {path}: {e}")
        return {}


def _env_target() -> Target | None:
    host = os.environ.get("BMC_HOST")
    if host:
        return Target(host, os.environ.get("BMC_USERNAME", ""),
                      os.environ.get("BMC_PASSWORD", ""),
                      _bool(os.environ.get("BMC_VERIFY_TLS")), "default")
    targets = _load_targets_file()
    if targets:
        name, t = next(iter(targets.items()))
        return Target(t["host"], t.get("username", ""), t.get("password", ""),
                      _bool(t.get("verify_tls")), name)
    return None


# --------------------------------------------------------------------------
# Redfish client
# --------------------------------------------------------------------------
class RedfishError(Exception):
    pass


class Redfish:
    def __init__(self, target: Target):
        self.t = target
        self.client = httpx.AsyncClient(
            base_url=target.base, verify=target.verify_tls,
            timeout=float(os.environ.get("BMC_TIMEOUT", "30")),
            headers={"Accept": "application/json", "OData-Version": "4.0"},
            follow_redirects=True,
            # BMCs sit on a local management network: never send this traffic
            # through a system/corporate proxy unless explicitly asked to.
            trust_env=_bool(os.environ.get("BMC_USE_PROXY")))
        self.token: str | None = None
        self.session_uri: str | None = None
        self.use_basic = False
        self.sem = asyncio.Semaphore(4)  # BMCs are slow; don't hammer them
        self._root: dict | None = None
        self._cache: dict[str, Any] = {}

    async def login(self) -> None:
        if self.token or self.use_basic:
            return
        try:
            r = await self.client.post(
                "/redfish/v1/SessionService/Sessions",
                json={"UserName": self.t.username, "Password": self.t.password})
            if r.status_code in (200, 201) and r.headers.get("X-Auth-Token"):
                self.token = r.headers["X-Auth-Token"]
                loc = r.headers.get("Location") or _odata(r.json() if r.content else {})
                if loc:
                    self.session_uri = re.sub(r"^https?://[^/]+", "", loc)
                return
            if r.status_code == 401:
                raise RedfishError("Login rejected (401). Check username/password and that the "
                                   "account has Redfish/remote access enabled.")
            if r.status_code == 403:
                raise RedfishError("Access denied (403). The account may lack login privilege, or a "
                                   "firewall/proxy between this computer and the BMC blocked it.")
        except httpx.ConnectError as e:
            raise RedfishError(f"Cannot reach {self.t.base}: {e}. Is this computer on the "
                               "management network/VPN?") from e
        except httpx.TimeoutException as e:
            raise RedfishError(f"Timed out connecting to {self.t.base}.") from e
        except httpx.HTTPError as e:
            raise RedfishError(f"Connection to {self.t.base} failed: {type(e).__name__}: {e}") from e
        # Session service unavailable -> fall back to HTTP Basic auth
        self.use_basic = True

    async def logout(self) -> None:
        if self.token and self.session_uri:
            try:
                await self.client.delete(self.session_uri, headers={"X-Auth-Token": self.token})
            except Exception:  # noqa: BLE001
                pass
        self.token = self.session_uri = None
        self.use_basic = False
        await self.client.aclose()

    async def get(self, path: str, _retry: bool = True) -> dict:
        path = re.sub(r"^https?://[^/]+", "", path)
        if path in self._cache:
            return self._cache[path]
        await self.login()
        kw: dict[str, Any] = {}
        if self.token:
            kw["headers"] = {"X-Auth-Token": self.token}
        else:
            kw["auth"] = (self.t.username, self.t.password)
        async with self.sem:
            try:
                r = await self.client.get(path, **kw)
            except httpx.ConnectError as e:
                raise RedfishError(f"Cannot reach {self.t.base}: {e}") from e
            except httpx.TimeoutException as e:
                raise RedfishError(f"Timeout on GET {path}") from e
            except httpx.HTTPError as e:
                raise RedfishError(f"GET {path} failed: {type(e).__name__}: {e}") from e
        if r.status_code == 401 and self.token and _retry:  # session expired
            self.token = None
            return await self.get(path, _retry=False)
        if r.status_code >= 400:
            msg = ""
            try:
                msg = json.dumps(r.json().get("error", {}).get("@Message.ExtendedInfo", ""))[:300]
            except Exception:  # noqa: BLE001
                msg = r.text[:200]
            raise RedfishError(f"GET {path} -> HTTP {r.status_code} {msg}")
        data = r.json()
        self._cache[path] = data
        return data

    async def try_get(self, path: str | None) -> dict | None:
        if not path:
            return None
        try:
            return await self.get(path)
        except RedfishError:
            return None

    async def root(self) -> dict:
        if self._root is None:
            self._root = await self.get("/redfish/v1/")
        return self._root

    async def members(self, coll_path: str | None, limit: int = 500) -> list[dict]:
        """Fetch every member resource of a collection (follows nextLink)."""
        if not coll_path:
            return []
        coll = await self.try_get(coll_path)
        if not coll:
            return []
        refs = list(coll.get("Members", []))
        nxt = coll.get("Members@odata.nextLink")
        while nxt and len(refs) < limit:
            page = await self.try_get(nxt)
            if not page:
                break
            refs += page.get("Members", [])
            nxt = page.get("Members@odata.nextLink")
        refs = refs[:limit]

        async def load(m: dict) -> dict | None:
            if len(m) > 1:  # already expanded
                return m
            return await self.try_get(_odata(m))
        res = await asyncio.gather(*(load(m) for m in refs))
        return [r for r in res if r]

    async def systems(self) -> list[dict]:
        return await self.members(_odata((await self.root()).get("Systems")))

    async def chassis(self) -> list[dict]:
        return await self.members(_odata((await self.root()).get("Chassis")))

    async def managers(self) -> list[dict]:
        return await self.members(_odata((await self.root()).get("Managers")))


# --------------------------------------------------------------------------
# Collectors (vendor/generation tolerant)
# --------------------------------------------------------------------------
def vendor_of(root: dict, mgr: dict | None) -> str:
    v = (root.get("Vendor") or "").lower()
    o = root.get("Oem") or {}
    model = ((mgr or {}).get("Model") or "") + " " + ((mgr or {}).get("Name") or "")
    if "dell" in v or "Dell" in o or "idrac" in model.lower():
        return "Dell"
    if "hpe" in v or "hp" == v or "Hpe" in o or "Hp" in o or "ilo" in model.lower():
        return "HPE"
    return root.get("Vendor") or "Unknown"


async def collect_identity(rf: Redfish) -> dict:
    root = await rf.root()
    systems, managers = await asyncio.gather(rf.systems(), rf.managers())
    mgr = managers[0] if managers else {}
    vendor = vendor_of(root, mgr)
    out: dict[str, Any] = {
        "target": rf.t.public(),
        "vendor": vendor,
        "redfish_version": root.get("RedfishVersion"),
        "bmc": {
            "type": "iDRAC" if vendor == "Dell" else "iLO" if vendor == "HPE" else "BMC",
            "model": mgr.get("Model"),
            "firmware": mgr.get("FirmwareVersion"),
            "status": _status(mgr),
        },
        "systems": [],
    }
    # iLO exposes the generation in Oem.Hpe.Manager[0].ManagerType ("iLO 5")
    mo = _oem(mgr)
    mtype = _get(mo, "Manager", default=[{}])
    if isinstance(mtype, list) and mtype and isinstance(mtype[0], dict):
        out["bmc"]["generation"] = mtype[0].get("ManagerType")
        out["bmc"]["firmware_detail"] = _get(mtype[0], "ManagerFirmwareVersion") or \
            _get(mtype[0], "Languages")
    for s in systems:
        out["systems"].append({
            "id": s.get("Id"),
            "manufacturer": s.get("Manufacturer"),
            "model": s.get("Model"),
            "serial": s.get("SerialNumber"),
            "service_tag_or_sku": s.get("SKU"),
            "hostname": s.get("HostName"),
            "bios": s.get("BiosVersion"),
            "power_state": s.get("PowerState"),
            "status": _status(s),
        })
    return _clean(out)


async def collect_thermal(rf: Redfish) -> dict:
    out: list[dict] = []
    for ch in await rf.chassis():
        entry: dict[str, Any] = {"chassis": ch.get("Id"), "temperatures": [], "fans": []}
        th = await rf.try_get(_odata(ch.get("Thermal")))
        if th:
            for t in th.get("Temperatures", []):
                if _get(t, "Status", "State") == "Absent":
                    continue
                entry["temperatures"].append({
                    "name": t.get("Name"), "reading_c": t.get("ReadingCelsius"),
                    "warn_c": t.get("UpperThresholdNonCritical"),
                    "critical_c": t.get("UpperThresholdCritical"),
                    "fatal_c": t.get("UpperThresholdFatal"), "status": _status(t)})
            for f in th.get("Fans", []):
                if _get(f, "Status", "State") == "Absent":
                    continue
                entry["fans"].append({
                    "name": f.get("Name") or f.get("FanName") or f.get("MemberId"),
                    "reading": f.get("Reading") if f.get("Reading") is not None else f.get("CurrentReading"),
                    "units": f.get("ReadingUnits") or f.get("Units"),
                    "status": _status(f)})
        else:  # newer schema
            ts = await rf.try_get(_odata(ch.get("ThermalSubsystem")))
            if ts:
                for f in await rf.members(_odata(ts.get("Fans"))):
                    entry["fans"].append({
                        "name": f.get("Name"), "reading": _get(f, "SpeedPercent", "Reading"),
                        "units": "Percent", "status": _status(f)})
            for s in await rf.members(_odata(ch.get("Sensors"))):
                if s.get("ReadingType") == "Temperature":
                    entry["temperatures"].append({
                        "name": s.get("Name"), "reading_c": s.get("Reading"),
                        "critical_c": _get(s, "Thresholds", "UpperCritical", "Reading"),
                        "status": _status(s)})
        if entry["temperatures"] or entry["fans"]:
            out.append(entry)
    return {"chassis": _clean(out)}


async def collect_power(rf: Redfish) -> dict:
    out: list[dict] = []
    for ch in await rf.chassis():
        entry: dict[str, Any] = {"chassis": ch.get("Id"), "power_supplies": []}
        pw = await rf.try_get(_odata(ch.get("Power")))
        if pw:
            for p in pw.get("PowerSupplies", []):
                entry["power_supplies"].append({
                    "name": p.get("Name") or p.get("MemberId"), "model": p.get("Model"),
                    "serial": p.get("SerialNumber"), "part": p.get("PartNumber") or p.get("SparePartNumber"),
                    "firmware": p.get("FirmwareVersion"),
                    "capacity_w": p.get("PowerCapacityWatts"),
                    "output_w": p.get("LastPowerOutputWatts"),
                    "input_v": p.get("LineInputVoltage"), "status": _status(p),
                    "oem": _clean({k: v for k, v in _oem(p).items()
                                   if k in ("PowerSupplyStatus", "BayNumber", "HotplugCapable",
                                            "Mismatched", "AveragePowerOutputWatts")})})
            pc = (pw.get("PowerControl") or [{}])[0]
            entry["consumed_w"] = pc.get("PowerConsumedWatts")
            entry["redundancy"] = [{"name": r.get("Name"), "mode": r.get("Mode"),
                                    "status": _status(r)} for r in pw.get("Redundancy", [])]
            entry["voltage_problems"] = [
                {"name": v.get("Name"), "reading_v": v.get("ReadingVolts"), "status": _status(v)}
                for v in pw.get("Voltages", []) if _is_problem(v)]
        else:
            ps = await rf.try_get(_odata(ch.get("PowerSubsystem")))
            if ps:
                for p in await rf.members(_odata(ps.get("PowerSupplies"))):
                    entry["power_supplies"].append({
                        "name": p.get("Name"), "model": p.get("Model"), "serial": p.get("SerialNumber"),
                        "firmware": p.get("FirmwareVersion"),
                        "capacity_w": p.get("PowerCapacityWatts"), "status": _status(p)})
        if entry["power_supplies"] or entry.get("consumed_w"):
            out.append(entry)
    return {"chassis": _clean(out)}


async def collect_inventory(rf: Redfish) -> dict:
    systems = await rf.systems()
    out: list[dict] = []
    for s in systems:
        cpus = await rf.members(_odata(s.get("Processors")))
        mem = await rf.members(_odata(s.get("Memory")))
        dimms = []
        for m in mem:
            dstat = _get(_oem(m), "DIMMStatus") or m.get("DIMMStatus")  # iLO 4/5
            if _get(m, "Status", "State") == "Absent" or dstat == "NotPresent" or \
                    (m.get("CapacityMiB") in (None, 0) and not m.get("SizeMB")):
                continue
            dimms.append({"slot": m.get("DeviceLocator") or m.get("Name") or m.get("Id"),
                          "capacity_mib": m.get("CapacityMiB") or m.get("SizeMB"),
                          "speed_mhz": m.get("OperatingSpeedMhz") or m.get("MaximumFrequencyMHz"),
                          "type": m.get("MemoryDeviceType") or m.get("DIMMType"),
                          "part": m.get("PartNumber"), "manufacturer": m.get("Manufacturer"),
                          "hpe_dimm_status": dstat, "status": _status(m)})
        out.append({
            "system": s.get("Id"),
            "processor_summary": s.get("ProcessorSummary"),
            "memory_summary": s.get("MemorySummary"),
            "processors": [{"socket": c.get("Socket") or c.get("Id"), "model": c.get("Model"),
                            "cores": c.get("TotalCores"), "threads": c.get("TotalThreads"),
                            "status": _status(c)}
                           for c in cpus if _get(c, "Status", "State") != "Absent"],
            "memory": dimms,
        })
    nics = []
    for ch in await rf.chassis():
        for n in await rf.members(_odata(ch.get("NetworkAdapters"))):
            nics.append({"name": n.get("Name") or n.get("Id"), "model": n.get("Model"),
                         "manufacturer": n.get("Manufacturer"), "status": _status(n)})
    return _clean({"systems": out, "network_adapters": nics})


async def collect_storage(rf: Redfish) -> dict:
    out: list[dict] = []
    for s in await rf.systems():
        sysentry: dict[str, Any] = {"system": s.get("Id"), "controllers": []}
        for st in await rf.members(_odata(s.get("Storage"))):
            ctrls = st.get("StorageControllers") or []
            drives = await asyncio.gather(*(rf.try_get(_odata(d)) for d in st.get("Drives", [])))
            vols = await rf.members(_odata(st.get("Volumes")))
            sysentry["controllers"].append({
                "id": st.get("Id"), "name": st.get("Name"),
                "model": (ctrls[0].get("Model") if ctrls else None),
                "firmware": (ctrls[0].get("FirmwareVersion") if ctrls else None),
                "status": _status(st),
                "drives": [_drive(d) for d in drives if d],
                "volumes": [{"name": v.get("Name") or v.get("Id"),
                             "raid": v.get("RAIDType") or v.get("VolumeType"),
                             "capacity_gb": _gb(v.get("CapacityBytes")),
                             "status": _status(v),
                             "operations": v.get("Operations")} for v in vols],
            })
        # HPE SmartStorage (iLO 4/5 when Storage is empty or absent)
        if not sysentry["controllers"]:
            ss_link = _odata(_get(_oem(s), "Links", "SmartStorage"))
            ss = await rf.try_get(ss_link)
            if ss:
                for ac in await rf.members(_odata(_get(ss, "Links", "ArrayControllers"))):
                    pds = await rf.members(_odata(_get(ac, "Links", "PhysicalDrives")))
                    lds = await rf.members(_odata(_get(ac, "Links", "LogicalDrives")))
                    sysentry["controllers"].append({
                        "id": ac.get("Id"), "name": ac.get("Model"), "model": ac.get("Model"),
                        "firmware": _get(ac, "FirmwareVersion", "Current", "VersionString"),
                        "location": ac.get("Location"), "status": _status(ac),
                        "cache_status": _get(ac, "CacheModuleStatus", "Health"),
                        "drives": [{"name": p.get("Location") or p.get("Id"), "model": p.get("Model"),
                                    "serial": p.get("SerialNumber"), "media": p.get("MediaType"),
                                    "capacity_gb": round(p["CapacityMiB"] / 953.674, 1) if p.get("CapacityMiB") else p.get("CapacityGB"),
                                    "status": _status(p),
                                    "ssd_life_left_pct": p.get("SSDEnduranceUtilizationPercentage") and
                                    100 - p["SSDEnduranceUtilizationPercentage"]} for p in pds],
                        "volumes": [{"name": l.get("LogicalDriveName") or l.get("Id"),
                                     "raid": l.get("Raid"), "capacity_gb": round(l["CapacityMiB"] / 953.674, 1) if l.get("CapacityMiB") else None,
                                     "status": _status(l)} for l in lds],
                    })
        if not sysentry["controllers"]:
            for ss in await rf.members(_odata(s.get("SimpleStorage"))):
                sysentry["controllers"].append({
                    "id": ss.get("Id"), "name": ss.get("Name"), "status": _status(ss),
                    "drives": [{"name": d.get("Name"), "model": d.get("Model"),
                                "capacity_gb": _gb(d.get("CapacityBytes")), "status": _status(d)}
                               for d in ss.get("Devices", [])]})
        if not sysentry["controllers"]:
            sysentry["note"] = ("No storage data returned. On HPE, storage inventory is only "
                                "populated after the server has completed POST; also check "
                                "the controller is supported by the BMC (some HBAs/NVMe are not).")
        out.append(sysentry)
    return _clean({"systems": out})


def _gb(b: Any) -> float | None:
    return round(b / 1e9, 1) if isinstance(b, (int, float)) and b else None


def _drive(d: dict) -> dict:
    return {"name": d.get("Name") or d.get("Id"), "model": d.get("Model"),
            "serial": d.get("SerialNumber"), "media": d.get("MediaType"),
            "protocol": d.get("Protocol"), "capacity_gb": _gb(d.get("CapacityBytes")),
            "failure_predicted": d.get("FailurePredicted"),
            "life_left_pct": d.get("PredictedMediaLifeLeftPercent"),
            "firmware": d.get("Revision"), "status": _status(d),
            "operations": d.get("Operations")}


async def collect_firmware(rf: Redfish) -> dict:
    root = await rf.root()
    inv_link = None
    us = await rf.try_get(_odata(root.get("UpdateService")))
    if us:
        inv_link = _odata(us.get("FirmwareInventory"))
    items = await rf.members(inv_link) if inv_link else []
    if not items:  # iLO 4 keeps it under the system OEM links
        for s in await rf.systems():
            link = _odata(_get(_oem(s), "Links", "FirmwareInventory"))
            fw = await rf.try_get(link)
            if fw and isinstance(fw.get("Current"), dict):
                for group in fw["Current"].values():
                    for f in group:
                        items.append({"Name": f.get("Name"),
                                      "Version": f.get("VersionString"),
                                      "Id": f.get("Key")})
    out = []
    for f in items:
        fid = f.get("Id") or ""
        if fid.startswith("Previous"):  # Dell keeps rollback images
            continue
        out.append({"name": f.get("Name"), "version": f.get("Version"),
                    "updateable": f.get("Updateable"), "id": fid,
                    "status": _status(f)})
    out.sort(key=lambda x: (x.get("name") or ""))
    return _clean({"firmware": out, "count": len(out)})


SEV_RANK = {"OK": 0, "Informational": 0, "Warning": 1, "Critical": 2}


async def list_log_services(rf: Redfish) -> list[dict]:
    out = []
    for kind, res in (("Manager", await rf.managers()), ("System", await rf.systems())):
        for r in res:
            for ls in await rf.members(_odata(r.get("LogServices"))):
                out.append({"id": ls.get("Id"), "name": ls.get("Name"), "on": kind,
                            "path": _odata(ls), "entries": _odata(ls.get("Entries")),
                            "status": _status(ls)})
    return out


def _parse_time(s: Any) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


async def read_log(rf: Redfish, entries_path: str, max_entries: int, min_sev: int) -> list[dict]:
    """Return newest-first entries. Handles BMCs that list oldest-first."""
    first = await rf.get(entries_path)
    total = first.get("Members@odata.count")
    members = list(first.get("Members", []))
    nxt = first.get("Members@odata.nextLink")
    cap = 3000
    ts = [_parse_time(m.get("Created")) for m in members if m.get("Created")]
    oldest_first = len(ts) >= 2 and ts[0] < ts[-1]
    # Oldest-first and paged (e.g. HPE IML): the newest entries are on the last
    # pages. Try to jump near the end with $skip, then read to the end.
    if nxt and oldest_first and isinstance(total, int) and total > len(members):
        sep = "&" if "?" in entries_path else "?"
        want = max(max_entries * 2, 100) if min_sev == 0 else cap
        tail = await rf.try_get(f"{entries_path}{sep}$skip={max(0, total - want)}")
        if tail and tail.get("Members"):
            members, nxt = list(tail["Members"]), tail.get("Members@odata.nextLink")
    while nxt and len(members) < cap and (oldest_first or min_sev > 0 or len(members) < max_entries * 2):
        page = await rf.try_get(nxt)
        if not page:
            break
        members += page.get("Members", [])
        nxt = page.get("Members@odata.nextLink")
    entries = []
    for m in members:
        sev = m.get("Severity") or m.get("EntryType")
        if SEV_RANK.get(sev, 0) < min_sev:
            continue
        o = _oem(m)
        entries.append(_clean({
            "created": m.get("Created"), "severity": sev, "message": m.get("Message"),
            "message_id": m.get("MessageId"), "sensor": m.get("SensorType"),
            "entry_code": m.get("EntryCode"), "id": m.get("Id"),
            "hpe_class": o.get("Class"), "hpe_code": o.get("Code"),
            "repaired": o.get("Repaired"), "count": o.get("Count"),
            "updated": o.get("Updated"), "dell_category": o.get("Category"),
        }))
    entries.sort(key=lambda e: _parse_time(e.get("created")), reverse=True)
    return entries[:max_entries]


async def collect_problems(rf: Redfish) -> list[dict]:
    """Every component whose Health/HealthRollup is not OK."""
    probs: list[dict] = []
    th, pw, inv, st = await asyncio.gather(
        collect_thermal(rf), collect_power(rf), collect_inventory(rf), collect_storage(rf),
        return_exceptions=True)

    def add(area: str, name: Any, status: dict | None, **extra: Any) -> None:
        if status and (status.get("Health") not in OK_HEALTH or
                       status.get("HealthRollup") not in OK_HEALTH) \
                and status.get("State") not in SKIP_STATES:
            probs.append(_clean({"area": area, "component": name, "status": status, **extra}))
    if isinstance(th, dict):
        for c in th.get("chassis", []):
            for t in c.get("temperatures", []):
                add("Temperature", t.get("name"), t.get("status"), reading_c=t.get("reading_c"))
            for f in c.get("fans", []):
                add("Fan", f.get("name"), f.get("status"), reading=f.get("reading"))
    if isinstance(pw, dict):
        for c in pw.get("chassis", []):
            for p in c.get("power_supplies", []):
                add("PSU", p.get("name"), p.get("status"))
            for r in c.get("redundancy", []):
                add("PSU redundancy", r.get("name"), r.get("status"))
            for v in c.get("voltage_problems", []):
                add("Voltage", v.get("name"), v.get("status"), reading_v=v.get("reading_v"))
    if isinstance(inv, dict):
        for s in inv.get("systems", []):
            for c in s.get("processors", []):
                add("CPU", c.get("socket"), c.get("status"))
            for m in s.get("memory", []):
                add("Memory", m.get("slot"), m.get("status"))
    if isinstance(st, dict):
        for s in st.get("systems", []):
            for c in s.get("controllers", []):
                add("Storage controller", c.get("name") or c.get("id"), c.get("status"))
                for d in c.get("drives", []):
                    add("Drive", d.get("name"), d.get("status"), serial=d.get("serial"))
                    if d.get("failure_predicted"):
                        probs.append({"area": "Drive", "component": d.get("name"),
                                      "issue": "Predictive failure flagged"})
                for v in c.get("volumes", []):
                    add("Volume", v.get("name"), v.get("status"))
    errors = [f"{n}: {e}" for n, e in zip(("thermal", "power", "inventory", "storage"),
                                          (th, pw, inv, st)) if isinstance(e, Exception)]
    if errors:
        probs.append({"area": "collector-errors", "details": errors})
    return probs


async def collect_health(rf: Redfish) -> dict:
    ident = await collect_identity(rf)
    systems = await rf.systems()
    rollups = []
    for s in systems:
        o = _oem(s)
        r: dict[str, Any] = {"system": s.get("Id"), "status": _status(s)}
        agg = o.get("AggregateHealthStatus")  # HPE iLO 5/6
        if isinstance(agg, dict):
            r["hpe_aggregate_health"] = {k: (_get(v, "Status", "Health") if isinstance(v, dict) else v)
                                         for k, v in agg.items()}
        ds = o.get("DellSystem")  # Dell iDRAC 9+
        if isinstance(ds, dict):
            r["dell_rollups"] = {k: v for k, v in ds.items()
                                 if "Status" in k and isinstance(v, str)}
        r["processor_summary_health"] = _get(s, "ProcessorSummary", "Status", "HealthRollup") or \
            _get(s, "ProcessorSummary", "Status", "Health")
        r["memory_summary_health"] = _get(s, "MemorySummary", "Status", "HealthRollup") or \
            _get(s, "MemorySummary", "Status", "Health")
        rollups.append(r)
    chassis = [{"chassis": c.get("Id"), "status": _status(c),
                "intrusion": _get(c, "PhysicalSecurity", "IntrusionSensor")}
               for c in await rf.chassis()]
    problems = await collect_problems(rf)
    real = [p for p in problems if p.get("area") != "collector-errors"]
    return _clean({
        "identity": ident, "rollups": rollups, "chassis": chassis,
        "overall": "OK" if not real else f"{len(real)} component(s) not OK",
        "problems": problems,
    })


# --------------------------------------------------------------------------
# MCP server
# --------------------------------------------------------------------------
mcp = _Server("bmc-mcp", instructions=(
    "Read-only access to a Dell iDRAC or HPE iLO over Redfish. Start with bmc_health_summary, "
    "then drill into event logs, thermal, power, storage, inventory or firmware. "
    "Nothing in this server changes the server's state."))

_state: dict[str, Any] = {"target": None}


def _target() -> Target:
    if _state["target"] is None:
        _state["target"] = _env_target()
    if _state["target"] is None:
        raise RedfishError("No BMC configured. Set BMC_HOST/BMC_USERNAME/BMC_PASSWORD in the "
                           "MCP config, or call bmc_set_target.")
    return _state["target"]


async def _run(fn, *a, **kw) -> dict:
    try:
        rf = Redfish(_target())
    except RedfishError as e:
        return {"error": str(e)}
    try:
        return await fn(rf, *a, **kw)
    except RedfishError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        await rf.logout()


@mcp.tool()
async def bmc_set_target(host: str = "", username: str = "", password: str = "",
                         verify_tls: bool = False, name: str = "") -> dict:
    """Point the server at a different iDRAC/iLO for this session.
    Either give host/username/password, or give `name` of an entry in BMC_TARGETS_FILE.
    Then verifies the connection and returns what was found."""
    if name and not host:
        t = _load_targets_file().get(name)
        if not t:
            return {"error": f"No target named '{name}' in BMC_TARGETS_FILE",
                    "available": list(_load_targets_file())}
        _state["target"] = Target(t["host"], t.get("username", ""), t.get("password", ""),
                                  _bool(t.get("verify_tls")), name)
    elif host:
        _state["target"] = Target(host, username, password, verify_tls, name or None)
    else:
        return {"error": "Provide host (+ username/password) or name."}
    return await _run(collect_identity)


@mcp.tool()
async def bmc_list_targets() -> dict:
    """Show the current target and any named targets in BMC_TARGETS_FILE (passwords hidden)."""
    cur = None
    try:
        cur = _target().public()
    except RedfishError:
        pass
    return {"current": cur,
            "named_targets": {k: {"host": v.get("host"), "username": v.get("username")}
                              for k, v in _load_targets_file().items()}}


@mcp.tool()
async def bmc_identify() -> dict:
    """Vendor (Dell/HPE), BMC type/generation/firmware, server model, serial/service tag, BIOS, power state."""
    return await _run(collect_identity)


@mcp.tool()
async def bmc_health_summary() -> dict:
    """Start here. Overall health rollups (Dell rollup statuses / HPE AggregateHealthStatus) plus
    a list of every component that is not OK across fans, temperatures, PSUs, voltages,
    CPUs, DIMMs, controllers, drives and volumes."""
    return await _run(collect_health)


@mcp.tool()
async def bmc_list_logs() -> dict:
    """List the log services this BMC exposes (e.g. Dell Sel/Lclog, HPE IML/IEL/SL)."""
    async def f(rf: Redfish) -> dict:
        return {"logs": _clean(await list_log_services(rf))}
    return await _run(f)


@mcp.tool()
async def bmc_get_event_logs(log: str = "", max_entries: int = 50,
                             min_severity: str = "all") -> dict:
    """Read event log entries, newest first.
    log: log service Id or name (e.g. "Sel", "Lclog", "IML", "IEL"). Empty = the main hardware
         logs (Dell SEL + Lifecycle, HPE IML + iLO Event Log).
    max_entries: per log (default 50, max 500).
    min_severity: "all", "warning" (Warning+Critical) or "critical"."""
    min_sev = {"all": 0, "warning": 1, "critical": 2}.get(min_severity.lower(), 0)
    max_entries = max(1, min(int(max_entries), 500))

    async def f(rf: Redfish) -> dict:
        services = await list_log_services(rf)
        if log:
            chosen = [s for s in services if log.lower() in
                      ((s.get("id") or "") + " " + (s.get("name") or "")).lower()]
        else:
            pref = re.compile(r"^(sel|lclog|iml|iel|eventlog|log1?)$", re.I)
            chosen = [s for s in services if pref.match(s.get("id") or "")] or services
        if not chosen:
            return {"error": f"No log matching '{log}'",
                    "available": [s.get("id") for s in services]}
        out = []
        for s in chosen:
            if not s.get("entries"):
                continue
            try:
                ents = await read_log(rf, s["entries"], max_entries, min_sev)
                out.append({"log": s.get("id"), "name": s.get("name"), "on": s.get("on"),
                            "returned": len(ents), "entries": ents})
            except RedfishError as e:
                out.append({"log": s.get("id"), "error": str(e)})
        return {"logs": out}
    return await _run(f)


@mcp.tool()
async def bmc_get_thermal() -> dict:
    """All temperature sensors (with warning/critical thresholds) and fans (speed, status)."""
    return await _run(collect_thermal)


@mcp.tool()
async def bmc_get_power() -> dict:
    """Power supplies (model, firmware, capacity, output, input voltage, status), power
    consumption, PSU redundancy, and any voltage sensors that are not OK."""
    return await _run(collect_power)


@mcp.tool()
async def bmc_get_storage() -> dict:
    """RAID/HBA controllers, physical drives (status, predictive failure, SSD life left) and
    virtual disks/logical drives (RAID level, status, rebuild/background operations)."""
    return await _run(collect_storage)


@mcp.tool()
async def bmc_get_inventory() -> dict:
    """CPUs, populated DIMMs (slot, size, speed, part, status) and network adapters."""
    return await _run(collect_inventory)


@mcp.tool()
async def bmc_get_firmware() -> dict:
    """Installed firmware versions: BIOS, BMC, controllers, NICs, drives, PSUs, CPLD, etc."""
    return await _run(collect_firmware)


@mcp.tool()
async def bmc_redfish_get(path: str) -> dict:
    """Raw read-only GET of any Redfish resource, for anything the other tools don't cover.
    path must start with /redfish/ (e.g. /redfish/v1/Managers/iDRAC.Embedded.1/NetworkProtocol)."""
    if not path.startswith("/redfish/"):
        return {"error": "path must start with /redfish/"}

    async def f(rf: Redfish) -> dict:
        data = await rf.get(path)
        text = json.dumps(data)
        if len(text) > 60000:
            return {"truncated": True, "size": len(text), "data_head": text[:60000]}
        return data
    return await _run(f)


# --------------------------------------------------------------------------
async def _check() -> None:
    t = _target()
    print(f"Connecting to {t.base} as {t.username} ...")
    rf = Redfish(t)
    try:
        print(json.dumps(await collect_health(rf), indent=2))
    finally:
        await rf.logout()


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(VERSION)
    elif "--check" in sys.argv:
        try:
            asyncio.run(_check())
        except RedfishError as e:
            print(f"ERROR: {e}")
            sys.exit(1)
    else:
        mcp.run()
