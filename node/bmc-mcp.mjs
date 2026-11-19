#!/usr/bin/env node
/**
 * bmc-mcp (Node) - read-only MCP server for Dell iDRAC and HPE iLO via Redfish.
 * Same tools and behaviour as the Python version (python/bmc_mcp.py).
 *
 * Generation-agnostic: every path is discovered from the Redfish service root,
 * with fallbacks for older schemas (legacy Thermal/Power vs ThermalSubsystem/
 * PowerSubsystem, HPE "Hp" vs "Hpe" OEM, HPE SmartStorage, SimpleStorage).
 *
 * Safety: the only non-GET requests are POST/DELETE to the Redfish
 * SessionService (log in / log out). Nothing on the server is changed.
 *
 * Env: BMC_HOST, BMC_USERNAME, BMC_PASSWORD, BMC_VERIFY_TLS (default false),
 *      BMC_TIMEOUT (s, default 30), BMC_TARGETS_FILE, BMC_USE_PROXY (default false)
 * Self-test without Claude:  node bmc-mcp.mjs --check
 */
import fs from "node:fs";
import { Agent, ProxyAgent, fetch as ufetch } from "undici";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const VERSION = "1.0.0";
const OK_HEALTH = new Set([undefined, null, "OK"]);
const SKIP_STATES = new Set(["Absent", "Disabled", "StandbyOffline"]);
const log = (...a) => console.error("[bmc-mcp]", ...a); // stdout is the MCP channel

const bool = (v, d = false) =>
  v === undefined || v === null || v === "" ? d : ["1", "true", "yes", "on"].includes(String(v).trim().toLowerCase());

function clean(d) {
  const empty = (v) => v === null || v === undefined || v === "" ||
    (Array.isArray(v) && v.length === 0) ||
    (v && typeof v === "object" && !Array.isArray(v) && Object.keys(v).length === 0);
  if (Array.isArray(d)) return d.map(clean).filter((x) => !empty(x));
  if (d && typeof d === "object") {
    const o = {};
    for (const [k, v] of Object.entries(d)) { const c = clean(v); if (!empty(c)) o[k] = c; }
    return o;
  }
  return d;
}
const get = (d, ...p) => { for (const k of p) { if (!d || typeof d !== "object" || !(k in d)) return undefined; d = d[k]; } return d; };
const oem = (d) => { const o = d?.Oem || {}; for (const k of ["Dell", "Hpe", "Hp"]) if (k in o) return o[k] || {}; return {}; };
const status = (d) => { const s = d?.Status || {}; return { State: s.State, Health: s.Health, HealthRollup: s.HealthRollup }; };
const isProblem = (st) => st && !SKIP_STATES.has(st.State) && (!OK_HEALTH.has(st.Health) || !OK_HEALTH.has(st.HealthRollup));
const odata = (x) => (x && typeof x === "object" ? x["@odata.id"] : undefined);
const gb = (b) => (typeof b === "number" && b ? Math.round(b / 1e8) / 10 : undefined);
const mibToGb = (m) => (m ? Math.round(m / 953.674 * 10) / 10 : undefined);
const ptime = (s) => { const t = Date.parse(s || ""); return isNaN(t) ? 0 : t; };

class RedfishError extends Error {}

