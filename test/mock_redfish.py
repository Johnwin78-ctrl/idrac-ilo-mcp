#!/usr/bin/env python3
"""Mock Redfish BMCs for testing bmc-mcp.  python mock_redfish.py dell|ilo5|ilo4 PORT"""
import base64
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

KIND, PORT = sys.argv[1], int(sys.argv[2])
USER, PASS = "monitor", "secret"
TOKEN = "tok-123"
R: dict[str, dict] = {}


def ref(p): return {"@odata.id": p}
def coll(p, items): R[p] = {"@odata.id": p, "Members": [ref(i) for i in items], "Members@odata.count": len(items)}
def ok(state="Enabled"): return {"State": state, "Health": "OK"}


if KIND == "dell":
    S, C, M = "/redfish/v1/Systems/System.Embedded.1", "/redfish/v1/Chassis/System.Embedded.1", "/redfish/v1/Managers/iDRAC.Embedded.1"
    R["/redfish/v1/"] = {"RedfishVersion": "1.17.0", "Vendor": "Dell", "Oem": {"Dell": {}},
                         "Systems": ref("/redfish/v1/Systems"), "Chassis": ref("/redfish/v1/Chassis"),
                         "Managers": ref("/redfish/v1/Managers"), "UpdateService": ref("/redfish/v1/UpdateService"),
                         "SessionService": ref("/redfish/v1/SessionService")}
    coll("/redfish/v1/Systems", [S]); coll("/redfish/v1/Chassis", [C]); coll("/redfish/v1/Managers", [M])
    R[S] = {"@odata.id": S, "Id": "System.Embedded.1", "Manufacturer": "Dell Inc.", "Model": "PowerEdge R750",
            "SerialNumber": "CNIVC001", "SKU": "ABC1234", "BiosVersion": "1.8.2", "PowerState": "On",
            "Status": {"State": "Enabled", "Health": "Critical", "HealthRollup": "Critical"},
            "ProcessorSummary": {"Count": 2, "Status": {"Health": "OK", "HealthRollup": "OK"}},
            "MemorySummary": {"TotalSystemMemoryGiB": 64, "Status": {"Health": "OK", "HealthRollup": "OK"}},
            "Oem": {"Dell": {"DellSystem": {"PSRollupStatus": "Critical", "FanRollupStatus": "OK",
                                            "StorageRollupStatus": "Warning", "TempRollupStatus": "OK"}}},
            "Processors": ref(S + "/Processors"), "Memory": ref(S + "/Memory"),
            "Storage": ref(S + "/Storage"), "LogServices": ref(S + "/LogServices")}
    coll(S + "/Processors", [S + "/Processors/CPU.Socket.1", S + "/Processors/CPU.Socket.2"])
    for i in (1, 2):
        R[S + f"/Processors/CPU.Socket.{i}"] = {"Id": f"CPU.Socket.{i}", "Socket": f"CPU.Socket.{i}",
                                                "Model": "Intel Xeon Gold 6338", "TotalCores": 32, "Status": ok()}
    coll(S + "/Memory", [S + "/Memory/DIMM.A1", S + "/Memory/DIMM.A2"])
    R[S + "/Memory/DIMM.A1"] = {"Id": "DIMM.Socket.A1", "DeviceLocator": "DIMM.Socket.A1", "CapacityMiB": 32768,
                                "OperatingSpeedMhz": 3200, "PartNumber": "M393A4K40EB3", "Status": ok()}
    R[S + "/Memory/DIMM.A2"] = {"Id": "DIMM.Socket.A2", "DeviceLocator": "DIMM.Socket.A2", "CapacityMiB": 0,
                                "Status": {"State": "Absent"}}
    coll(S + "/Storage", [S + "/Storage/RAID.Integrated.1-1"])
    R[S + "/Storage/RAID.Integrated.1-1"] = {
        "@odata.id": S + "/Storage/RAID.Integrated.1-1", "Id": "RAID.Integrated.1-1", "Name": "PERC H755",
        "Status": {"State": "Enabled", "Health": "Warning", "HealthRollup": "Warning"},
        "StorageControllers": [{"Model": "PERC H755 Front", "FirmwareVersion": "52.16.1-4074"}],
        "Drives": [ref(S + "/Storage/Drives/Disk.0"), ref(S + "/Storage/Drives/Disk.1")],
        "Volumes": ref(S + "/Storage/RAID.Integrated.1-1/Volumes")}
    R[S + "/Storage/Drives/Disk.0"] = {"Id": "Disk.Bay.0", "Name": "SSD 0", "Model": "MZ7L3960", "SerialNumber": "S1",
                                       "MediaType": "SSD", "CapacityBytes": 960197124096, "Status": ok(),
                                       "PredictedMediaLifeLeftPercent": 97}
    R[S + "/Storage/Drives/Disk.1"] = {"Id": "Disk.Bay.1", "Name": "SSD 1", "Model": "MZ7L3960", "SerialNumber": "S2",
                                       "MediaType": "SSD", "CapacityBytes": 960197124096, "FailurePredicted": True,
                                       "Status": {"State": "Enabled", "Health": "Warning"}}
    coll(S + "/Storage/RAID.Integrated.1-1/Volumes", [S + "/Storage/Volumes/Disk.Virtual.0"])
    R[S + "/Storage/Volumes/Disk.Virtual.0"] = {"Id": "Disk.Virtual.0", "Name": "OS", "RAIDType": "RAID1",
                                                "CapacityBytes": 959656755200,
                                                "Status": {"State": "Enabled", "Health": "Warning"}}
    R[C] = {"@odata.id": C, "Id": "System.Embedded.1", "Status": ok(), "Thermal": ref(C + "/Thermal"),
            "Power": ref(C + "/Power"), "NetworkAdapters": ref(C + "/NetworkAdapters")}
    R[C + "/Thermal"] = {"Temperatures": [{"Name": "System Board Inlet Temp", "ReadingCelsius": 24,
                                           "UpperThresholdCritical": 47, "Status": ok()}],
                         "Fans": [{"Name": "System Board Fan1A", "Reading": 6840, "ReadingUnits": "RPM", "Status": ok()}]}
    R[C + "/Power"] = {"PowerControl": [{"PowerConsumedWatts": 312}],
                       "PowerSupplies": [
                           {"Name": "PS1 Status", "Model": "PWR SPLY,1400W", "PowerCapacityWatts": 1400,
                            "FirmwareVersion": "00.1B.53", "LineInputVoltage": 230, "Status": ok()},
                           {"Name": "PS2 Status", "Model": "PWR SPLY,1400W", "PowerCapacityWatts": 1400,
                            "FirmwareVersion": "00.1B.53", "LineInputVoltage": 0,
                            "Status": {"State": "Enabled", "Health": "Critical"}}],
                       "Redundancy": [{"Name": "System Board PS Redundancy", "Mode": "N+m",
                                       "Status": {"State": "Enabled", "Health": "Critical"}}],
                       "Voltages": [{"Name": "PS2 Voltage 2", "ReadingVolts": 0,
                                     "Status": {"State": "Enabled", "Health": "Critical"}}]}
    coll(C + "/NetworkAdapters", [C + "/NetworkAdapters/NIC.Integrated.1"])
    R[C + "/NetworkAdapters/NIC.Integrated.1"] = {"Id": "NIC.Integrated.1", "Model": "BCM57414", "Status": ok()}
    R[M] = {"@odata.id": M, "Id": "iDRAC.Embedded.1", "Model": "15G Monolithic", "FirmwareVersion": "7.00.00.00",
            "Status": ok(), "LogServices": ref(M + "/LogServices")}
    coll(M + "/LogServices", [M + "/LogServices/Sel", M + "/LogServices/Lclog"])
    R[M + "/LogServices/Sel"] = {"@odata.id": M + "/LogServices/Sel", "Id": "Sel", "Name": "SEL Log",
                                 "Entries": ref(M + "/LogServices/Sel/Entries")}
    R[M + "/LogServices/Lclog"] = {"@odata.id": M + "/LogServices/Lclog", "Id": "Lclog", "Name": "LC Log",
                                   "Entries": ref(M + "/LogServices/Lclog/Entries")}
    R[M + "/LogServices/Sel/Entries"] = {"Members@odata.count": 2, "Members": [
        {"Id": "2", "Created": "2026-11-19T08:12:03-06:00", "Severity": "Critical", "MessageId": "PSU0003",
         "Message": "The power input for power supply 2 is lost.", "SensorType": "Power Supply"},
        {"Id": "1", "Created": "2026-11-10T01:00:00-06:00", "Severity": "OK", "Message": "Log cleared."}]}
    R[M + "/LogServices/Lclog/Entries"] = {"Members@odata.count": 1, "Members": [
        {"Id": "9", "Created": "2026-11-19T08:15:00-06:00", "Severity": "Warning", "MessageId": "PDR1016",
         "Message": "Drive Disk.Bay.1 is predicted to fail.", "Oem": {"Dell": {"Category": "Storage"}}}]}
    R["/redfish/v1/UpdateService"] = {"FirmwareInventory": ref("/redfish/v1/UpdateService/FirmwareInventory")}
    coll("/redfish/v1/UpdateService/FirmwareInventory",
         ["/redfish/v1/UpdateService/FirmwareInventory/Installed-159-1.8.2",
          "/redfish/v1/UpdateService/FirmwareInventory/Previous-159-1.7.5"])
    R["/redfish/v1/UpdateService/FirmwareInventory/Installed-159-1.8.2"] = {"Id": "Installed-159-1.8.2", "Name": "BIOS", "Version": "1.8.2", "Updateable": True, "Status": ok()}
    R["/redfish/v1/UpdateService/FirmwareInventory/Previous-159-1.7.5"] = {"Id": "Previous-159-1.7.5", "Name": "BIOS", "Version": "1.7.5"}

