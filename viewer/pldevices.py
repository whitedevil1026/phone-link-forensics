"""
pldevices.py - every phone ever linked to this PC through Phone Link.

Sources (all under ...\\Microsoft.YourPhone_8wekyb3d8bbwe\\LocalCache\\):

  Indexed\\<GUID>\\                 one folder per pairing. The folder's creation time is when that
                                   phone was linked; any databases left inside hold its data. Phone
                                   Link empties a folder when a phone is removed, but the folder (and
                                   its GUID and times) stays - evidence of a pairing even with no data.
  DeviceMetadataStorage.json       phone details per hardware ID: display name, manufacturer, model,
                                   Android version, Phone Link client version, capabilities, linked,
                                   registration and last-seen times. A hardware ID with no entry is a
                                   phone whose details were removed.
  YppCryptoTrustRelationships      trust relationships with partner devices: partner id, trust type,
                                   last access, key expiry, Microsoft account id (MsaCid).
  Indexed\\FullTrust\\phone_apps.db  hosted_app_ids: apps streamed to the PC, per device_id.

Not read: PlatformEncryptedKeyStorage.json (encryption keys, no device details) and certificate
bodies. Only the certificate count is shown.

Everything is copied to a temporary folder first, each copy is re-hashed against its source,
and databases are opened read-only. Standard library only.
"""
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile

GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
META, TRUST, FULLTRUST = "DeviceMetadataStorage.json", "YppCryptoTrustRelationships", "phone_apps.db"
DATA_TABLES = {"calling.db": ["call_history"], "phone.db": ["message", "conversation", "mms"],
               "contacts.db": ["contact"], "photos.db": ["media"], "phoneapps.db": ["phone_apps"],
               "notifications.db": ["notifications"], "sharedcontent.db": ["sharedcontent", "shareduri"]}
INFER_WINDOW_S = 600   # metadata registration this close before a folder's creation -> inferred same pairing


def find_localcache(path):
    """LocalCache folder for an input path (package root, LocalCache, Indexed or a device folder), or None."""
    p = os.path.abspath(os.path.expandvars(path))
    for _ in range(6):
        for cand in (p, os.path.join(p, "LocalCache")):
            if os.path.isdir(os.path.join(cand, "Indexed")) or os.path.isfile(os.path.join(cand, META)):
                return cand
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return None


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _utc_ts(ts):
    try:
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError, TypeError):
        return ""


def _iso(s):
    """'2026-09-21T17:17:49.123+00:00' -> ('2026-09-21 17:17:49', epoch seconds)."""
    if not s:
        return "", None
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")[:32])
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        t = t.astimezone(dt.timezone.utc)
        return t.strftime("%Y-%m-%d %H:%M:%S"), t.timestamp()
    except ValueError:
        m = re.match(r"(\d{4}-\d\d-\d\d)T(\d\d:\d\d:\d\d)", str(s))
        return ((m.group(1) + " " + m.group(2)) if m else str(s)), None


class PackageCopy:
    """Copy the parts of LocalCache this module reads; verify every copy; never touch the source again."""

    def __init__(self, localcache):
        self.src = os.path.abspath(localcache)
        self.work = tempfile.mkdtemp(prefix="pldevices-")
        self.verify = []
        self.folders = {}  # guid -> {"created": ts, "modified": ts, "files": [...]}
        idx = os.path.join(self.src, "Indexed")
        for name in (META, TRUST):
            self._copy(os.path.join(self.src, name), os.path.join(self.work, name))
        if os.path.isdir(idx):
            for g in sorted(os.listdir(idx)):
                sp = os.path.join(idx, g)
                if not os.path.isdir(sp):
                    continue
                st = os.stat(sp)
                info = {"created": getattr(st, "st_birthtime", st.st_ctime), "modified": st.st_mtime, "files": []}
                for root, _, files in os.walk(sp):
                    for f in files:
                        rel = os.path.relpath(os.path.join(root, f), sp)
                        dst = os.path.join(self.work, "Indexed", g, rel)
                        if self._copy(os.path.join(root, f), dst):
                            info["files"].append(rel)
                self.folders[g] = info
        if any(v["copy_matches"] != "yes" for v in self.verify):
            raise IOError("a copy does not match its source hash")

    def _copy(self, src, dst):
        if not os.path.isfile(src):
            return False
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        a, b = _sha(src), _sha(dst)
        self.verify.append({"file": os.path.relpath(src, self.src), "source_sha256": a,
                            "copy_matches": "yes" if a == b else "NO"})
        return True

    def path(self, *parts):
        return os.path.join(self.work, *parts)

    def close(self):
        """Remove the copies. Returns the folder if it could not be removed (so the caller can warn)."""
        shutil.rmtree(self.work, ignore_errors=True)
        return self.work if os.path.exists(self.work) else None