// ---------------------------------------------------------------- targets
class Target {
  constructor(host, username, password, verifyTls = false, name) {
    host = String(host).trim().replace(/\/+$/, "");
    if (!/^https?:\/\//i.test(host)) host = "https://" + host;
    Object.assign(this, { base: host, username, password, verifyTls, name: name || host });
  }
  public() { return { name: this.name, url: this.base, username: this.username, verify_tls: this.verifyTls }; }
}
function loadTargetsFile() {
  const p = process.env.BMC_TARGETS_FILE;
  if (!p) return {};
  try { const d = JSON.parse(fs.readFileSync(p, "utf8")); return d.targets || d; }
  catch (e) { log(`could not read BMC_TARGETS_FILE ${p}: ${e.message}`); return {}; }
}
function envTarget() {
  const e = process.env;
  if (e.BMC_HOST) return new Target(e.BMC_HOST, e.BMC_USERNAME || "", e.BMC_PASSWORD || "", bool(e.BMC_VERIFY_TLS), "default");
  const t = loadTargetsFile(); const k = Object.keys(t)[0];
  return k ? new Target(t[k].host, t[k].username || "", t[k].password || "", bool(t[k].verify_tls), k) : null;
}

// ---------------------------------------------------------------- client
class Redfish {
  constructor(t) {
    this.t = t;
    this.timeout = Number(process.env.BMC_TIMEOUT || 30) * 1000;
    const proxy = process.env.HTTPS_PROXY || process.env.https_proxy;
    this.dispatcher = bool(process.env.BMC_USE_PROXY) && proxy
      ? new ProxyAgent({ uri: proxy, requestTls: { rejectUnauthorized: t.verifyTls } })
      : new Agent({ connect: { rejectUnauthorized: t.verifyTls } });
    this.token = null; this.sessionUri = null; this.useBasic = false;
    this.cache = new Map(); this._root = null;
    this.active = 0; this.queue = []; // simple concurrency limit (4)
  }
  async _slot() { if (this.active >= 4) await new Promise((r) => this.queue.push(r)); this.active++; }
  _release() { this.active--; const n = this.queue.shift(); if (n) n(); }

