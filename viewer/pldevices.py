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
import stat
import tempfile

import plparse

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


_ISO = re.compile(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.(\d+))?\s*(Z|[+-]\d\d(?::?\d\d)?)?")


def _iso(s):
    """'2026-09-21T17:17:49.1234567+05:30' -> ('2026-09-21 11:47:49', epoch seconds).

    Accepts any number of fraction digits (.NET writes 7). A time with no offset is not UTC: it is shown as
    written with ' (no zone)' and no epoch."""
    if not s:
        return "", None
    m = _ISO.fullmatch(str(s).strip())
    if m:
        y, mo, d, h, mi, se = (int(x) for x in m.group(1, 2, 3, 4, 5, 6))
        zone = m.group(8)
        if zone is None:
            return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{se:02d} (no zone)", None
        try:
            if zone == "Z":
                off = 0
            else:
                digits = zone[1:].replace(":", "")
                off = (int(digits[:2]) * 60 + int(digits[2:] or 0)) * (-1 if zone[0] == "-" else 1)
            micro = int((m.group(7) or "")[:6].ljust(6, "0"))
            t = dt.datetime(y, mo, d, h, mi, se, micro, tzinfo=dt.timezone(dt.timedelta(minutes=off)))
            t = t.astimezone(dt.timezone.utc)
            return t.strftime("%Y-%m-%d %H:%M:%S"), t.timestamp()
        except (ValueError, OverflowError):
            pass
    return str(s), None


