"""
plconnect.py - phones (and other devices) connected to THIS PC by USB cable or Bluetooth.

Phone Link is one way a phone reaches a PC; a cable or a Bluetooth pairing leaves its own traces.
This module reads them from the LIVE Windows registry, logs and event logs of the computer it
runs on - read-only - so it describes THIS PC, never an evidence image. The viewer only shows it
when you ask to analyse this PC.

What is read (no admin needed for any of it) and what is deliberately NOT read:

  HKLM\\SYSTEM\\CurrentControlSet\\Enum\\USB                     USB device instances: VID/PID, serial (instance id),
                                                              description, manufacturer, driver service, ContainerID
  HKLM\\SYSTEM\\CurrentControlSet\\Enum\\SWD\\WPDBUSENUM          portable devices (MTP phones, cameras, volumes)
  HKLM\\SOFTWARE\\Microsoft\\Windows Portable Devices\\Devices   portable-device friendly names
  HKLM\\SYSTEM\\CurrentControlSet\\Services\\BTHPORT\\Parameters\\Devices
                                                              paired / seen Bluetooth devices: name, class of device,
                                                              manufacturer, LMP version, VID/PID, LastSeen, LastConnected
  HKLM\\SYSTEM\\CurrentControlSet\\Enum\\BTHENUM                  Bluetooth services per device (hands-free, phonebook, ...)
  HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\AutoplayHandlers\\KnownDevices
                                                              devices this user's AutoPlay has seen (label, ContainerID)
  C:\\Windows\\INF\\setupapi.dev.log                             first time each device's driver was installed
  Event logs: Microsoft-Windows-Kernel-PnP/Configuration (device configured / started),
              Microsoft-Windows-WPD-MTPClassDriver/Operational, Microsoft-Windows-Bluetooth-BthLEPrepairing/Operational

  NOT read: BTHPORT\\Parameters\\Keys (Bluetooth link keys - secrets) and the Enum\\...\\Properties
  subkeys (exact install / first / last arrival times - SYSTEM-only; need admin and a SYSTEM-level reader).

Registry key last-write times are shown as "KeyLastWriteUTC": Windows updates a device's key when
it changes, which often means when it was last connected - a lead, not a documented connection time.
Standard library only (winreg, ElementTree).
"""
import datetime as dt
import os
import re
import subprocess
import xml.etree.ElementTree as ET

HKLM, HKCU = "HKLM", "HKCU"
USB = r"SYSTEM\CurrentControlSet\Enum\USB"
WPD = r"SYSTEM\CurrentControlSet\Enum\SWD\WPDBUSENUM"
WPD_NAMES = r"SOFTWARE\Microsoft\Windows Portable Devices\Devices"
BTPORT = r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices"
BTENUM = r"SYSTEM\CurrentControlSet\Enum\BTHENUM"
KNOWN = r"Software\Microsoft\Windows\CurrentVersion\Explorer\AutoplayHandlers\KnownDevices"
SETUPAPI = r"C:\Windows\INF\setupapi.dev.log"
CHANNELS = ["Microsoft-Windows-Kernel-PnP/Configuration", "Microsoft-Windows-WPD-MTPClassDriver/Operational",
            "Microsoft-Windows-Bluetooth-BthLEPrepairing/Operational"]
DEVICE_PREFIXES = ("USB\\", "SWD\\WPDBUSENUM", "BTHENUM\\", "BTHLE\\", "USBSTOR\\")

# [published] Bluetooth Core: class-of-device major classes
BT_MAJOR = {0: "Miscellaneous", 1: "Computer", 2: "Phone", 3: "Network access point", 4: "Audio/Video",
            5: "Peripheral", 6: "Imaging", 7: "Body-worn (class 7)", 8: "Toy", 9: "Health", 31: "Uncategorized"}
# [published] Bluetooth SIG assigned numbers: service class UUIDs a phone commonly exposes
BT_SERVICES = {"1101": "Serial Port", "1103": "Dial-up Networking", "1105": "OBEX Object Push",
               "1106": "OBEX File Transfer", "1108": "Headset", "110a": "Audio Source (A2DP)",
               "110b": "Audio Sink (A2DP)", "110c": "A/V Remote Control Target", "110e": "A/V Remote Control",
               "1112": "Headset Audio Gateway", "1115": "PAN User", "1116": "Network Access Point",
               "111e": "Hands-Free", "111f": "Hands-Free Audio Gateway", "112f": "Phonebook Access (server)",
               "1132": "Message Access (server)", "1133": "Message Notification (server)", "1200": "PnP Information",
               "1800": "Generic Access (Bluetooth LE)", "1801": "Generic Attribute (Bluetooth LE)"}