elif KIND == "ilo5":
    S, C, M = "/redfish/v1/Systems/1", "/redfish/v1/Chassis/1", "/redfish/v1/Managers/1"
    R["/redfish/v1/"] = {"RedfishVersion": "1.13.0", "Vendor": "HPE", "Oem": {"Hpe": {}},
                         "Systems": ref("/redfish/v1/Systems/"), "Chassis": ref("/redfish/v1/Chassis/"),
                         "Managers": ref("/redfish/v1/Managers/"), "UpdateService": ref("/redfish/v1/UpdateService/")}
    coll("/redfish/v1/Systems/", [S]); coll("/redfish/v1/Chassis/", [C]); coll("/redfish/v1/Managers/", [M])
    R[S] = {"@odata.id": S, "Id": "1", "Manufacturer": "HPE", "Model": "ProLiant DL380 Gen10 Plus",
            "SerialNumber": "SGH123", "BiosVersion": "U46 v2.80", "PowerState": "On",
            "Status": {"State": "Enabled", "Health": "Warning"},
            "Oem": {"Hpe": {"AggregateHealthStatus": {"Fans": {"Status": {"Health": "OK"}},
                                                      "Memory": {"Status": {"Health": "Warning"}},
                                                      "PowerSupplies": {"Status": {"Health": "OK"}}}}},
            "Processors": ref(S + "/Processors/"), "Memory": ref(S + "/Memory/"),
            "Storage": ref(S + "/Storage/"), "LogServices": ref(S + "/LogServices/")}
    coll(S + "/Processors/", [S + "/Processors/1"])
    R[S + "/Processors/1"] = {"Id": "1", "Socket": "Proc 1", "Model": "Intel Xeon Silver 4314", "TotalCores": 16, "Status": ok()}
    coll(S + "/Memory/", [S + "/Memory/proc1dimm1", S + "/Memory/proc1dimm2"])
    R[S + "/Memory/proc1dimm1"] = {"Id": "proc1dimm1", "DeviceLocator": "PROC 1 DIMM 1", "CapacityMiB": 32768,
                                   "Status": {"State": "Enabled", "Health": "Warning"},
                                   "Oem": {"Hpe": {"DIMMStatus": "DegradedModuleFailurePredicted"}}}
    R[S + "/Memory/proc1dimm2"] = {"Id": "proc1dimm2", "DeviceLocator": "PROC 1 DIMM 2", "CapacityMiB": 0,
                                   "Oem": {"Hpe": {"DIMMStatus": "NotPresent"}}}
    coll(S + "/Storage/", [])
    R[C] = {"@odata.id": C, "Id": "1", "Status": ok(), "Thermal": ref(C + "/Thermal/"), "Power": ref(C + "/Power/")}
    R[C + "/Thermal/"] = {"Temperatures": [{"Name": "01-Inlet Ambient", "ReadingCelsius": 22, "UpperThresholdCritical": 42, "Status": ok()}],
                          "Fans": [{"Name": "Fan 1", "Reading": 23, "ReadingUnits": "Percent", "Status": ok()}]}
    R[C + "/Power/"] = {"PowerControl": [{"PowerConsumedWatts": 180}],
                        "PowerSupplies": [{"Name": "HpeServerPowerSupply", "MemberId": "0", "Model": "865414-B21",
                                           "PowerCapacityWatts": 800, "Status": ok(),
                                           "Oem": {"Hpe": {"BayNumber": 1, "PowerSupplyStatus": {"State": "Ok"}}}}]}
    R[M] = {"@odata.id": M, "Id": "1", "Model": "iLO 5", "FirmwareVersion": "iLO 5 v3.04", "Status": ok(),
            "LogServices": ref(M + "/LogServices/"),
            "Oem": {"Hpe": {"Manager": [{"ManagerType": "iLO 5", "ManagerFirmwareVersion": "3.04"}]}}}
    coll(M + "/LogServices/", [M + "/LogServices/IEL/"])
    R[M + "/LogServices/IEL/"] = {"@odata.id": M + "/LogServices/IEL/", "Id": "IEL", "Name": "iLO Event Log",
                                  "Entries": ref(M + "/LogServices/IEL/Entries/")}
    R[M + "/LogServices/IEL/Entries/"] = {"Members@odata.count": 1, "Members": [
        {"Id": "1", "Created": "2026-11-18T10:00:00Z", "Severity": "OK", "Message": "Browser login: monitor"}]}
    coll(S + "/LogServices/", [S + "/LogServices/IML/"])
    R[S + "/LogServices/IML/"] = {"@odata.id": S + "/LogServices/IML/", "Id": "IML", "Name": "Integrated Management Log",
                                  "Entries": ref(S + "/LogServices/IML/Entries/")}
    R[S + "/LogServices/IML/Entries/"] = {"Members@odata.count": 1, "Members": [
        {"Id": "5", "Created": "2026-11-19T02:00:00Z", "Severity": "Warning",
         "Message": "Corrected Memory Error threshold exceeded (Processor 1, Memory Module 1)",
         "Oem": {"Hpe": {"Class": 19, "Code": 1, "Repaired": False, "Count": 3}}}]}
    R["/redfish/v1/UpdateService/"] = {"FirmwareInventory": ref("/redfish/v1/UpdateService/FirmwareInventory/")}
    coll("/redfish/v1/UpdateService/FirmwareInventory/", ["/redfish/v1/UpdateService/FirmwareInventory/1/"])
    R["/redfish/v1/UpdateService/FirmwareInventory/1/"] = {"Id": "1", "Name": "iLO 5", "Version": "3.04", "Updateable": True}