def _is_link(path):
    """True for a symlink or a Windows junction. These are never followed when copying."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    isj = getattr(os.path, "isjunction", None)   # 3.12+; older Pythons rely on the reparse-point attribute
    reparse = getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(st.st_mode) or bool(reparse) or bool(isj and isj(path))


class PackageCopy:
    """Copy the parts of LocalCache this module reads; verify every copy; never touch the source again.

    Symlinks and junctions are not followed (they could point outside the package): each is listed in
    `skipped` and shown in the copy report. If copying fails, the work folder is removed."""

    def __init__(self, localcache):
        self.src = os.path.abspath(localcache)
        self.work = tempfile.mkdtemp(prefix="pldevices-")
        self.verify = []
        self.skipped = []  # relative paths of symlinks / junctions that were not followed
        self.folders = {}  # guid -> {"created": ts, "modified": ts, "files": [...]}
        try:
            self._copy_all()
        except BaseException:
            shutil.rmtree(self.work, ignore_errors=True)
            raise

    def _copy_all(self):
        idx = os.path.join(self.src, "Indexed")
        for name in (META, TRUST):
            self._copy(os.path.join(self.src, name), os.path.join(self.work, name))
        if os.path.isdir(idx) and not _is_link(idx):
            for g in sorted(os.listdir(idx)):
                sp = os.path.join(idx, g)
                if _is_link(sp):
                    self.skipped.append(os.path.relpath(sp, self.src))
                    continue
                if not os.path.isdir(sp):
                    continue
                st = os.stat(sp)
                info = {"created": getattr(st, "st_birthtime", st.st_ctime), "modified": st.st_mtime, "files": []}
                for root, dirs, files in os.walk(sp):
                    for d in [d for d in dirs if _is_link(os.path.join(root, d))]:
                        dirs.remove(d)    # do not descend
                        self.skipped.append(os.path.relpath(os.path.join(root, d), self.src))
                    for f in files:
                        rel = os.path.relpath(os.path.join(root, f), sp)
                        dst = os.path.join(self.work, "Indexed", g, rel)
                        if self._copy(os.path.join(root, f), dst):
                            info["files"].append(rel)
                self.folders[g] = info
        elif os.path.isdir(idx):
            self.skipped.append("Indexed")
        if any(v["copy_matches"] != "yes" for v in self.verify):
            raise IOError("a copy does not match its source hash")

    def _copy(self, src, dst):
        if _is_link(src):
            self.skipped.append(os.path.relpath(src, self.src))
            return False
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


def _why(e):
    """A short reason for an error row."""
    return f"{type(e).__name__}: {e}"


def _ro(path):
    c = sqlite3.connect(plparse.ro_uri(path), uri=True)
    c.execute("PRAGMA query_only = ON")
    return c


def _db_summary(pc, guid, info, ts_seconds):
    """Per-device database summary: databases present, data rows, activity range, SIM."""
    dbs = sorted({os.path.basename(f) for f in info["files"] if f.endswith(".db")})
    rows, other, bad, first, last, sim, details = 0, 0, 0, None, None, "", []
    for db in dbs:
        p = None
        for f in info["files"]:
            if os.path.basename(f) == db:
                p = pc.path("Indexed", guid, f)
        try:
            c = _ro(p)
        except sqlite3.Error:
            details.append(f"{db}: unreadable")
            bad += 1
            continue
        try:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            n_db = 0
            for t in DATA_TABLES.get(db, []):
                if t in tables:
                    n = c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                    n_db += n
            rows += n_db
            for t in tables - set(DATA_TABLES.get(db, [])):   # rows in any other table are still data
                if not t.startswith("sqlite_"):
                    other += c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
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
            bad += 1
        finally:
            c.close()
    if not info["files"]:
        status = "folder only - its data was removed"
    elif not dbs:
        status = "files present but no databases"
    elif rows:
        status = "data present"
    elif other:
        status = "data in other tables"
    elif bad:
        status = "unreadable database"
    else:
        status = "databases present but empty"
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
    try:
        return _build(pc, g, View, ts_seconds, iso), pc
    except BaseException:
        pc.close()   # never leave the copies behind when a view cannot be built
        raise


SOURCE_ERRORS = (sqlite3.Error, OSError, ValueError, AttributeError, TypeError, KeyError)   # bad file or odd shape


def _build(pc, g, View, ts_seconds, iso):
    vids = []

    def add(vid, title, basis, cols, rows, entity=True):
        v = View(vid, title, "pc", basis, cols, rows)
        g.add_view(v, "_e" if entity else None)
        vids.append(vid)

    # ---------- streamed apps per device (FullTrust)
    streamed, by_dev, streamed_err = [], {}, None
    ft = pc.path("Indexed", "FullTrust", FULLTRUST)
    if os.path.isfile(ft):
        try:
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
        except SOURCE_ERRORS as ex:
            streamed, by_dev, streamed_err = [], {}, f"could not read {FULLTRUST}: {_why(ex)}"

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
    meta_rows, mids, meta_err = [], {}, None
    if os.path.isfile(pc.path(META)):
        try:
            with open(pc.path(META), encoding="utf-8-sig") as f:
                meta = json.load(f)
            metas = meta.get("DeviceMetadatas") or {}
            if not isinstance(metas, dict):
                raise TypeError(f"DeviceMetadatas is a {type(metas).__name__}, expected an object keyed by hardware ID")
        except SOURCE_ERRORS as ex:
            metas, meta_err = {}, f"could not read {META}: {_why(ex)}"
        for hw, entries in metas.items():
            if not entries:
                e = ("phone", hw)
                g.label[e] = f"phone {hw} (details removed)"
                meta_rows.append({"_e": e, "HardwareID": hw, "Linked": "", "DisplayName": "",
                                  "Note": "hardware ID kept, details removed - a phone that was unlinked"})
                continue
            try:
                for x in entries if isinstance(entries, list) else [None]:
                    m = x.get("Metadata") or {}
                    inner = m.get("Metadata") or {}
                    reg, reg_ts = _iso(m.get("RegitrationTime") or m.get("RegistrationTime"))  # Microsoft's spelling
                    seen, seen_ts = _iso(m.get("LastSeenTime"))
                    e = ("phone", m.get("Id") or hw)
                    mids[m.get("Id")] = e
                    g.label[e] = f"phone {inner.get('DisplayName') or inner.get('ModelName') or hw}"
                    g.time[e] = reg_ts
                    meta_rows.append({
                        "_e": e, "_reg_ts": reg_ts, "_seen_ts": seen_ts, "HardwareID": hw,
                        "Linked": "yes" if x.get("IsLinked") else "no",
                        "DisplayName": inner.get("DisplayName"), "Manufacturer": inner.get("Manufacture"),
                        "Model": inner.get("ModelName"), "ModelVersion": inner.get("ModelVersion"),
                        "OS": " ".join(str(v) for v in (inner.get("OsName"), inner.get("OsVersion")) if v),
                        "PhoneLinkApp": " ".join(str(v) for v in (inner.get("ClientType"), inner.get("ClientVersion")) if v),
                        "Enabled": inner.get("IsEnabled"), "ConsentVersion": inner.get("ConsentVersion"),
                        "Capabilities": ", ".join(inner.get("Capabilities") or []), "RegisteredUTC": reg,
                        "LastSeenUTC": seen, "MetadataID": m.get("Id"), "DistinguishedDeviceID": m.get("DistinguishedDeviceId"),
                        "Certificates": sum(len(v or []) for v in (x.get("Certificates") or {}).values()),
                        "Note": ""})
            except SOURCE_ERRORS as ex:
                meta_rows.append({"HardwareID": hw, "Linked": "", "DisplayName": "",
                                  "Note": f"could not read this entry: {_why(ex)}"})
    if meta_err:
        add("PC_phones", "Phone details (DeviceMetadataStorage.json)", meta_err, ["Error"], [{"Error": meta_err}])
    else:
        add("PC_phones", "Phone details (DeviceMetadataStorage.json)",
            "per hardware ID: the phone's own name, make, model, Android version and Phone Link app; registration and "
            "last-seen times. Certificate bodies are not shown, only their count",
            ["HardwareID", "Linked", "DisplayName", "Manufacturer", "Model", "ModelVersion", "OS", "PhoneLinkApp",
             "Enabled", "ConsentVersion", "Capabilities", "RegisteredUTC", "LastSeenUTC", "MetadataID",
             "DistinguishedDeviceID", "Certificates", "Note"], meta_rows)

    # ---------- trust relationships
    trust_rows, trust_err = [], None
    if os.path.isfile(pc.path(TRUST)):
        try:
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
                # PartnerClientId is stored as '<prefix>_<id>'; it must end with '_' + the phone's metadata Id
                partner = str(t.get("PartnerClientId") or "").lower()
                hit = next((pe for mid, pe in mids.items() if mid and partner.endswith("_" + str(mid).lower())), None)
                if hit:
                    g.link("PR1", hit, e, "Stored link: this trust relationship's partner ID contains the phone's "
                                          "metadata ID")
        except SOURCE_ERRORS as ex:
            trust_rows, trust_err = [], f"could not read {TRUST}: {_why(ex)}"
    if trust_err:
        add("PC_trust", "Trust relationships (YppCryptoTrustRelationships)", trust_err, ["Error"], [{"Error": trust_err}])
    else:
        add("PC_trust", "Trust relationships (YppCryptoTrustRelationships)",
            "cryptographic trust with partner devices: last access, key expiry, trust type and the Microsoft account "
            "id (MsaCid). Certificates are not shown",
            ["PartnerClientID", "TrustType", "LastAccessUTC", "KeyExpiresUTC", "MsaCid", "SelfClientID", "DcgClientID"],
            trust_rows)

    if streamed_err:
        add("PC_streamed", "Apps streamed to this PC, per device (FullTrust phone_apps.db)", streamed_err, ["Error"],
            [{"Error": streamed_err}])
    else:
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
        if reg is None or "_e" not in m:
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
            events.append((m.get("_seen_ts"), "phone last seen (metadata)", g.label[m["_e"]], m["_e"]))
    for t in trust_rows:
        events.append((g.time.get(t["_e"]), "trust relationship last used", g.label[t["_e"]], t["_e"]))
    rows = [{"_e": e, "TimeUTC": _utc_ts(ts) if ts is not None else "", "Event": what, "Detail": detail}
            for ts, what, detail, e in sorted((x for x in events if x[0] is not None), key=lambda x: x[0])]
    add("PR4", "Pairing history (all linked-phone events in time order)",
        "folder creations, registrations, last-seen and trust times from all the sources above",
        ["TimeUTC", "Event", "Detail"], rows, entity=False)

    v = View("PC_copies", "Linked phones - copies and hashes", "provenance",
             f"copied from {pc.src} to {pc.work} (PlatformEncryptedKeyStorage.json deliberately not copied; "
             f"symlinks and junctions are not followed)",
             ["file", "source_sha256", "copy_matches"],
             pc.verify + [{"file": f, "source_sha256": "", "copy_matches": "skipped - symlink or junction, not followed"}
                          for f in pc.skipped])
    g.add_view(v)
    vids.append(v.vid)
    return vids