# driver services that indicate a phone on USB (the evidence for the flag is shown with it)
PHONE_SERVICES = {"WUDFWpdMtp": "MTP phone/camera driver", "dg_ssudbus": "vendor phone USB driver",
                  "Modem": "phone modem interface", "ssudmdm": "vendor phone modem"}


# ---------------------------------------------------------------- sources
class LiveSource:
    """Read-only access to this computer's registry, setupapi log and event logs."""

    def __init__(self):
        import winreg
        self.W = winreg
        self.roots = {HKLM: winreg.HKEY_LOCAL_MACHINE, HKCU: winreg.HKEY_CURRENT_USER}
        self.denied = []

    def _open(self, hive, path):
        try:
            return self.W.OpenKey(self.roots[hive], path, 0, self.W.KEY_READ)
        except PermissionError:
            self.denied.append(f"{hive}\\{path}")
            return None
        except OSError:
            return None

    def subkeys(self, hive, path):
        k = self._open(hive, path)
        out = []
        while k is not None:
            try:
                out.append(self.W.EnumKey(k, len(out)))
            except OSError:
                break
        return out

    def values(self, hive, path):
        k = self._open(hive, path)
        out, i = {}, 0
        while k is not None:
            try:
                n, v, _ = self.W.EnumValue(k, i)
                out[n] = v
            except OSError:
                break
            i += 1
        return out

    def last_write(self, hive, path):
        k = self._open(hive, path)
        return self.W.QueryInfoKey(k)[2] if k is not None else None

    def text(self, path):
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def setupapi(self):
        """The current setupapi.dev.log and every archived copy Windows rotated next to it."""
        import glob
        files = sorted(glob.glob(os.path.join(os.path.dirname(SETUPAPI), "setupapi.dev*.log")))
        return [(os.path.basename(f), self.text(f)) for f in files]

    def events(self, channel, count=2000):
        """[(utc 'YYYY-MM-DD HH:MM:SS', event id, {data name: value})], newest first."""
        try:
            r = subprocess.run(["wevtutil", "qe", channel, f"/c:{count}", "/rd:true", "/f:xml"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return []
        return parse_events_xml(r.stdout) if r.returncode == 0 else []


class FakeSource:
    """Synthetic source for tests and the demo: {(hive, path): {"values": {...}, "subkeys": [...], "lw": int}}."""

    def __init__(self, keys, text="", events=None):
        self.keys, self._text, self._events, self.denied = keys, text, events or {}, []

    def subkeys(self, hive, path):
        return list(self.keys.get((hive, path), {}).get("subkeys", []))

    def values(self, hive, path):
        return dict(self.keys.get((hive, path), {}).get("values", {}))

    def last_write(self, hive, path):
        return self.keys.get((hive, path), {}).get("lw")

    def text(self, path):
        return self._text

    def setupapi(self):
        return [("setupapi.dev.log", self._text)]

    def events(self, channel, count=2000):
        return list(self._events.get(channel, []))


# ---------------------------------------------------------------- decoding helpers
def filetime(v):
    """FILETIME as int, or 8 little-endian bytes -> 'YYYY-MM-DD HH:MM:SS' UTC."""
    if isinstance(v, (bytes, bytearray)) and len(v) >= 8:
        v = int.from_bytes(bytes(v[:8]), "little")
    if not isinstance(v, int) or v <= 0:
        return ""
    try:
        return (dt.datetime(1601, 1, 1) + dt.timedelta(microseconds=v // 10)).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, ValueError):
        return ""


def bt_name(v):
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).split(b"\x00", 1)[0].decode("utf-8", "replace")
    return str(v or "")


def mac(key):
    k = re.sub(r"[^0-9a-fA-F]", "", key or "")
    return ":".join(k[i:i + 2] for i in range(0, 12, 2)).upper() if len(k) == 12 else (key or "")


def bt_class(cod):
    if not isinstance(cod, int):
        return "", False
    major = (cod >> 8) & 0x1F
    return f"{BT_MAJOR.get(major, f'major {major}')} (COD 0x{cod:06x})", major == 2


def multi(v):
    return "; ".join(v) if isinstance(v, (list, tuple)) else str(v or "")


def clean_desc(v):
    """'@oem12.inf,%device%;Galaxy S24' -> 'Galaxy S24' (the text after the last ';')."""
    s = multi(v)
    return s.rsplit(";", 1)[-1] if s.startswith("@") else s


def parse_events_xml(xml_text):
    out = []
    ns = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
    try:
        root = ET.fromstring("<Events>" + xml_text + "</Events>")
    except ET.ParseError:
        return out
    for ev in root.findall("e:Event", ns):
        sysn = ev.find("e:System", ns)
        if sysn is None:
            continue
        eid = sysn.findtext("e:EventID", default="", namespaces=ns)
        tc = sysn.find("e:TimeCreated", ns)
        t = (tc.get("SystemTime") or "")[:19].replace("T", " ") if tc is not None else ""
        data = {}
        for d in ev.iter("{http://schemas.microsoft.com/win/2004/08/events/event}Data"):
            data[d.get("Name") or f"Data{len(data)}"] = (d.text or "").strip()
        out.append((t, eid, data))
    return out


def parse_setupapi(text):
    """[(device id, section start as logged (local time), section title)] for device installs."""
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r">>>\s+\[(Device Install[^\]]*?) - (.+?)\]\s*$", line)
        if not m:
            continue
        start = ""
        for nxt in lines[i + 1:i + 4]:
            s = re.match(r">>>\s+Section start (\d{4}/\d\d/\d\d \d\d:\d\d:\d\d)", nxt)
            if s:
                start = s.group(1).replace("/", "-")
                break
        out.append((m.group(2).strip(), start, m.group(1).strip()))
    return out


def local_to_utc(s):
    """setupapi.dev.log logs local time; convert with this PC's current UTC offset (labelled as such)."""
    try:
        t = dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").astimezone()
        return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""


# ---------------------------------------------------------------- build
def build(src, g, View):
    """Add the 'Phone connections (this PC)' views to graph g. Returns view ids."""
    vids, by_container, by_instance = [], {}, {}

    def add(vid, title, basis, cols, rows, entity=True):
        v = View(vid, title, "conn", basis, cols, rows)
        g.add_view(v, "_e" if entity else None)
        vids.append(vid)

    def remember(e, container, instance):
        if container:
            by_container.setdefault(container.lower(), []).append(e)
        if instance:
            by_instance[instance.upper()] = e

    install = {}   # device id -> (earliest start as logged, section, log file)
    for fname, text in src.setupapi():
        for devid, start, title in parse_setupapi(text):
            k = devid.upper()
            if start and (k not in install or start < install[k][0]):
                install[k] = (start, title, fname)

    # USB
    rows = []
    for dev in src.subkeys(HKLM, USB):
        vm = re.search(r"VID_([0-9A-Fa-f]{4})", dev)
        pm = re.search(r"PID_([0-9A-Fa-f]{4})", dev)
        for inst in src.subkeys(HKLM, f"{USB}\\{dev}"):
            path = f"{USB}\\{dev}\\{inst}"
            v = src.values(HKLM, path)
            iid = f"USB\\{dev}\\{inst}"
            e = ("usb", iid)
            svc = str(v.get("Service") or "")
            first = install.get(iid.upper(), ("",))[0]
            name = clean_desc(v.get("FriendlyName")) or clean_desc(v.get("DeviceDesc"))
            g.label[e] = f"USB {name} ({vm.group(1) if vm else '?'}:{pm.group(1) if pm else '?'})"
            rows.append({"_e": e, "Name": name, "Manufacturer": clean_desc(v.get("Mfg")),
                         "VID": vm.group(1).upper() if vm else "", "PID": pm.group(1).upper() if pm else "",
                         "SerialOrInstance": inst, "Driver": svc,
                         "PhoneEvidence": PHONE_SERVICES.get(svc, ""), "ContainerID": v.get("ContainerID", ""),
                         "Location": v.get("LocationInformation", ""), "FirstInstallLocal": first,
                         "FirstInstallUTC": local_to_utc(first), "KeyLastWriteUTC": filetime(src.last_write(HKLM, path)),
                         "HardwareID": multi(v.get("HardwareID")), "InstanceID": iid})
            remember(e, v.get("ContainerID"), iid)
    add("C_usb", "USB devices (cable)", "HKLM\\" + USB + " - every USB device ever installed; PhoneEvidence shows "
        "why a row looks like a phone (driver service). FirstInstall from setupapi.dev.log (local time as logged)",
        ["Name", "Manufacturer", "VID", "PID", "SerialOrInstance", "Driver", "PhoneEvidence", "ContainerID",
         "Location", "FirstInstallLocal", "FirstInstallUTC", "KeyLastWriteUTC", "HardwareID", "InstanceID"], rows)

    # portable devices (MTP)
    names = {k.upper(): src.values(HKLM, f"{WPD_NAMES}\\{k}").get("FriendlyName", "")
             for k in src.subkeys(HKLM, WPD_NAMES)}
    rows = []
    for inst in src.subkeys(HKLM, WPD):
        path = f"{WPD}\\{inst}"
        v = src.values(HKLM, path)
        iid = f"SWD\\WPDBUSENUM\\{inst}"
        e = ("wpd", iid)
        fname = clean_desc(v.get("FriendlyName")) or next((n for k, n in names.items() if k in inst.upper()), "")
        g.label[e] = f"portable device {fname}"
        first = install.get(iid.upper(), ("",))[0]
        rows.append({"_e": e, "Name": fname, "Manufacturer": clean_desc(v.get("Mfg")),
                     "Description": clean_desc(v.get("DeviceDesc")), "Driver": v.get("Service", ""),
                     "ContainerID": v.get("ContainerID", ""), "FirstInstallLocal": first,
                     "FirstInstallUTC": local_to_utc(first), "KeyLastWriteUTC": filetime(src.last_write(HKLM, path)),
                     "InstanceID": iid})
        remember(e, v.get("ContainerID"), iid)
    add("C_wpd", "Portable devices (MTP phones, cameras, volumes)",
        "HKLM\\" + WPD + " and HKLM\\" + WPD_NAMES + " - what Windows showed in This PC when the device was plugged in",
        ["Name", "Manufacturer", "Description", "Driver", "ContainerID", "FirstInstallLocal", "FirstInstallUTC",
         "KeyLastWriteUTC", "InstanceID"], rows)

    # Bluetooth paired / seen
    rows, bt = [], {}
    for key in src.subkeys(HKLM, BTPORT):
        path = f"{BTPORT}\\{key}"
        v = src.values(HKLM, path)
        cls, is_phone = bt_class(v.get("COD"))
        e = ("bt", key.lower())
        name = bt_name(v.get("Name")) or bt_name(v.get("FriendlyName")) or bt_name(v.get("LEName"))
        g.label[e] = f"Bluetooth {name or mac(key)}"
        bt[key.lower()] = e
        rows.append({"_e": e, "Name": name, "FriendlyName": bt_name(v.get("FriendlyName")),
                     "LEName": bt_name(v.get("LEName")), "Address": mac(key), "Class": cls,
                     "IsPhone": "yes" if is_phone else "", "LastConnectedUTC": filetime(v.get("LastConnected")),
                     "LastSeenUTC": filetime(v.get("LastSeen")), "ManufacturerId": v.get("ManufacturerId", ""),
                     "LMPVersion": v.get("LmpVersion", ""), "VID": v.get("VID", ""), "PID": v.get("PID", ""),
                     "KeyLastWriteUTC": filetime(src.last_write(HKLM, path))})
        remember(e, v.get("LeContainerId"), None)
    add("C_bt", "Bluetooth devices (paired or seen)",
        "HKLM\\" + BTPORT + " - name, class of device (Phone = major class 2) and LastSeen / LastConnected as Windows "
        "stored them. Link keys are never read", ["Name", "FriendlyName", "LEName", "Address", "Class", "IsPhone",
                                                 "LastConnectedUTC", "LastSeenUTC", "ManufacturerId", "LMPVersion",
                                                 "VID", "PID", "KeyLastWriteUTC"], rows)

    # Bluetooth services
    rows = []
    for svc_key in src.subkeys(HKLM, BTENUM):
        um = re.search(r"\{0000([0-9a-fA-F]{4})-0000-1000-8000-00805f9b34fb\}", svc_key)
        uuid = um.group(1).lower() if um else ""
        for inst in src.subkeys(HKLM, f"{BTENUM}\\{svc_key}"):
            path = f"{BTENUM}\\{svc_key}\\{inst}"
            v = src.values(HKLM, path)
            am = re.search(r"[&_]([0-9A-Fa-f]{12})(?:_|$)", inst)   # ...&0&<MAC>_C00000000
            addr = am.group(1).lower() if am else ""
            iid = f"BTHENUM\\{svc_key}\\{inst}"
            e = ("btsvc", iid)
            u128 = re.match(r"\{([0-9a-fA-F-]{36})\}", svc_key)
            service = BT_SERVICES.get(uuid, f"UUID {uuid}" if uuid else
                                      "device node" if svc_key.upper().startswith("DEV_") else
                                      f"vendor-specific service {u128.group(1)}" if u128 else svc_key)
            g.label[e] = f"Bluetooth service {service} on {mac(addr) if addr else '?'}"
            rows.append({"_e": e, "Service": service, "Address": mac(addr) if addr else "",
                         "Name": clean_desc(v.get("FriendlyName")) or clean_desc(v.get("DeviceDesc")),
                         "Driver": v.get("Service", ""), "ContainerID": v.get("ContainerID", ""),
                         "KeyLastWriteUTC": filetime(src.last_write(HKLM, path)), "InstanceID": iid})
            remember(e, v.get("ContainerID"), iid)
            if addr in bt:
                g.link("CR2", bt[addr], e, f"Stored link: the service instance carries this device's address ({service})")
    add("C_btsvc", "Bluetooth services per device",
        "HKLM\\" + BTENUM + " - one row per profile a device offered (Hands-Free, Phonebook, Message Access, ...); names "
        "from the Bluetooth SIG assigned numbers", ["Service", "Address", "Name", "Driver", "ContainerID",
                                                   "KeyLastWriteUTC", "InstanceID"], rows)

    # AutoPlay known devices
    rows = []
    for key in src.subkeys(HKCU, KNOWN):
        path = f"{KNOWN}\\{key}"
        v = src.values(HKCU, path)
        e = ("autoplay", key)
        g.label[e] = f"AutoPlay {v.get('Label', key)}"
        rows.append({"_e": e, "Label": v.get("Label", ""), "ContainerID": v.get("ContainerID", ""),
                     "KeyLastWriteUTC": filetime(src.last_write(HKCU, path)), "Key": key})
        remember(e, v.get("ContainerID"), None)
    add("C_autoplay", "AutoPlay known devices (this user)",
        "HKCU\\" + KNOWN + " - devices this Windows user plugged in, with the label AutoPlay showed",
        ["Label", "ContainerID", "KeyLastWriteUTC", "Key"], rows)

    # first installs
    rows = []
    for devid, (start, title, fname) in install.items():
        if not devid.startswith(DEVICE_PREFIXES):
            continue
        dev = by_instance.get(devid)
        ie = ("install", devid)
        g.label[ie] = f"first install {start} - {devid[:60]}"
        if dev:
            g.link("CR3", dev, ie, "Stored link: setupapi.dev.log names this device id - its first driver install")
        rows.append({"_e": ie, "DeviceID": devid, "FirstInstallLocal": start, "FirstInstallUTC": local_to_utc(start),
                     "Section": title, "Log": fname, "MatchedDevice": g.label.get(dev, "") if dev else ""})
    rows.sort(key=lambda r: r["FirstInstallLocal"])
    add("C_install", "First driver install (setupapi.dev.log)",
        SETUPAPI + " and the archived setupapi.dev.<date>.log copies beside it - the first time Windows installed a "
        "driver for each device id (earliest across all logs); local time as logged, UTC with the current offset",
        ["FirstInstallLocal", "FirstInstallUTC", "DeviceID", "Section", "Log", "MatchedDevice"], rows)

    # events
    rows = []
    for ch in CHANNELS:
        for t, eid, data in src.events(ch):
            devid = next((x for x in data.values() if x.upper().startswith(DEVICE_PREFIXES)), "")
            if ch.endswith("Kernel-PnP/Configuration") and not devid:
                continue
            dev = by_instance.get(devid.upper()) if devid else None
            ee = ("event", f"{ch}|{len(rows)}")
            log = ch.split("/")[0].replace("Microsoft-Windows-", "")
            g.label[ee] = f"event {eid} {log} {t}"
            if dev:
                g.link("CR4", dev, ee, f"Stored link: event {eid} in {log} names this device id")
            rows.append({"_e": ee, "TimeUTC": t, "Log": log, "EventID": eid, "DeviceID": devid,
                         "MatchedDevice": g.label.get(dev, "") if dev else "",
                         "Data": "; ".join(f"{k}={v}" for k, v in data.items() if v and v != devid)[:300]})
    rows.sort(key=lambda r: r["TimeUTC"], reverse=True)
    add("C_events", "Connection events (event logs)",
        "Kernel-PnP/Configuration (device configured / started), WPD-MTP and Bluetooth LE pairing logs. Logs roll over, "
        "so only recent connections remain", ["TimeUTC", "Log", "EventID", "DeviceID", "MatchedDevice", "Data"], rows)

    # same physical device across sources (ContainerID)
    rel = []
    for cid, ents in by_container.items():
        if len(ents) < 2:
            continue
        for i, a in enumerate(ents):
            for b in ents[i + 1:]:
                g.link("CR1", a, b, "Stored link: same ContainerID - the same physical device")
        for a in ents:
            rel.append({"_e": a, "ContainerID": cid, "Source": a[0], "Device": g.label.get(a, str(a))})
    add("CR1", "Same physical device across USB, MTP, Bluetooth and AutoPlay (ContainerID)",
        "rows that share a ContainerID - Windows' own id for one physical device", ["ContainerID", "Source", "Device"],
        rel, entity=False)

    phone_containers = {str(r["ContainerID"]).lower() for r in g.views["C_usb"].rows
                        if r["PhoneEvidence"] and r["ContainerID"]}
    phones = []
    for vid in ("C_usb", "C_wpd", "C_bt"):
        for r in g.views[vid].rows:
            why = r.get("PhoneEvidence") or ("Bluetooth class of device = Phone" if r.get("IsPhone") == "yes" else "") or \
                ("same ContainerID as a USB phone" if vid == "C_wpd" and str(r.get("ContainerID", "")).lower()
                 in phone_containers else "")
            if why:
                phones.append({"_e": r["_e"], "How": {"C_usb": "USB cable", "C_wpd": "USB cable (MTP)",
                                                      "C_bt": "Bluetooth"}[vid], "Device": g.label[r["_e"]],
                               "Evidence": why, "LastConnectedUTC": r.get("LastConnectedUTC", ""),
                               "LastSeenUTC": r.get("LastSeenUTC", ""),
                               "KeyLastWriteUTC": r.get("KeyLastWriteUTC", ""),
                               "FirstInstallUTC": r.get("FirstInstallUTC", "")})
    add("C_phones", "Phones connected to this PC (cable or Bluetooth) - summary",
        "every row above that looks like a phone, with the evidence for it: a phone driver on USB, the MTP portable-"
        "device driver, or Bluetooth class of device = Phone. An empty LastConnected with a LastSeen means Windows "
        "saw the phone but stored no connection time", ["How", "Device", "Evidence", "LastConnectedUTC", "LastSeenUTC",
                                                        "FirstInstallUTC", "KeyLastWriteUTC"], phones,
        entity=False)
    if getattr(src, "denied", None):
        add("C_denied", "Locations this account could not read", "access denied - run as administrator to include them",
            ["Location"], [{"Location": d} for d in sorted(set(src.denied))], entity=False)
    return vids


class CachedSource:
    """Wrap a source so the registry, logs and event logs are read once per viewer session."""

    def __init__(self, src):
        self.src, self.memo = src, {}

    @property
    def denied(self):
        return getattr(self.src, "denied", [])

    def _get(self, name, *args):
        k = (name,) + args
        if k not in self.memo:
            self.memo[k] = getattr(self.src, name)(*args)
        return self.memo[k]

    def subkeys(self, hive, path):
        return self._get("subkeys", hive, path)

    def values(self, hive, path):
        return self._get("values", hive, path)

    def last_write(self, hive, path):
        return self._get("last_write", hive, path)

    def text(self, path):
        return self._get("text", path)

    def setupapi(self):
        return self._get("setupapi")

    def events(self, channel, count=2000):
        return self._get("events", channel, count)


def demo_source():
    """Synthetic registry / logs: one phone seen over USB (MTP) and Bluetooth, one headset, one USB stick.

    Values are invented: VID 1234, documentation MAC range 00:00:5E:00:53:xx (RFC 7042), example names.
    """
    ft = 133500000000000000                     # a FILETIME in 2024
    phone_c, stick_c = "{e5e5e5e5-0000-4000-a000-000000000001}", "{e5e5e5e5-0000-4000-a000-000000000002}"
    k = {}

    def key(hive, path, values=None, subkeys=(), lw=ft):
        k[(hive, path)] = {"values": values or {}, "subkeys": list(subkeys), "lw": lw}

    key(HKLM, USB, subkeys=["VID_1234&PID_0F01", "VID_1234&PID_0F02"])
    key(HKLM, USB + r"\VID_1234&PID_0F01", subkeys=["EXAMPLEPHONE01"])
    key(HKLM, USB + r"\VID_1234&PID_0F01\EXAMPLEPHONE01",
        {"DeviceDesc": "@oem1.inf,%phone%;Example Phone", "Mfg": "ExampleCorp", "Service": "WUDFWpdMtp",
         "ContainerID": phone_c, "HardwareID": ["USB\\VID_1234&PID_0F01"]}, lw=ft + 10 ** 9)
    key(HKLM, USB + r"\VID_1234&PID_0F02", subkeys=["EXAMPLESTICK01"])
    key(HKLM, USB + r"\VID_1234&PID_0F02\EXAMPLESTICK01",
        {"DeviceDesc": "USB Mass Storage Device", "Mfg": "ExampleCorp", "Service": "USBSTOR", "ContainerID": stick_c})
    key(HKLM, WPD, subkeys=["EXAMPLE_MTP_PHONE"])
    key(HKLM, WPD + r"\EXAMPLE_MTP_PHONE", {"FriendlyName": "Example Phone", "Mfg": "ExampleCorp",
                                             "Service": "WUDFWpdFs", "ContainerID": phone_c})
    key(HKLM, WPD_NAMES, subkeys=["EXAMPLE_MTP_PHONE"])
    key(HKLM, WPD_NAMES + r"\EXAMPLE_MTP_PHONE", {"FriendlyName": "Example Phone"})
    key(HKLM, BTPORT, subkeys=["00005e005301", "00005e005302"])
    key(HKLM, BTPORT + r"\00005e005301", {"Name": b"Example Phone\x00", "COD": 0x000204, "LastSeen": ft + 2 * 10 ** 9,
                                          "LastConnected": ft + 2 * 10 ** 9, "ManufacturerId": 117, "LmpVersion": 10})
    key(HKLM, BTPORT + r"\00005e005302", {"Name": b"Example Headset\x00", "COD": 0x000418, "LastSeen": ft,
                                          "LastConnected": 0})
    key(HKLM, BTENUM, subkeys=["{0000111f-0000-1000-8000-00805f9b34fb}_EXAMPLEMFG&0000",
                               "{0000112f-0000-1000-8000-00805f9b34fb}_EXAMPLEMFG&0000"])
    for svc in ("111f", "112f"):
        sk = "{0000%s-0000-1000-8000-00805f9b34fb}_EXAMPLEMFG&0000" % svc
        key(HKLM, BTENUM + "\\" + sk, subkeys=["7&1a2b3c4d&0&00005E005301_C00000000"])
        key(HKLM, BTENUM + "\\" + sk + "\\7&1a2b3c4d&0&00005E005301_C00000000",
            {"DeviceDesc": "Example Phone", "ContainerID": "{e5e5e5e5-0000-4000-a000-000000000003}"})
    key(HKCU, KNOWN, subkeys=["EXAMPLE_KNOWN_1"])
    key(HKCU, KNOWN + r"\EXAMPLE_KNOWN_1", {"Label": "Example Phone", "ContainerID": phone_c})
    key(HKLM, r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Keys",
        {"secret": b"MUST-NOT-BE-READ"}, ["00005e005300"])
    setup = ("\n>>>  [Device Install (Hardware initiated) - USB\\VID_1234&PID_0F01\\EXAMPLEPHONE01]\n"
             ">>>  Section start 2030/01/01 12:00:00.000\n<<<  Section end 2030/01/01 12:00:05.000\n")
    events = {"Microsoft-Windows-Kernel-PnP/Configuration": [
        ("2030-01-02 09:00:00", "410", {"DeviceInstanceId": "USB\\VID_1234&PID_0F01\\EXAMPLEPHONE01"}),
        ("2030-01-02 09:00:01", "410", {"DeviceInstanceId": "PCI\\EXAMPLE_NOT_A_PHONE"})]}
    return FakeSource(k, setup, events)