elif KIND == "ilo4":  # legacy: Hp OEM, no Vendor, SmartStorage, oldest-first paged IML, no sessions
    S, C, M = "/redfish/v1/Systems/1/", "/redfish/v1/Chassis/1/", "/redfish/v1/Managers/1/"
    R["/redfish/v1/"] = {"RedfishVersion": "1.0.0", "Oem": {"Hp": {}},
                         "Systems": ref("/redfish/v1/Systems/"), "Chassis": ref("/redfish/v1/Chassis/"),
                         "Managers": ref("/redfish/v1/Managers/")}
    coll("/redfish/v1/Systems/", [S]); coll("/redfish/v1/Chassis/", [C]); coll("/redfish/v1/Managers/", [M])
    R[S] = {"@odata.id": S, "Id": "1", "Manufacturer": "HP", "Model": "ProLiant DL360 Gen9", "SerialNumber": "CZJ999",
            "BiosVersion": "P89 v2.76", "PowerState": "On", "Status": {"State": "Enabled", "Health": "Warning"},
            "Memory": ref(S + "Memory/"), "LogServices": ref(S + "LogServices/"),
            "Oem": {"Hp": {"Links": {"SmartStorage": ref(S + "SmartStorage/"),
                                     "FirmwareInventory": ref(S + "FirmwareInventory/")}}}}
    coll(S + "Memory/", [S + "Memory/proc1dimm1/"])
    R[S + "Memory/proc1dimm1/"] = {"Name": "PROC 1 DIMM 1", "SizeMB": 16384, "DIMMStatus": "GoodInUse"}
    R[S + "SmartStorage/"] = {"Links": {"ArrayControllers": ref(S + "SmartStorage/ArrayControllers/")}}
    coll(S + "SmartStorage/ArrayControllers/", [S + "SmartStorage/ArrayControllers/0/"])
    AC = S + "SmartStorage/ArrayControllers/0/"
    R[AC] = {"Id": "0", "Model": "Smart Array P440ar", "Location": "Slot 0",
             "FirmwareVersion": {"Current": {"VersionString": "7.00"}},
             "Status": {"State": "Enabled", "Health": "Warning"},
             "Links": {"PhysicalDrives": ref(AC + "DiskDrives/"), "LogicalDrives": ref(AC + "LogicalDrives/")}}
    coll(AC + "DiskDrives/", [AC + "DiskDrives/0/"])
    R[AC + "DiskDrives/0/"] = {"Id": "0", "Location": "1I:1:1", "Model": "EG0300FCSPH", "CapacityMiB": 286102,
                               "Status": {"State": "Enabled", "Health": "Critical"}}
    coll(AC + "LogicalDrives/", [AC + "LogicalDrives/1/"])
    R[AC + "LogicalDrives/1/"] = {"Id": "1", "LogicalDriveName": "LD1", "Raid": "1", "CapacityMiB": 286070,
                                  "Status": {"State": "Enabled", "Health": "Warning"}}
    R[S + "FirmwareInventory/"] = {"Current": {"SystemRomActive": [{"Name": "System ROM", "VersionString": "P89 v2.76", "Key": "rom"}],
                                               "SystemBMC": [{"Name": "iLO", "VersionString": "2.82", "Key": "ilo"}]}}
    R[C] = {"@odata.id": C, "Id": "1", "Status": ok(), "Thermal": ref(C + "Thermal/"), "Power": ref(C + "Power/")}
    R[C + "Thermal/"] = {"Temperatures": [{"Name": "01-Inlet Ambient", "CurrentReading": 21, "ReadingCelsius": 21, "Status": ok()}],
                         "Fans": [{"FanName": "Fan 1", "CurrentReading": 30, "Units": "Percent", "Status": ok()}]}
    R[C + "Power/"] = {"PowerSupplies": [{"Name": "HpServerPowerSupply", "PowerCapacityWatts": 500, "Status": ok()}]}
    R[M] = {"@odata.id": M, "Id": "1", "Model": "iLO 4", "FirmwareVersion": "iLO 4 v2.82", "Status": ok(),
            "Oem": {"Hp": {"Manager": [{"ManagerType": "iLO 4"}]}}}
    coll(S + "LogServices/", [S + "LogServices/IML/"])
    R[S + "LogServices/IML/"] = {"@odata.id": S + "LogServices/IML/", "Id": "IML", "Name": "Integrated Management Log",
                                 "Entries": ref(S + "LogServices/IML/Entries/")}
    IML = [{"Id": str(i), "Created": f"2026-11-{(i % 19) + 1:02d}T00:{i % 60:02d}:00Z",
            "Severity": "Critical" if i == 239 else "Informational",
            "Message": "Drive 1I:1:1 failed" if i == 239 else f"Routine entry {i}",
            "Oem": {"Hp": {"Class": 17, "Code": 3}}} for i in range(240)]
    IML.sort(key=lambda e: e["Created"])  # oldest first, like iLO
    PAGE = 40


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _auth(self):
        if self.headers.get("X-Auth-Token") == TOKEN:
            return True
        a = self.headers.get("Authorization", "")
        return a == "Basic " + base64.b64encode(f"{USER}:{PASS}".encode()).decode()

    def _send(self, code, body=None, headers=None):
        data = json.dumps(body or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if KIND == "ilo4" or not self.path.startswith("/redfish/v1/SessionService/Sessions"):
            return self._send(405, {"error": "not allowed"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if body.get("UserName") != USER or body.get("Password") != PASS:
            return self._send(401, {"error": {"message": "bad creds"}})
        self._send(201, {"@odata.id": "/redfish/v1/SessionService/Sessions/1"},
                   {"X-Auth-Token": TOKEN, "Location": f"http://localhost:{PORT}/redfish/v1/SessionService/Sessions/1"})

    def do_DELETE(self):
        print("SESSION DELETED", self.path, file=sys.stderr, flush=True)
        self._send(204)

    def do_PUT(self): self._send(405)
    do_PATCH = do_PUT

    def do_GET(self):
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        u = urlparse(self.path)
        p = u.path
        if KIND == "ilo4" and p == "/redfish/v1/Systems/1/LogServices/IML/Entries/":
            q = parse_qs(u.query)
            skip = int(q.get("$skip", ["0"])[0])
            page = IML[skip:skip + PAGE]
            body = {"Members@odata.count": len(IML), "Members": page}
            if skip + PAGE < len(IML):
                body["Members@odata.nextLink"] = f"{p}?$skip={skip + PAGE}"
            return self._send(200, body)
        if p in R:
            return self._send(200, R[p])
        self._send(404, {"error": {"message": "not found " + p}})


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