def _ro(path):
    c = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True)
    c.execute("PRAGMA query_only = ON")
    return c


def _db_summary(pc, guid, info, ts_seconds):
    """Per-device database summary: databases present, data rows, activity range, SIM."""
    dbs = sorted({os.path.basename(f) for f in info["files"] if f.endswith(".db")})
    rows, first, last, sim, details = 0, None, None, "", []
    for db in dbs:
        p = None
        for f in info["files"]:
            if os.path.basename(f) == db:
                p = pc.path("Indexed", guid, f)
        try:
            c = _ro(p)
        except sqlite3.Error:
            details.append(f"{db}: unreadable")
            continue
        try:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            n_db = 0
            for t in DATA_TABLES.get(db, []):
                if t in tables:
                    n = c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                    n_db += n
            rows += n_db
            details.append(f"{db}: {n_db} rows")
            for t, col in (("call_history", "start_time"), ("message", "timestamp"), ("media", "taken_time")):
                if t in tables:
                    lo, hi = c.execute(f'SELECT min("{col}"), max("{col}") FROM "{t}" WHERE "{col}" > 0').fetchone()
                    lo, hi = ts_seconds(lo), ts_seconds(hi)
                    if lo is not None:
                        first = lo if first is None else min(first, lo)
                    if hi is not None:
                        last = hi if last is None else max(last, hi)
            if "subscription" in tables:
                s = c.execute("SELECT name, number FROM subscription ORDER BY subscription_id LIMIT 1").fetchone()
                if s:
                    sim = " ".join(str(x) for x in s if x)
        except sqlite3.Error as e:
            details.append(f"{db}: {e}")
        finally:
            c.close()
    if not info["files"]:
        status = "folder only - its data was removed"
    elif rows == 0:
        status = "databases present but empty"
    else:
        status = "data present"
    return dbs, rows, first, last, sim, status, details