  async _fetch(path, opts = {}) {
    const url = /^https?:\/\//i.test(path) ? path : this.t.base + path;
    try {
      return await ufetch(url, { ...opts, dispatcher: this.dispatcher, signal: AbortSignal.timeout(this.timeout),
        headers: { Accept: "application/json", "OData-Version": "4.0", ...(opts.headers || {}) } });
    } catch (e) {
      const c = e.cause?.code || e.name;
      if (c === "TimeoutError" || c === "UND_ERR_CONNECT_TIMEOUT") throw new RedfishError(`Timed out connecting to ${this.t.base}.`);
      throw new RedfishError(`Cannot reach ${this.t.base}: ${c} ${e.cause?.message || e.message}. Is this computer on the management network/VPN?`);
    }
  }
  async login() {
    if (this.token || this.useBasic) return;
    const r = await this._fetch("/redfish/v1/SessionService/Sessions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ UserName: this.t.username, Password: this.t.password }) });
    const tok = r.headers.get("x-auth-token");
    if ((r.status === 200 || r.status === 201) && tok) {
      this.token = tok;
      let loc = r.headers.get("location");
      if (!loc) { try { loc = odata(await r.json()); } catch { /* ignore */ } }
      if (loc) this.sessionUri = loc.replace(/^https?:\/\/[^/]+/, "");
      return;
    }
    await r.text().catch(() => {});
    if (r.status === 401) throw new RedfishError("Login rejected (401). Check username/password and that the account has Redfish/remote access enabled.");
    if (r.status === 403) throw new RedfishError("Access denied (403). The account may lack login privilege, or a firewall/proxy between this computer and the BMC blocked it.");
    this.useBasic = true; // session service unavailable -> HTTP Basic
  }
  async logout() {
    if (this.token && this.sessionUri) {
      try { const r = await this._fetch(this.sessionUri, { method: "DELETE", headers: { "X-Auth-Token": this.token } }); await r.text(); } catch { /* ignore */ }
    }
    this.token = this.sessionUri = null;
    await this.dispatcher.close().catch(() => {});
  }
  async get(path, retry = true) {
    path = path.replace(/^https?:\/\/[^/]+/, "");
    if (this.cache.has(path)) return this.cache.get(path);
    await this.login();
    const headers = this.token ? { "X-Auth-Token": this.token }
      : { Authorization: "Basic " + Buffer.from(`${this.t.username}:${this.t.password}`).toString("base64") };
    await this._slot();
    let r;
    try { r = await this._fetch(path, { headers }); } finally { this._release(); }
    if (r.status === 401 && this.token && retry) { await r.text(); this.token = null; return this.get(path, false); }
    if (r.status >= 400) {
      let msg = ""; try { msg = JSON.stringify((await r.json())?.error?.["@Message.ExtendedInfo"] || "").slice(0, 300); } catch { /* ignore */ }
      throw new RedfishError(`GET ${path} -> HTTP ${r.status} ${msg}`);
    }
    const data = await r.json();
    this.cache.set(path, data);
    return data;
  }
  async tryGet(p) { if (!p) return null; try { return await this.get(p); } catch (e) { if (e instanceof RedfishError) return null; throw e; } }
  async root() { if (!this._root) this._root = await this.get("/redfish/v1/"); return this._root; }
  async members(collPath, limit = 500) {
    if (!collPath) return [];
    const coll = await this.tryGet(collPath); if (!coll) return [];
    let refs = [...(coll.Members || [])]; let nxt = coll["Members@odata.nextLink"];
    while (nxt && refs.length < limit) { const pg = await this.tryGet(nxt); if (!pg) break; refs.push(...(pg.Members || [])); nxt = pg["Members@odata.nextLink"]; }
    const res = await Promise.all(refs.slice(0, limit).map((m) => (Object.keys(m).length > 1 ? m : this.tryGet(odata(m)))));
    return res.filter(Boolean);
  }
  async systems() { return this.members(odata((await this.root()).Systems)); }
  async chassis() { return this.members(odata((await this.root()).Chassis)); }
  async managers() { return this.members(odata((await this.root()).Managers)); }
}

// ---------------------------------------------------------------- collectors
function vendorOf(root, mgr) {
  const v = (root.Vendor || "").toLowerCase(); const o = root.Oem || {};
  const model = `${mgr?.Model || ""} ${mgr?.Name || ""}`.toLowerCase();
  if (v.includes("dell") || "Dell" in o || model.includes("idrac")) return "Dell";
  if (v.includes("hpe") || v === "hp" || "Hpe" in o || "Hp" in o || model.includes("ilo")) return "HPE";
  return root.Vendor || "Unknown";
}

async function collectIdentity(rf) {
  const root = await rf.root();
  const [systems, managers] = await Promise.all([rf.systems(), rf.managers()]);
  const mgr = managers[0] || {}; const vendor = vendorOf(root, mgr);
  const out = { target: rf.t.public(), vendor, redfish_version: root.RedfishVersion,
    bmc: { type: vendor === "Dell" ? "iDRAC" : vendor === "HPE" ? "iLO" : "BMC", model: mgr.Model, firmware: mgr.FirmwareVersion, status: status(mgr) },
    systems: [] };
  const mt = get(oem(mgr), "Manager");
  if (Array.isArray(mt) && mt[0] && typeof mt[0] === "object") {
    out.bmc.generation = mt[0].ManagerType; out.bmc.firmware_detail = mt[0].ManagerFirmwareVersion;
  }
  for (const s of systems) out.systems.push({ id: s.Id, manufacturer: s.Manufacturer, model: s.Model, serial: s.SerialNumber,
    service_tag_or_sku: s.SKU, hostname: s.HostName, bios: s.BiosVersion, power_state: s.PowerState, status: status(s) });
  return clean(out);
}

async function collectThermal(rf) {
  const out = [];
  for (const ch of await rf.chassis()) {
    const e = { chassis: ch.Id, temperatures: [], fans: [] };
    const th = await rf.tryGet(odata(ch.Thermal));
    if (th) {
      for (const t of th.Temperatures || []) { if (get(t, "Status", "State") === "Absent") continue;
        e.temperatures.push({ name: t.Name, reading_c: t.ReadingCelsius, warn_c: t.UpperThresholdNonCritical,
          critical_c: t.UpperThresholdCritical, fatal_c: t.UpperThresholdFatal, status: status(t) }); }
      for (const f of th.Fans || []) { if (get(f, "Status", "State") === "Absent") continue;
        e.fans.push({ name: f.Name || f.FanName || f.MemberId, reading: f.Reading ?? f.CurrentReading,
          units: f.ReadingUnits || f.Units, status: status(f) }); }
    } else {
      const ts = await rf.tryGet(odata(ch.ThermalSubsystem));
      if (ts) for (const f of await rf.members(odata(ts.Fans)))
        e.fans.push({ name: f.Name, reading: get(f, "SpeedPercent", "Reading"), units: "Percent", status: status(f) });
      for (const s of await rf.members(odata(ch.Sensors)))
        if (s.ReadingType === "Temperature") e.temperatures.push({ name: s.Name, reading_c: s.Reading,
          critical_c: get(s, "Thresholds", "UpperCritical", "Reading"), status: status(s) });
    }
    if (e.temperatures.length || e.fans.length) out.push(e);
  }
  return { chassis: clean(out) };
}

async function collectPower(rf) {
  const out = [];
  for (const ch of await rf.chassis()) {
    const e = { chassis: ch.Id, power_supplies: [] };
    const pw = await rf.tryGet(odata(ch.Power));
    if (pw) {
      for (const p of pw.PowerSupplies || []) {
        const o = oem(p); const keep = {};
        for (const k of ["PowerSupplyStatus", "BayNumber", "HotplugCapable", "Mismatched", "AveragePowerOutputWatts"]) if (k in o) keep[k] = o[k];
        e.power_supplies.push({ name: p.Name || p.MemberId, model: p.Model, serial: p.SerialNumber, part: p.PartNumber || p.SparePartNumber,
          firmware: p.FirmwareVersion, capacity_w: p.PowerCapacityWatts, output_w: p.LastPowerOutputWatts,
          input_v: p.LineInputVoltage, status: status(p), oem: keep });
      }
      e.consumed_w = (pw.PowerControl || [{}])[0]?.PowerConsumedWatts;
      e.redundancy = (pw.Redundancy || []).map((r) => ({ name: r.Name, mode: r.Mode, status: status(r) }));
      e.voltage_problems = (pw.Voltages || []).filter((v) => isProblem(v.Status)).map((v) => ({ name: v.Name, reading_v: v.ReadingVolts, status: status(v) }));
    } else {
      const ps = await rf.tryGet(odata(ch.PowerSubsystem));
      if (ps) for (const p of await rf.members(odata(ps.PowerSupplies)))
        e.power_supplies.push({ name: p.Name, model: p.Model, serial: p.SerialNumber, firmware: p.FirmwareVersion, capacity_w: p.PowerCapacityWatts, status: status(p) });
    }
    if (e.power_supplies.length || e.consumed_w) out.push(e);
  }
  return { chassis: clean(out) };
}

async function collectInventory(rf) {
  const out = [];
  for (const s of await rf.systems()) {
    const [cpus, mem] = await Promise.all([rf.members(odata(s.Processors)), rf.members(odata(s.Memory))]);
    const dimms = [];
    for (const m of mem) {
      const dstat = oem(m).DIMMStatus ?? m.DIMMStatus;
      if (get(m, "Status", "State") === "Absent" || dstat === "NotPresent" || (!m.CapacityMiB && !m.SizeMB)) continue;
      dimms.push({ slot: m.DeviceLocator || m.Name || m.Id, capacity_mib: m.CapacityMiB || m.SizeMB,
        speed_mhz: m.OperatingSpeedMhz || m.MaximumFrequencyMHz, type: m.MemoryDeviceType || m.DIMMType,
        part: m.PartNumber, manufacturer: m.Manufacturer, hpe_dimm_status: dstat, status: status(m) });
    }
    out.push({ system: s.Id, processor_summary: s.ProcessorSummary, memory_summary: s.MemorySummary,
      processors: cpus.filter((c) => get(c, "Status", "State") !== "Absent").map((c) => ({ socket: c.Socket || c.Id, model: c.Model,
        cores: c.TotalCores, threads: c.TotalThreads, status: status(c) })),
      memory: dimms });
  }
  const nics = [];
  for (const ch of await rf.chassis())
    for (const n of await rf.members(odata(ch.NetworkAdapters)))
      nics.push({ name: n.Name || n.Id, model: n.Model, manufacturer: n.Manufacturer, status: status(n) });
  return clean({ systems: out, network_adapters: nics });
}

const drive = (d) => ({ name: d.Name || d.Id, model: d.Model, serial: d.SerialNumber, media: d.MediaType, protocol: d.Protocol,
  capacity_gb: gb(d.CapacityBytes), failure_predicted: d.FailurePredicted, life_left_pct: d.PredictedMediaLifeLeftPercent,
  firmware: d.Revision, status: status(d), operations: d.Operations });

async function collectStorage(rf) {
  const out = [];
  for (const s of await rf.systems()) {
    const se = { system: s.Id, controllers: [] };
    for (const st of await rf.members(odata(s.Storage))) {
      const ctrls = st.StorageControllers || [];
      const drives = await Promise.all((st.Drives || []).map((d) => rf.tryGet(odata(d))));
      const vols = await rf.members(odata(st.Volumes));
      se.controllers.push({ id: st.Id, name: st.Name, model: ctrls[0]?.Model, firmware: ctrls[0]?.FirmwareVersion, status: status(st),
        drives: drives.filter(Boolean).map(drive),
        volumes: vols.map((v) => ({ name: v.Name || v.Id, raid: v.RAIDType || v.VolumeType, capacity_gb: gb(v.CapacityBytes), status: status(v), operations: v.Operations })) });
    }
    if (!se.controllers.length) { // HPE SmartStorage
      const ss = await rf.tryGet(odata(get(oem(s), "Links", "SmartStorage")));
      if (ss) for (const ac of await rf.members(odata(get(ss, "Links", "ArrayControllers")))) {
        const [pds, lds] = await Promise.all([rf.members(odata(get(ac, "Links", "PhysicalDrives"))), rf.members(odata(get(ac, "Links", "LogicalDrives")))]);
        se.controllers.push({ id: ac.Id, name: ac.Model, model: ac.Model, firmware: get(ac, "FirmwareVersion", "Current", "VersionString"),
          location: ac.Location, status: status(ac), cache_status: get(ac, "CacheModuleStatus", "Health"),
          drives: pds.map((p) => ({ name: p.Location || p.Id, model: p.Model, serial: p.SerialNumber, media: p.MediaType,
            capacity_gb: p.CapacityMiB ? mibToGb(p.CapacityMiB) : p.CapacityGB, status: status(p),
            ssd_life_left_pct: p.SSDEnduranceUtilizationPercentage != null ? 100 - p.SSDEnduranceUtilizationPercentage : undefined })),
          volumes: lds.map((l) => ({ name: l.LogicalDriveName || l.Id, raid: l.Raid, capacity_gb: mibToGb(l.CapacityMiB), status: status(l) })) });
      }
    }
    if (!se.controllers.length)
      for (const ss of await rf.members(odata(s.SimpleStorage)))
        se.controllers.push({ id: ss.Id, name: ss.Name, status: status(ss),
          drives: (ss.Devices || []).map((d) => ({ name: d.Name, model: d.Model, capacity_gb: gb(d.CapacityBytes), status: status(d) })) });
    if (!se.controllers.length)
      se.note = "No storage data returned. On HPE, storage inventory is only populated after the server has completed POST; also check the controller is supported by the BMC (some HBAs/NVMe are not).";
    out.push(se);
  }
  return clean({ systems: out });
}

async function collectFirmware(rf) {
  const root = await rf.root();
  const us = await rf.tryGet(odata(root.UpdateService));
  const inv = us ? odata(us.FirmwareInventory) : undefined;
  let items = inv ? await rf.members(inv) : [];
  if (!items.length) for (const s of await rf.systems()) { // iLO 4
    const fw = await rf.tryGet(odata(get(oem(s), "Links", "FirmwareInventory")));
    if (fw && fw.Current && typeof fw.Current === "object")
      for (const g of Object.values(fw.Current)) for (const f of g) items.push({ Name: f.Name, Version: f.VersionString, Id: f.Key });
  }
  const out = items.filter((f) => !String(f.Id || "").startsWith("Previous"))
    .map((f) => ({ name: f.Name, version: f.Version, updateable: f.Updateable, id: f.Id, status: status(f) }))
    .sort((a, b) => String(a.name || "").localeCompare(String(b.name || "")));
  return clean({ firmware: out, count: out.length });
}

const SEV = { OK: 0, Informational: 0, Warning: 1, Critical: 2 };

async function listLogServices(rf) {
  const out = [];
  for (const [kind, res] of [["Manager", await rf.managers()], ["System", await rf.systems()]])
    for (const r of res) for (const ls of await rf.members(odata(r.LogServices)))
      out.push({ id: ls.Id, name: ls.Name, on: kind, path: odata(ls), entries: odata(ls.Entries), status: status(ls) });
  return out;
}

async function readLog(rf, entriesPath, maxEntries, minSev) {
  const first = await rf.get(entriesPath);
  const total = first["Members@odata.count"];
  let members = [...(first.Members || [])]; let nxt = first["Members@odata.nextLink"];
  const cap = 3000;
  const ts = members.filter((m) => m.Created).map((m) => ptime(m.Created));
  const oldestFirst = ts.length >= 2 && ts[0] < ts[ts.length - 1];
  if (nxt && oldestFirst && Number.isInteger(total) && total > members.length) {
    const sep = entriesPath.includes("?") ? "&" : "?";
    const want = minSev === 0 ? Math.max(maxEntries * 2, 100) : cap;
    const tail = await rf.tryGet(`${entriesPath}${sep}$skip=${Math.max(0, total - want)}`);
    if (tail?.Members?.length) { members = [...tail.Members]; nxt = tail["Members@odata.nextLink"]; }
  }
  while (nxt && members.length < cap && (oldestFirst || minSev > 0 || members.length < maxEntries * 2)) {
    const pg = await rf.tryGet(nxt); if (!pg) break;
    members.push(...(pg.Members || [])); nxt = pg["Members@odata.nextLink"];
  }
  const entries = [];
  for (const m of members) {
    const sev = m.Severity || m.EntryType;
    if ((SEV[sev] ?? 0) < minSev) continue;
    const o = oem(m);
    entries.push(clean({ created: m.Created, severity: sev, message: m.Message, message_id: m.MessageId, sensor: m.SensorType,
      entry_code: m.EntryCode, id: m.Id, hpe_class: o.Class, hpe_code: o.Code, repaired: o.Repaired, count: o.Count,
      updated: o.Updated, dell_category: o.Category }));
  }
  entries.sort((a, b) => ptime(b.created) - ptime(a.created));
  return entries.slice(0, maxEntries);
}

async function collectProblems(rf) {
  const probs = [];
  const names = ["thermal", "power", "inventory", "storage"];
  const res = await Promise.allSettled([collectThermal(rf), collectPower(rf), collectInventory(rf), collectStorage(rf)]);
  const [th, pw, inv, st] = res.map((r) => (r.status === "fulfilled" ? r.value : null));
  const add = (area, name, s, extra = {}) => { if (isProblem(s)) probs.push(clean({ area, component: name, status: s, ...extra })); };
  for (const c of th?.chassis || []) {
    for (const t of c.temperatures || []) add("Temperature", t.name, t.status, { reading_c: t.reading_c });
    for (const f of c.fans || []) add("Fan", f.name, f.status, { reading: f.reading });
  }
  for (const c of pw?.chassis || []) {
    for (const p of c.power_supplies || []) add("PSU", p.name, p.status);
    for (const r of c.redundancy || []) add("PSU redundancy", r.name, r.status);
    for (const v of c.voltage_problems || []) add("Voltage", v.name, v.status, { reading_v: v.reading_v });
  }
  for (const s of inv?.systems || []) {
    for (const c of s.processors || []) add("CPU", c.socket, c.status);
    for (const m of s.memory || []) add("Memory", m.slot, m.status);
  }
  for (const s of st?.systems || []) for (const c of s.controllers || []) {
    add("Storage controller", c.name || c.id, c.status);
    for (const d of c.drives || []) {
      add("Drive", d.name, d.status, { serial: d.serial });
      if (d.failure_predicted) probs.push({ area: "Drive", component: d.name, issue: "Predictive failure flagged" });
    }
    for (const v of c.volumes || []) add("Volume", v.name, v.status);
  }
  const errors = res.map((r, i) => (r.status === "rejected" ? `${names[i]}: ${r.reason?.message || r.reason}` : null)).filter(Boolean);
  if (errors.length) probs.push({ area: "collector-errors", details: errors });
  return probs;
}

async function collectHealth(rf) {
  const identity = await collectIdentity(rf);
  const rollups = [];
  for (const s of await rf.systems()) {
    const o = oem(s); const r = { system: s.Id, status: status(s) };
    if (o.AggregateHealthStatus && typeof o.AggregateHealthStatus === "object") {
      r.hpe_aggregate_health = {};
      for (const [k, v] of Object.entries(o.AggregateHealthStatus)) r.hpe_aggregate_health[k] = v && typeof v === "object" ? get(v, "Status", "Health") : v;
    }
    if (o.DellSystem && typeof o.DellSystem === "object") {
      r.dell_rollups = {};
      for (const [k, v] of Object.entries(o.DellSystem)) if (k.includes("Status") && typeof v === "string") r.dell_rollups[k] = v;
    }
    r.processor_summary_health = get(s, "ProcessorSummary", "Status", "HealthRollup") || get(s, "ProcessorSummary", "Status", "Health");
    r.memory_summary_health = get(s, "MemorySummary", "Status", "HealthRollup") || get(s, "MemorySummary", "Status", "Health");
    rollups.push(r);
  }
  const chassis = (await rf.chassis()).map((c) => ({ chassis: c.Id, status: status(c), intrusion: get(c, "PhysicalSecurity", "IntrusionSensor") }));
  const problems = await collectProblems(rf);
  const real = problems.filter((p) => p.area !== "collector-errors");
  return clean({ identity, rollups, chassis, overall: real.length ? `${real.length} component(s) not OK` : "OK", problems });
}

// ---------------------------------------------------------------- MCP
const state = { target: null };
function target() {
  if (!state.target) state.target = envTarget();
  if (!state.target) throw new RedfishError("No BMC configured. Set BMC_HOST/BMC_USERNAME/BMC_PASSWORD in the MCP config, or call bmc_set_target.");
  return state.target;
}
async function run(fn) {
  let rf;
  try { rf = new Redfish(target()); return await fn(rf); }
  catch (e) { return { error: e instanceof RedfishError ? e.message : `${e.name}: ${e.message}` }; }
  finally { if (rf) await rf.logout(); }
}
const asResult = (obj) => ({ content: [{ type: "text", text: JSON.stringify(obj, null, 1) }], isError: !!obj?.error });

const server = new McpServer({ name: "bmc-mcp", version: VERSION }, {
  instructions: "Read-only access to a Dell iDRAC or HPE iLO over Redfish. Start with bmc_health_summary, then drill into event logs, thermal, power, storage, inventory or firmware. Nothing in this server changes the server's state." });

const tool = (name, description, shape, handler) =>
  server.registerTool(name, { description, inputSchema: shape, annotations: { readOnlyHint: true } },
    async (args) => asResult(await handler(args || {})));

tool("bmc_set_target", "Point the server at a different iDRAC/iLO for this session. Either give host/username/password, or give `name` of an entry in BMC_TARGETS_FILE. Then verifies the connection and returns what was found.",
  { host: z.string().optional(), username: z.string().optional(), password: z.string().optional(), verify_tls: z.boolean().optional(), name: z.string().optional() },
  async ({ host, username = "", password = "", verify_tls = false, name }) => {
    if (name && !host) {
      const all = loadTargetsFile(); const t = all[name];
      if (!t) return { error: `No target named '${name}' in BMC_TARGETS_FILE`, available: Object.keys(all) };
      state.target = new Target(t.host, t.username || "", t.password || "", bool(t.verify_tls), name);
    } else if (host) state.target = new Target(host, username, password, verify_tls, name);
    else return { error: "Provide host (+ username/password) or name." };
    return run(collectIdentity);
  });
tool("bmc_list_targets", "Show the current target and any named targets in BMC_TARGETS_FILE (passwords hidden).", {}, async () => {
  let cur = null; try { cur = target().public(); } catch { /* none */ }
  const named = {}; for (const [k, v] of Object.entries(loadTargetsFile())) named[k] = { host: v.host, username: v.username };
  return { current: cur, named_targets: named };
});
tool("bmc_identify", "Vendor (Dell/HPE), BMC type/generation/firmware, server model, serial/service tag, BIOS, power state.", {}, () => run(collectIdentity));
tool("bmc_health_summary", "Start here. Overall health rollups (Dell rollup statuses / HPE AggregateHealthStatus) plus a list of every component that is not OK across fans, temperatures, PSUs, voltages, CPUs, DIMMs, controllers, drives and volumes.", {}, () => run(collectHealth));
tool("bmc_list_logs", "List the log services this BMC exposes (e.g. Dell Sel/Lclog, HPE IML/IEL/SL).", {}, () => run(async (rf) => ({ logs: clean(await listLogServices(rf)) })));
tool("bmc_get_event_logs",
  "Read event log entries, newest first. log: log service Id or name (e.g. \"Sel\", \"Lclog\", \"IML\", \"IEL\"); empty = main hardware logs (Dell SEL + Lifecycle, HPE IML + iLO Event Log). max_entries: per log (default 50, max 500). min_severity: \"all\", \"warning\" (Warning+Critical) or \"critical\".",
  { log: z.string().optional(), max_entries: z.number().int().optional(), min_severity: z.string().optional() },
  ({ log: name = "", max_entries = 50, min_severity = "all" }) => run(async (rf) => {
    const minSev = { all: 0, warning: 1, critical: 2 }[String(min_severity).toLowerCase()] ?? 0;
    const max = Math.max(1, Math.min(Number(max_entries) || 50, 500));
    const services = await listLogServices(rf);
    let chosen;
    if (name) chosen = services.filter((s) => `${s.id || ""} ${s.name || ""}`.toLowerCase().includes(name.toLowerCase()));
    else { const pref = /^(sel|lclog|iml|iel|eventlog|log1?)$/i; chosen = services.filter((s) => pref.test(s.id || "")); if (!chosen.length) chosen = services; }
    if (!chosen.length) return { error: `No log matching '${name}'`, available: services.map((s) => s.id) };
    const out = [];
    for (const s of chosen) {
      if (!s.entries) continue;
      try { const ents = await readLog(rf, s.entries, max, minSev); out.push({ log: s.id, name: s.name, on: s.on, returned: ents.length, entries: ents }); }
      catch (e) { if (e instanceof RedfishError) out.push({ log: s.id, error: e.message }); else throw e; }
    }
    return { logs: out };
  }));
tool("bmc_get_thermal", "All temperature sensors (with warning/critical thresholds) and fans (speed, status).", {}, () => run(collectThermal));
tool("bmc_get_power", "Power supplies (model, firmware, capacity, output, input voltage, status), power consumption, PSU redundancy, and any voltage sensors that are not OK.", {}, () => run(collectPower));
tool("bmc_get_storage", "RAID/HBA controllers, physical drives (status, predictive failure, SSD life left) and virtual disks/logical drives (RAID level, status, rebuild/background operations).", {}, () => run(collectStorage));
tool("bmc_get_inventory", "CPUs, populated DIMMs (slot, size, speed, part, status) and network adapters.", {}, () => run(collectInventory));
tool("bmc_get_firmware", "Installed firmware versions: BIOS, BMC, controllers, NICs, drives, PSUs, CPLD, etc.", {}, () => run(collectFirmware));
tool("bmc_redfish_get", "Raw read-only GET of any Redfish resource, for anything the other tools don't cover. path must start with /redfish/ (e.g. /redfish/v1/Managers/iDRAC.Embedded.1/NetworkProtocol).",
  { path: z.string() }, ({ path }) => {
    if (!path.startsWith("/redfish/")) return { error: "path must start with /redfish/" };
    return run(async (rf) => { const d = await rf.get(path); const t = JSON.stringify(d);
      return t.length > 60000 ? { truncated: true, size: t.length, data_head: t.slice(0, 60000) } : d; });
  });

// ---------------------------------------------------------------- main
const argv = process.argv.slice(2);
if (argv.includes("--version")) console.log(VERSION);
else if (argv.includes("--check")) {
  try {
    const t = target(); console.log(`Connecting to ${t.base} as ${t.username} ...`);
    const res = await run(collectHealth); console.log(JSON.stringify(res, null, 2));
    process.exit(res.error ? 1 : 0);
  } catch (e) { console.log(`ERROR: ${e.message}`); process.exit(1); }
} else {
  await server.connect(new StdioServerTransport());
  log(`bmc-mcp ${VERSION} ready`);
}