def build(localcache, g, View, ts_seconds, iso):
    """Add the 'Linked phones (this PC)' views to graph g. Returns (vids, PackageCopy or None)."""
    if not localcache:
        v = View("PC_devices", "Phone pairings (device folders)", "pc",
                 "only one device folder was given, so the package-level files that list every linked phone are "
                 "not available. Open the Phone Link package folder to see all pairings",
                 ["Note"], [{"Note": "Open ...\\Packages\\Microsoft.YourPhone_8wekyb3d8bbwe to list every linked phone"}])
        g.add_view(v)
        return [v.vid], None
    pc = PackageCopy(localcache)
    vids = []

    def add(vid, title, basis, cols, rows, entity=True):
        v = View(vid, title, "pc", basis, cols, rows)
        g.add_view(v, "_e" if entity else None)
        vids.append(vid)

    # ---------- streamed apps per device (FullTrust)
    streamed, by_dev = [], {}
    ft = pc.path("Indexed", "FullTrust", FULLTRUST)
    if os.path.isfile(ft):
        c = _ro(ft)
        try:
            cols = [r[1] for r in c.execute("PRAGMA table_info(hosted_app_ids)")]
            if cols:
                for r in c.execute("SELECT * FROM hosted_app_ids"):
                    d = dict(zip(cols, r))
                    e = ("streamed", d.get("row_id"))
                    dev = str(d.get("device_id") or "").lower()
                    by_dev.setdefault(dev, []).append(e)
                    streamed.append({"_e": e, **{k: d[k] for k in cols}})
                    g.label[e] = f"streamed app {d.get('package_id')}"
        finally:
            c.close()

    # ---------- device folders
    order = sorted((v["created"], k) for k, v in pc.folders.items() if GUID.match(k))
    folder_rows = []
    for n, (created, guid) in enumerate(order, 1):
        info = pc.folders[guid]
        dbs, rows, first, last, sim, status, details = _db_summary(pc, guid, info, ts_seconds)
        e = ("pairing", guid)
        g.label[e] = f"pairing #{n} - folder {guid[:8]} (created {_utc_ts(created)[:10]})"
        g.time[e] = created
        folder_rows.append({"_e": e, "#": n, "DeviceFolder": guid, "FolderCreatedUTC": _utc_ts(created),
                            "FolderModifiedUTC": _utc_ts(info["modified"]), "Status": status,
                            "Databases": ", ".join(dbs), "DataRows": rows,
                            "FirstActivityUTC": iso(first), "LastActivityUTC": iso(last), "SIM": sim,
                            "StreamedApps": len(by_dev.get(guid.lower(), [])), "Detail": "; ".join(details)})
    add("PC_devices", "Phone pairings (device folders)",
        "one folder per pairing under LocalCache\\Indexed; created time = when that phone was linked. Empty "
        "folders are pairings whose data Phone Link removed",
        ["#", "DeviceFolder", "FolderCreatedUTC", "FolderModifiedUTC", "Status", "Databases", "DataRows",
         "FirstActivityUTC", "LastActivityUTC", "SIM", "StreamedApps", "Detail"], folder_rows)

    # ---------- phone details (metadata)
    meta_rows, mids = [], {}
    if os.path.isfile(pc.path(META)):
        with open(pc.path(META), encoding="utf-8-sig") as f:
            meta = json.load(f)
        for hw, entries in (meta.get("DeviceMetadatas") or {}).items():
            if not entries:
                e = ("phone", hw)
                g.label[e] = f"phone {hw} (details removed)"
                meta_rows.append({"_e": e, "HardwareID": hw, "Linked": "", "DisplayName": "",
                                  "Note": "hardware ID kept, details removed - a phone that was unlinked"})
                continue
            for x in entries:
                m = x.get("Metadata") or {}
                inner = m.get("Metadata") or {}
                reg, reg_ts = _iso(m.get("RegitrationTime") or m.get("RegistrationTime"))  # Microsoft's spelling
                seen, _ = _iso(m.get("LastSeenTime"))
                e = ("phone", m.get("Id") or hw)
                mids[m.get("Id")] = e
                g.label[e] = f"phone {inner.get('DisplayName') or inner.get('ModelName') or hw}"
                g.time[e] = reg_ts
                meta_rows.append({
                    "_e": e, "_reg_ts": reg_ts, "HardwareID": hw, "Linked": "yes" if x.get("IsLinked") else "no",
                    "DisplayName": inner.get("DisplayName"), "Manufacturer": inner.get("Manufacture"),
                    "Model": inner.get("ModelName"), "ModelVersion": inner.get("ModelVersion"),
                    "OS": " ".join(str(v) for v in (inner.get("OsName"), inner.get("OsVersion")) if v),
                    "PhoneLinkApp": " ".join(str(v) for v in (inner.get("ClientType"), inner.get("ClientVersion")) if v),
                    "Enabled": inner.get("IsEnabled"), "ConsentVersion": inner.get("ConsentVersion"),
                    "Capabilities": ", ".join(inner.get("Capabilities") or []), "RegisteredUTC": reg,
                    "LastSeenUTC": seen, "MetadataID": m.get("Id"), "DistinguishedDeviceID": m.get("DistinguishedDeviceId"),
                    "Certificates": sum(len(v or []) for v in (x.get("Certificates") or {}).values()),
                    "Note": ""})
    add("PC_phones", "Phone details (DeviceMetadataStorage.json)",
        "per hardware ID: the phone's own name, make, model, Android version and Phone Link app; registration and "
        "last-seen times. Certificate bodies are not shown, only their count",
        ["HardwareID", "Linked", "DisplayName", "Manufacturer", "Model", "ModelVersion", "OS", "PhoneLinkApp",
         "Enabled", "ConsentVersion", "Capabilities", "RegisteredUTC", "LastSeenUTC", "MetadataID",
         "DistinguishedDeviceID", "Certificates", "Note"], meta_rows)

    # ---------- trust relationships
    trust_rows = []
    if os.path.isfile(pc.path(TRUST)):
        with open(pc.path(TRUST), encoding="utf-8-sig") as f:
            trust = json.load(f)
        for key, t in (trust.get("TrustedClients") or {}).items():
            e = ("trust", key)
            last, last_ts = _iso(t.get("RelationshipLastAccessTime"))
            exp, _ = _iso(t.get("PartnerKeyExpirationTime"))
            g.label[e] = f"trust relationship type {t.get('TrustType')} (last access {last[:10]})"
            g.time[e] = last_ts
            trust_rows.append({"_e": e, "PartnerClientID": t.get("PartnerClientId"), "TrustType": t.get("TrustType"),
                               "LastAccessUTC": last, "KeyExpiresUTC": exp, "MsaCid": t.get("MsaCid"),
                               "SelfClientID": t.get("SelfClientId"),
                               "DcgClientID": (t.get("Attributes") or {}).get("dcg_prod_client_id")})
            # PartnerClientId is stored as '<prefix>_<id>'; the part after the last '_' is the metadata Id
            core = str(t.get("PartnerClientId") or "").rsplit("_", 1)[-1].lower()
            hit = next((pe for mid, pe in mids.items() if mid and str(mid).lower() == core), None)
            if hit:
                g.link("PR1", hit, e, "Stored link: this trust relationship's partner ID contains the phone's "
                                      "metadata ID")
    add("PC_trust", "Trust relationships (YppCryptoTrustRelationships)",
        "cryptographic trust with partner devices: last access, key expiry, trust type and the Microsoft account "
        "id (MsaCid). Certificates are not shown",
        ["PartnerClientID", "TrustType", "LastAccessUTC", "KeyExpiresUTC", "MsaCid", "SelfClientID", "DcgClientID"],
        trust_rows)

    add("PC_streamed", "Apps streamed to this PC, per device (FullTrust phone_apps.db)",
        "hosted_app_ids: one row per app a phone streamed to the PC; device_id is the pairing folder's GUID",
        list(streamed[0].keys())[1:] if streamed else ["device_id"], streamed)
    for r in folder_rows:
        for se in by_dev.get(r["DeviceFolder"].lower(), []):
            g.link("PR2", r["_e"], se, "Stored link: hosted_app_ids.device_id is this pairing folder's name")

    # ---------- relations
    rel = []
    for m in meta_rows:
        reg = m.get("_reg_ts")
        if reg is None:
            continue
        near = [(g.time[r["_e"]] - reg, r) for r in folder_rows if 0 <= g.time[r["_e"]] - reg <= INFER_WINDOW_S]
        for gap, r in sorted(near, key=lambda x: x[0])[:1]:   # the closest folder only
            created = g.time[r["_e"]]
            if True:
                g.link("PR3", m["_e"], r["_e"], f"Inferred by time: folder created {int(created - reg)} s after the "
                                                f"phone registered")
                rel.append({"_e": m["_e"], "Phone": g.label[m["_e"]], "Pairing": g.label[r["_e"]],
                            "RegisteredUTC": m["RegisteredUTC"], "FolderCreatedUTC": r["FolderCreatedUTC"],
                            "GapSeconds": int(created - reg), "Basis": "inferred (time) - no stored ID links them"})
    add("PR3", "Phone details -> pairing folder (inferred by time)",
        f"the metadata keeps no folder GUID, so a phone is matched to the closest folder created within "
        f"{INFER_WINDOW_S // 60} minutes after its registration time. An inference, not a stored link",
        ["Phone", "Pairing", "RegisteredUTC", "FolderCreatedUTC", "GapSeconds", "Basis"], rel, entity=False)

    rel = [{"_e": e, "Phone": g.label[e], "TrustRelationship": g.label[t], "Basis": "stored: PartnerClientId contains metadata Id"}
           for e in mids.values() for rid, t, _ in g.links(e) if rid == "PR1"]
    add("PR1", "Phone details -> trust relationship (stored link)",
        "YppCryptoTrustRelationships PartnerClientId ('<prefix>_<id>') whose id part equals DeviceMetadataStorage "
        "Metadata.Id",
        ["Phone", "TrustRelationship", "Basis"], rel, entity=False)

    rel = [{"_e": r["_e"], "Pairing": g.label[r["_e"]], "StreamedApps": r["StreamedApps"],
            "Basis": "stored: hosted_app_ids.device_id = folder name"} for r in folder_rows if r["StreamedApps"]]
    add("PR2", "Pairing folder -> streamed apps (stored link)",
        "FullTrust phone_apps.db hosted_app_ids.device_id equal to the pairing folder's GUID",
        ["Pairing", "StreamedApps", "Basis"], rel, entity=False)

    events = []
    for r in folder_rows:
        events.append((g.time[r["_e"]], "phone linked (pairing folder created)", g.label[r["_e"]], r["_e"]))
    for m in meta_rows:
        if m.get("RegisteredUTC"):
            events.append((m.get("_reg_ts"), "phone registered (metadata)", g.label[m["_e"]], m["_e"]))
        if m.get("LastSeenUTC"):
            events.append((_iso(m["LastSeenUTC"])[1], "phone last seen (metadata)", g.label[m["_e"]], m["_e"]))
    for t in trust_rows:
        events.append((g.time.get(t["_e"]), "trust relationship last used", g.label[t["_e"]], t["_e"]))
    rows = [{"_e": e, "TimeUTC": _utc_ts(ts) if ts is not None else "", "Event": what, "Detail": detail}
            for ts, what, detail, e in sorted((x for x in events if x[0] is not None), key=lambda x: x[0])]
    add("PR4", "Pairing history (all linked-phone events in time order)",
        "folder creations, registrations, last-seen and trust times from all the sources above",
        ["TimeUTC", "Event", "Detail"], rows, entity=False)

    v = View("PC_copies", "Linked phones - copies and hashes", "provenance",
             f"copied from {pc.src} to {pc.work} (PlatformEncryptedKeyStorage.json deliberately not copied)",
             ["file", "source_sha256", "copy_matches"], pc.verify)
    g.add_view(v)
    vids.append(v.vid)
    return vids, pc
