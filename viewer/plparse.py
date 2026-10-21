#!/usr/bin/env python3
"""
plparse.py - Microsoft Phone Link (Your Phone) correlating parser.

Reads the nine per-device SQLite databases plus the FullTrust app-streaming
database, correlates them (the databases hold NO foreign keys between files),
and writes CSVs, a unified timeline and a single self-contained HTML report.

Usage:
    python plparse.py --input "<path>" --out "<dir>" [--case "name"] [--digits 10]

--input may be any of:
    ...\\Packages\\Microsoft.YourPhone_8wekyb3d8bbwe          (package root)
    ...\\LocalCache\\Indexed                                  (all devices)
    ...\\LocalCache\\Indexed\\{GUID}                          (one device)
    a directory holding the .db files themselves               (a KAPE/triage dump)

Design notes that matter forensically:
  * Source files are never opened in place. Every .db is copied WITH its -wal
    and -shm into a working directory, then hashed. If the WAL is left behind
    the data is lost silently, so the copy is explicit and reported.
  * Timestamps: most tables use Windows FILETIME; phoneapps.phone_apps uses
    Unix milliseconds. The converter detects by magnitude and RECORDS which
    rule it applied, per value, in the *_epoch column.
  * Correlation is done here, not by the data. Every joined name is an
    inference by this tool and is labelled as such in the output.

Stdlib only. Read-only with respect to the evidence.
"""

import argparse, csv, hashlib, html, json, os, pathlib, re, shutil, sqlite3, sys, tempfile
try:
    import plcarve
except ImportError:
    plcarve = None
from datetime import datetime, timedelta, timezone

TOOL = "plparse"
VERSION = "1.0"

DB_NAMES = ["phone.db", "calling.db", "contacts.db", "photos.db", "phoneapps.db",
            "notifications.db", "continuity.db", "deviceData.db", "settings.db"]
FULLTRUST_DB = "phone_apps.db"

CALL_TYPE = {1: "Incoming", 2: "Outgoing", 3: "Missed", 4: "Voicemail",
             5: "Rejected", 6: "Blocked", 7: "AnsweredElsewhere"}
MSG_TYPE = {1: "Inbox", 2: "Sent", 3: "Draft", 4: "Outbox", 5: "Failed", 6: "Queued"}

FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------- timestamps
def conv_time(v):
    """Return (iso_utc, rule). Magnitude decides; the rule is recorded, never assumed."""
    if v is None:
        return ("", "null")
    try:
        v = int(v)
    except (TypeError, ValueError):
        return ("", "unparsed")
    if v <= 0:
        return ("", "zero")
    try:
        if v > 10 ** 16:                       # ~1.3e17 on a current host
            return ((FILETIME_EPOCH + timedelta(microseconds=v // 10)).isoformat(), "FILETIME")
        if v > 10 ** 11:                       # ~1.8e12
            return ((UNIX_EPOCH + timedelta(milliseconds=v)).isoformat(), "UnixMillis")
        if v > 10 ** 8:                        # ~1.8e9
            return ((UNIX_EPOCH + timedelta(seconds=v)).isoformat(), "UnixSeconds")
    except (OverflowError, OSError, ValueError):
        return ("", "out-of-range")
    return ("", "unrecognised-magnitude")


# ---------------------------------------------------------------- addresses
SIP_RE = re.compile(r"^sip:([^@]+)@", re.I)


def norm_addr(a, digits=10):
    """
    Return (key, kind, display).

    kind: phone | short | alpha | rcs-agent | empty
    Matching is on the LAST n digits because the same number appears as
    +15550100123, 15550100123 and 5550100123 across these databases.
    """
    if a is None:
        return ("", "empty", "")
    a = str(a).strip()
    if not a:
        return ("", "empty", "")
    m = SIP_RE.match(a)
    if m:
        return (a.lower(), "rcs-agent", m.group(1).replace("_", " ").strip())
    d = re.sub(r"\D", "", a)
    if not d:
        return (a.upper(), "alpha", a)          # e.g. AB-EXAMPL-S (bank sender ID)
    if len(d) >= digits:
        return (d[-digits:], "phone", a)
    return (d, "short", a)                      # 5-6 digit shortcode


# ---------------------------------------------------------------- discovery
def find_device_dirs(root):
    """Return [(label, dir_with_dbs)]. Handles all four accepted --input shapes."""
    root = os.path.abspath(root)
    out = []

    def has_dbs(d):
        try:
            return any(f in os.listdir(d) for f in DB_NAMES)
        except OSError:
            return False

    if has_dbs(root):
        out.append((os.path.basename(root.rstrip("\\/")) or "root", root))
        return out

    cands = [root,
             os.path.join(root, "LocalCache", "Indexed"),
             os.path.join(root, "Indexed")]
    for base in cands:
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            sub = os.path.join(base, name)
            if not os.path.isdir(sub):
                continue
            for probe in (os.path.join(sub, "System", "Database"), sub):
                if has_dbs(probe):
                    out.append((name, probe))
                    break
        if out:
            return out
    return out


def find_fulltrust(root):
    root = os.path.abspath(root)
    for base in (root,
                 os.path.join(root, "LocalCache", "Indexed", "FullTrust"),
                 os.path.join(root, "Indexed", "FullTrust"),
                 os.path.join(os.path.dirname(root), "FullTrust")):
        p = os.path.join(base, FULLTRUST_DB)
        if os.path.isfile(p):
            return p
    return None


def find_metadata(root):
    root = os.path.abspath(root)
    for base in (root, os.path.join(root, "LocalCache"),
                 os.path.dirname(root), os.path.dirname(os.path.dirname(root))):
        p = os.path.join(base, "DeviceMetadataStorage.json")
        if os.path.isfile(p):
            return p
    return None


# ---------------------------------------------------------------- acquisition
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ro_uri(path):
    """SQLite read-only URI for a path. Escapes '#', '%', '?' and spaces so any folder name opens correctly."""
    return pathlib.Path(path).resolve().as_uri() + "?mode=ro"


SNAPSHOT_TRIES = 3
UNSTABLE = "NO - source changed during copy"


def _source_state(src):
    """Hashes of the source .db and -wal. The -shm is a shared-memory index and is left out."""
    w = src + "-wal"
    return (sha256(src), sha256(w) if os.path.isfile(w) else "")


def stage(src_dir, work, acq):
    """
    Copy every .db WITH its -wal/-shm. Report what was and was not found.

    A live database can change while its three files are copied one after another, which
    would give a .db and a -wal from different moments. The .db and -wal are hashed before
    and after each set; if either changed the whole set is copied again (3 tries). A set
    that never settles is kept, and its row says so: source_stable = UNSTABLE, and the
    recorded hashes are those of the copy actually taken.
    """
    os.makedirs(work, exist_ok=True)
    staged = {}
    for name in DB_NAMES:
        src = os.path.join(src_dir, name)
        if not os.path.isfile(src):
            acq.append({"file": name, "status": "absent"})
            continue
        dst = os.path.join(work, name)
        stable = False
        for _ in range(SNAPSHOT_TRIES):
            before = _source_state(src)
            shutil.copy2(src, dst)
            for suf in ("-wal", "-shm"):
                if os.path.isfile(src + suf):
                    shutil.copy2(src + suf, dst + suf)
                elif os.path.isfile(dst + suf):
                    os.remove(dst + suf)         # left over from an earlier try
            if _source_state(src) == before:
                stable = True
                break
        row = {"file": name, "status": "copied", "bytes": os.path.getsize(dst),
               "sha256": before[0] if stable else sha256(dst),
               "wal": "", "shm": "", "wal_sha256": "", "shm_sha256": "",
               "source_stable": "yes" if stable else UNSTABLE}
        for suf in ("-wal", "-shm"):
            if os.path.isfile(dst + suf):
                k = suf[1:]
                row[k] = os.path.getsize(dst + suf)
                # The WAL can hold most of the evidence, so it needs a hash too.
                if suf == "-wal":
                    row[k + "_sha256"] = before[1] if stable else sha256(dst + suf)
                else:
                    row[k + "_sha256"] = sha256(src + suf) if stable else sha256(dst + suf)
        acq.append(row)
        staged[name] = dst
    return staged


def deleted_evidence(path, dbname):
    """
    AUTOINCREMENT tables keep a high-water mark in sqlite_sequence. A mark above the
    live row count means rowids were issued that are no longer occupied.

    READ THIS BEFORE QUOTING IT. It is NOT a count of user deletions:
      * Phone Link re-syncs wholesale - it deletes and reinserts rows routinely, so
        churn is the ordinary explanation, not a person deleting something.
      * Some tables carry app-assigned rowids (huge, non-sequential values). The
        subtraction is meaningless there and is reported as such, not as a number.
      * content_sequence and _change_notifications are plumbing, rewritten constantly.
        They are excluded entirely.
    What it IS good for: telling you a table once held more rows than it does now,
    so its free pages are worth carving.
    """
    SKIP = {"content_sequence", "_change_notifications"}
    APP_ASSIGNED = 10 ** 12          # beyond any plausible insert counter here
    out = []
    try:
        c = sqlite3.connect(ro_uri(path), uri=True)
        try:
            seqs = c.execute("SELECT name, seq FROM sqlite_sequence").fetchall()
        except sqlite3.Error:
            c.close()
            return out
        for name, seq in seqs:
            if name in SKIP or name.startswith("_"):
                continue
            try:
                live = c.execute('SELECT COUNT(*) FROM "%s"' % name).fetchone()[0]
            except sqlite3.Error:
                continue
            seq = seq or 0
            if seq >= APP_ASSIGNED:
                out.append({"db": dbname, "table": name, "live_rows": live,
                            "highwater_seq": seq, "rowids_unused": "",
                            "classification": "app-assigned-rowids",
                            "note": "Counter is not a sequential insert count - no deletion "
                                    "inference can be drawn from it."})
                continue
            gap = seq - live
            if gap > 0:
                out.append({"db": dbname, "table": name, "live_rows": live,
                            "highwater_seq": seq, "rowids_unused": gap,
                            "classification": "churn-or-deletion",
                            "note": "%s rowid(s) issued and now vacant. Sync churn is the ordinary "
                                    "cause; user deletion is only one possibility. Treat as a reason "
                                    "to carve free pages, not as proof of deletion." % gap})
        c.close()
    except sqlite3.Error:
        pass
    return out


def db_health(path):
    """PRAGMAs that decide whether the evidence is complete and carvable."""
    out = {}
    try:
        c = sqlite3.connect(ro_uri(path), uri=True)
        for p in ("journal_mode", "page_count", "freelist_count", "page_size", "secure_delete"):
            try:
                out[p] = c.execute("PRAGMA " + p).fetchone()[0]
            except sqlite3.Error:
                out[p] = "?"
        c.close()
    except sqlite3.Error as e:
        out["error"] = str(e)
    return out


# Every query failure is recorded here. An empty result and a failed query look
# identical to the caller, so a forensic tool must not let them look identical in
# the report: "0 messages" must never silently mean "the query blew up".
QUERY_ERRORS = []


def q(path, sql, params=()):
    """Query helper: missing table or file yields [] AND records why."""
    if not path:
        QUERY_ERRORS.append({"file": "(none)", "sql": sql.strip()[:120], "error": "no path given"})
        return []
    if not os.path.isfile(path):
        QUERY_ERRORS.append({"file": os.path.basename(path), "sql": sql.strip()[:120],
                             "error": "file not present"})
        return []
    try:
        c = sqlite3.connect(ro_uri(path), uri=True)
        c.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in c.execute(sql, params)]
        finally:
            c.close()
        return rows
    except sqlite3.Error as e:
        QUERY_ERRORS.append({"file": os.path.basename(path), "sql": sql.strip()[:120],
                             "error": "%s: %s" % (type(e).__name__, e)})
        return []


# ---------------------------------------------------------------- parsing
def build_contact_index(db, digits):
    """key -> {name, contact_id, label}. The basis of every name in the output."""
    idx = {}
    for r in q(db, """SELECT p.phone_number, p.display_phone_number, p.label,
                             c.contact_id, c.display_name, c.company
                      FROM phonenumber p LEFT JOIN contact c ON c.contact_id = p.contact_id"""):
        k, kind, _ = norm_addr(r["phone_number"], digits)
        if not k:
            continue
        idx.setdefault(k, {"name": r["display_name"] or "",
                           "contact_id": r["contact_id"],
                           "company": r["company"] or "",
                           "label": r["label"] or ""})
    return idx


GROUP_SEP = re.compile(r"[,;]|\s{2,}")


def resolve(addr, idx, digits):
    """Return (display, name, kind, matched). 'matched' is what makes it an inference."""
    # A multi-recipient thread must NOT be collapsed to one contact: norm_addr would
    # strip separators and key on a splice of the last recipient's digits, which can
    # match a real person and put their name on someone else's group thread.
    if addr and GROUP_SEP.search(str(addr)):
        parts = [x for x in GROUP_SEP.split(str(addr)) if x.strip()]
        names = []
        for prt in parts:
            pk, pkind, pdisp = norm_addr(prt, digits)
            hit = idx.get(pk)
            names.append(hit["name"] if hit and hit["name"] else pdisp)
        return (" | ".join(p.strip() for p in parts), " | ".join(names), "group",
                "contacts.db (per-member)")
    k, kind, disp = norm_addr(addr, digits)
    if kind == "rcs-agent":
        return (disp, "", kind, "rcs-business-agent")
    hit = idx.get(k)
    if hit and hit["name"]:
        return (disp, hit["name"], kind, "contacts.db")
    return (disp, "", kind, "")


def parse_all(dbs, ft_db, idx, digits, device_label=None):
    d = {}

    # --- calls -------------------------------------------------------------
    calls = []
    for r in q(dbs.get("calling.db"), "SELECT * FROM call_history ORDER BY start_time DESC"):
        iso, rule = conv_time(r["start_time"])
        sync, _ = conv_time(r.get("last_updated_time"))
        disp, name, kind, src = resolve(r["phone_number"], idx, digits)
        calls.append({"start_utc": iso, "start_epoch": rule,
                      "direction": CALL_TYPE.get(r["call_type"], "type=%s" % r["call_type"]),
                      "number": disp, "addr_kind": kind,
                      "contact_name": name, "name_source": src,
                      "duration_sec": r["duration"],
                      "is_read": r["is_read"], "sim_account": r.get("phone_account_id") or "",
                      "synced_utc": sync, "call_id": r["call_id"]})
    d["calls"] = calls

    # --- SIM ---------------------------------------------------------------
    sims = {}
    for r in q(dbs.get("phone.db"), "SELECT * FROM subscription"):
        sims[r["subscription_id"]] = r
    d["sims"] = [{"subscription_id": k, "own_number": v.get("number") or "",
                  "carrier": v.get("name") or "", "country": v.get("country_iso") or "",
                  "sim_slot": v.get("sim_slot_index"), "roaming": v.get("is_roaming"),
                  "default_sms": v.get("is_default_sms_subscription"),
                  "default_voice": v.get("is_default_voice_subscription"),
                  "rcs_supported": v.get("is_rcs_supported")} for k, v in sims.items()]

    # --- threads -----------------------------------------------------------
    parts = {}
    for r in q(dbs.get("phone.db"), "SELECT thread_id, normalized_address, send_address FROM participant"):
        parts.setdefault(r["thread_id"], []).append(r["normalized_address"] or r["send_address"])

    bodies = {}
    for r in q(dbs.get("phone.db"), "SELECT thread_id, COUNT(*) n FROM message GROUP BY thread_id"):
        bodies[r["thread_id"]] = r["n"]

    threads = []
    for r in q(dbs.get("phone.db"), "SELECT * FROM conversation ORDER BY timestamp DESC"):
        iso, rule = conv_time(r["timestamp"])
        disp, name, kind, src = resolve(r["recipient_list"], idx, digits)
        threads.append({"thread_id": r["thread_id"], "counterparty": disp,
                        "addr_kind": kind, "contact_name": name, "name_source": src,
                        "last_activity_utc": iso, "epoch": rule,
                        "msg_count_reported": r["msg_count"],
                        "bodies_retained": bodies.get(r["thread_id"], 0),
                        "content_aged_out": "YES" if bodies.get(r["thread_id"], 0) == 0 else "",
                        "unread_pc": r["unread_count"], "unread_phone": r.get("phone_unread_count"),
                        "has_rcs": r["has_rcs"],
                        "participants": " | ".join(parts.get(r["thread_id"], []))})
    d["threads"] = threads
    tmap = {t["thread_id"]: t for t in threads}

    # --- messages ----------------------------------------------------------
    msgs = []
    for r in q(dbs.get("phone.db"), "SELECT * FROM message ORDER BY timestamp DESC"):
        iso, rule = conv_time(r["timestamp"])
        t = tmap.get(r["thread_id"], {})
        # to_address is empty on every observed row: direction comes from type.
        direction = MSG_TYPE.get(r["type"], "type=%s" % r["type"])
        counter = r["from_address"] if direction == "Inbox" and r["from_address"] else t.get("counterparty", "")
        disp, name, kind, src = resolve(counter, idx, digits)
        sim = sims.get(r["subscription_id"], {})
        msgs.append({"timestamp_utc": iso, "epoch": rule, "direction": direction,
                     "counterparty": disp, "addr_kind": kind,
                     "contact_name": name or t.get("contact_name", ""),
                     "name_source": src or t.get("name_source", ""),
                     "thread_id": r["thread_id"], "subject": r.get("subject") or "",
                     "body": (r["body"] or "").replace("\r", " ").replace("\n", " "),
                     "body_len": len(r["body"] or ""),
                     "own_number": sim.get("number", ""), "carrier": sim.get("name", ""),
                     "pc_status": r.get("pc_status"), "message_id": r["message_id"]})
    d["messages"] = msgs

    # --- RCS ---------------------------------------------------------------
    rcs = []
    for r in q(dbs.get("phone.db"), "SELECT * FROM rcs_chat ORDER BY timestamp DESC"):
        iso, rule = conv_time(r["timestamp"])
        disp, name, kind, src = resolve(r["from_address"], idx, digits)
        rcs.append({"timestamp_utc": iso, "epoch": rule, "thread_id": r["thread_id"],
                    "from": disp, "contact_name": name, "addr_kind": kind,
                    "content_type": r["content_type"], "body": (r["body"] or "").replace("\n", " "),
                    "session_id": r["session_id"], "message_id": r["message_id"]})
    d["rcs"] = rcs

    # --- MMS parts (attachments) -------------------------------------------
    att = []
    for r in q(dbs.get("phone.db"), "SELECT * FROM mms_part"):
        att.append({"message_id": r["message_id"], "name": r.get("name") or "",
                    "content_type": r.get("content_type") or "", "size": r.get("size"),
                    "blob_bytes": len(r["blob"]) if r.get("blob") else 0,
                    "text": (r.get("text") or "")[:200]})
    for r in q(dbs.get("phone.db"), "SELECT * FROM rcs_filetransfer"):
        iso, _ = conv_time(r["timestamp"])
        att.append({"message_id": r["message_id"], "name": r.get("name") or "",
                    "content_type": r.get("content_type") or "", "size": r.get("size"),
                    "blob_bytes": len(r["blob"]) if r.get("blob") else 0, "text": "RCS " + iso})
    d["attachments"] = att

    # --- outbound composed ON THE PC ---------------------------------------
    d["sent_from_pc"] = q(dbs.get("phone.db"), "SELECT * FROM sending_message")

    # --- contacts ----------------------------------------------------------
    emails, addrs = {}, {}
    for r in q(dbs.get("contacts.db"), "SELECT contact_id, address FROM emailaddress"):
        emails.setdefault(r["contact_id"], []).append(r["address"])
    for r in q(dbs.get("contacts.db"), "SELECT * FROM postaladdress"):
        addrs.setdefault(r["contact_id"], []).append(r.get("display_address") or
                                                     " ".join(filter(None, [r.get("street"), r.get("city"),
                                                                            r.get("region"), r.get("postal_code"),
                                                                            r.get("country_code")])))
    nums = {}
    for r in q(dbs.get("contacts.db"), "SELECT contact_id, phone_number, label FROM phonenumber"):
        nums.setdefault(r["contact_id"], []).append(r["phone_number"])
    cons = []
    for r in q(dbs.get("contacts.db"), "SELECT * FROM contact ORDER BY display_name"):
        iso, rule = conv_time(r.get("last_updated_time"))
        cons.append({"contact_id": r["contact_id"], "display_name": r.get("display_name") or "",
                     "given": r.get("given_name") or "", "family": r.get("family_name") or "",
                     "company": r.get("company") or "", "job_title": r.get("job_title") or "",
                     "notes": (r.get("notes") or "").replace("\n", " "),
                     "numbers": " | ".join(nums.get(r["contact_id"], [])),
                     "emails": " | ".join(emails.get(r["contact_id"], [])),
                     "addresses": " | ".join(addrs.get(r["contact_id"], [])),
                     "has_photo": "yes" if r.get("thumbnail") else "",
                     "synced_utc": iso, "epoch": rule})
    d["contacts"] = cons
    d["deleted_contact_refs"] = q(dbs.get("contacts.db"), "SELECT * FROM contact_index")

    # --- photos ------------------------------------------------------------
    pics = []
    for r in q(dbs.get("photos.db"), "SELECT * FROM media ORDER BY taken_time DESC"):
        iso, rule = conv_time(r["taken_time"])
        pics.append({"taken_utc": iso, "epoch": rule, "name": r["name"],
                     "mime": r["mime_type"], "width": r["width"], "height": r["height"],
                     "size_bytes": r["size"], "android_uri": r["uri"],
                     "thumbnail_cached": "yes" if r.get("thumbnail") else "",
                     "fullres_cached": "yes" if r.get("media") else "", "id": r["id"]})
    d["photos"] = pics

    # --- apps: installed x streamed ----------------------------------------
    # hosted_app_ids is SHARED across devices and carries device_id. Scope it, or
    # a device's streamed-app list is silently credited to every other device.
    hosted = {}
    ft_rows = q(ft_db, "SELECT * FROM hosted_app_ids")
    ft_devices = sorted({(r.get("device_id") or "") for r in ft_rows})
    for r in ft_rows:
        did = r.get("device_id") or ""
        if device_label and did and did != device_label:
            continue                      # belongs to a different linked device
        hosted[r["package_id"]] = r
    apps = []
    seen = set()
    for r in q(dbs.get("phoneapps.db"), "SELECT * FROM phone_apps ORDER BY app_name"):
        pkg = r["package_name"]
        seen.add(pkg)
        h = hosted.get(pkg)
        iso, rule = conv_time(r.get("last_updated_time"))
        apps.append({"app_name": r["app_name"], "package": pkg, "version": r["version"],
                     "installed_on_phone": "yes",
                     "streamed_to_pc": "yes" if h else "",
                     "last_window_position": (h or {}).get("last_window_position", ""),
                     "last_volume": (h or {}).get("last_volume_level", ""),
                     "last_muted": (h or {}).get("last_is_muted_state", ""),
                     "synced_utc": iso, "epoch": rule})
    for pkg, h in sorted(hosted.items()):
        if pkg in seen:
            continue
        apps.append({"app_name": "", "package": pkg, "version": "",
                     "installed_on_phone": "NO - streamed but not in current inventory",
                     "streamed_to_pc": "yes",
                     "last_window_position": h.get("last_window_position", ""),
                     "last_volume": h.get("last_volume_level", ""),
                     "last_muted": h.get("last_is_muted_state", ""),
                     "synced_utc": "", "epoch": ""})
    d["apps"] = apps
    d["_ft_devices"] = ft_devices
    # If scoping removed every row, say so - otherwise it reads as "no apps streamed"
    if device_label and ft_rows and not hosted:
        d["_ft_scope_warning"] = ("hosted_app_ids holds %d row(s) for device_id(s) %s but none "
                                  "for this device (%s). Streamed-app correlation is empty - "
                                  "check the folder name matches the device_id exactly."
                                  % (len(ft_rows), ", ".join(x or "(blank)" for x in ft_devices),
                                     device_label))
    appname = {a["package"]: a["app_name"] for a in apps if a["app_name"]}

    # --- notifications ------------------------------------------------------
    nots = []
    for r in q(dbs.get("notifications.db"), "SELECT * FROM notifications ORDER BY post_time DESC"):
        iso, rule = conv_time(r["post_time"])
        title = body = ""
        try:
            j = json.loads(r["json"])
            if isinstance(j, dict):
                for k in ("title", "Title", "contentTitle"):
                    if j.get(k):
                        title = str(j[k]); break
                for k in ("text", "Text", "body", "contentText", "message"):
                    if j.get(k):
                        body = str(j[k]); break
        except (ValueError, TypeError):
            pass
        nots.append({"post_utc": iso, "epoch": rule, "package": r["package_name"],
                     "app_name": appname.get(r["package_name"], ""),
                     "title": title, "text": body, "state": r["state"],
                     "json_len": len(r["json"] or ""), "notification_id": r["notification_id"]})
    d["notifications"] = nots

    # --- browser handoff ----------------------------------------------------
    hist = []
    for r in q(dbs.get("continuity.db"), "SELECT * FROM browser_history"):
        hist.append({"title": r.get("browser_history_title") or "", "uri": r.get("web_uri") or "",
                     "package": r.get("package_name") or "", "source": r.get("source") or ""})
    d["browser"] = hist

    return d


# ---------------------------------------------------------------- timeline
def build_timeline(d):
    tl = []

    def add(ts, epoch, kind, who, detail, src):
        if ts:
            tl.append({"timestamp_utc": ts, "epoch_rule": epoch, "event": kind,
                       "party": who, "detail": detail, "source": src})

    for r in d["calls"]:
        add(r["start_utc"], r["start_epoch"], "Call " + r["direction"],
            r["contact_name"] or r["number"], "%ss" % r["duration_sec"], "calling.db/call_history")
    for r in d["messages"]:
        add(r["timestamp_utc"], r["epoch"], "SMS " + r["direction"],
            r["contact_name"] or r["counterparty"],
            (r["body"][:80] + ("..." if len(r["body"]) > 80 else "")), "phone.db/message")
    for r in d["rcs"]:
        add(r["timestamp_utc"], r["epoch"], "RCS", r["contact_name"] or r["from"],
            r["body"][:80], "phone.db/rcs_chat")
    for r in d["photos"]:
        add(r["taken_utc"], r["epoch"], "Photo taken", "",
            "%s  %sx%s  %s bytes" % (r["name"], r["width"], r["height"], r["size_bytes"]),
            "photos.db/media")
    for r in d["notifications"]:
        add(r["post_utc"], r["epoch"], "Notification",
            r["app_name"] or r["package"], (r["title"] + " " + r["text"])[:80],
            "notifications.db/notifications")
    tl.sort(key=lambda x: x["timestamp_utc"], reverse=True)
    return tl


# ---------------------------------------------------------------- output
_CTRL = re.compile("[" + "".join(chr(c) for c in list(range(0, 9)) + [11, 12] + list(range(14, 32))) + "]")


def clean(v):
    """
    Carved data is partially overwritten by definition, so a recovered TEXT field
    can contain NUL and other control bytes that the csv writer rejects. Replace
    them visibly rather than dropping the row - the marker is itself a signal that
    the value is damaged.
    """
    if isinstance(v, bytes):
        return "<blob %d bytes>" % len(v)
    if isinstance(v, str) and _CTRL.search(v):
        return _CTRL.sub(chr(0xFFFD), v)
    return v


def write_csv(path, rows, cols=None):
    if not rows:
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh).writerow(cols or ["(no rows)"])
        return 0
    if not cols:
        # Union of every row's keys, in first-seen order. Taking rows[0] alone
        # silently drops every column of every other shape in a mixed list -
        # which is exactly what carved_records.csv is.
        cols, seen_k = [], set()
        for r in rows:
            for k in r:
                if k not in seen_k:
                    seen_k.add(k)
                    cols.append(k)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: clean(v) for k, v in r.items()})
    return len(rows)


def esc(x):
    return html.escape("" if x is None else str(x))


def html_report(out, case, dev, meta, acq, health, d, tl, counts, digits, warnings):
    def table(rows, cols, limit=40):
        if not rows:
            return "<p class=muted>No rows.</p>"
        h = "<table><tr>" + "".join("<th>%s</th>" % esc(c) for c in cols) + "</tr>"
        for r in rows[:limit]:
            h += "<tr>" + "".join("<td>%s</td>" % esc(r.get(c, "")) for c in cols) + "</tr>"
        h += "</table>"
        if len(rows) > limit:
            h += "<p class=muted>Showing %d of %d — full set in the CSV.</p>" % (limit, len(rows))
        return h

    def wal_cell(a):
        # Built as markup on purpose: the value is escaped, the warning span is not.
        # Escaping the whole thing printed the literal tag and killed the red cue.
        if a.get("status") != "copied":
            return ""
        return esc(a["wal"]) if a.get("wal") else "<span class=warn>none</span>"

    acq_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class=mono>%s</td></tr>" % (
            esc(a.get("file")), esc(a.get("status")), esc(a.get("bytes", "")),
            wal_cell(a), esc(a.get("shm", "")), esc((a.get("sha256") or "")[:32]))
        for a in acq)

    hl = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
        esc(k), esc(v.get("journal_mode")), esc(v.get("page_count")),
        esc(v.get("freelist_count")), esc(v.get("secure_delete"))) for k, v in health.items())

    warn_html = "".join("<li>%s</li>" % esc(w) for w in warnings) or "<li>None raised.</li>"

    cnt = "".join("<div class=stat><b>%s</b><span>%s</span></div>" % (esc(v), esc(k))
                  for k, v in counts.items())

    doc = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(case)s — Phone Link report</title><style>
:root{--bg:#0e1a2b;--pan:#16293f;--ink:#13233a;--pap:#fff;--teal:#2ec4b6;--amb:#f4a215;--red:#d9534f;--mut:#667c93}
*{box-sizing:border-box}body{margin:0;background:#f4f7fa;color:var(--ink);font:15px/1.55 Calibri,Segoe UI,sans-serif}
header{background:var(--bg);color:#fff;padding:28px 32px}
header h1{margin:0 0 6px;font:600 26px Cambria,Georgia,serif}
header p{margin:0;color:#9fb3c8;font-size:13px}
main{padding:24px 32px;max-width:1400px}
h2{font:600 19px Cambria,Georgia,serif;margin:30px 0 10px;color:var(--bg)}
table{border-collapse:collapse;width:100%%;font-size:12.5px;background:#fff;margin:8px 0 4px}
th{background:var(--pan);color:#fff;text-align:left;padding:7px 9px;font-weight:600}
td{border-bottom:1px solid #e3e9ef;padding:6px 9px;vertical-align:top}
tr:nth-child(even) td{background:#f7fafc}
.mono{font-family:Consolas,monospace;font-size:11.5px}
.muted{color:var(--mut);font-size:12.5px}
.warn{color:var(--red);font-weight:700}
.stats{display:flex;flex-wrap:wrap;gap:12px;margin:14px 0}
.stat{background:#fff;border-radius:8px;padding:12px 18px;min-width:120px;box-shadow:0 1px 3px #0002}
.stat b{display:block;font:700 24px Cambria,serif;color:var(--bg)}
.stat span{font-size:11.5px;color:var(--mut)}
.note{background:#fff6e4;border-radius:8px;padding:14px 18px;margin:14px 0;font-size:13px}
.note b{color:#8a5a00}
footer{padding:18px 32px;color:var(--mut);font-size:12px}
@media(max-width:700px){main,header,footer{padding:16px}table{font-size:11px}}
</style></head><body>
<header><h1>%(case)s</h1>
<p>Phone Link correlating parse &middot; device <span class=mono>%(dev)s</span> &middot; generated %(now)s UTC by %(tool)s %(ver)s</p></header><main>

<div class=stats>%(cnt)s</div>

<div class=note><b>Every name in this report is an inference by this tool.</b>
These databases contain no foreign keys between files. Numbers were matched to contacts on the
last %(digits)d digits, because the same number appears as +91…, 91… and bare in different tables.
Each correlated row carries a <span class=mono>name_source</span> column recording where the name came from.
Rows with an empty <span class=mono>name_source</span> were not matched to any contact.</div>

<h2>Acquisition integrity</h2>
<table><tr><th>File</th><th>Status</th><th>Bytes</th><th>WAL bytes</th><th>SHM bytes</th><th>SHA-256 (first 32)</th></tr>%(acq)s</table>
<p class=muted>A database copied without its WAL can open cleanly and be empty. "none" above means no WAL existed
at copy time — expected only if the app had exited cleanly.</p>

<h2>Database health</h2>
<table><tr><th>File</th><th>journal_mode</th><th>pages</th><th>freelist</th><th>secure_delete</th></tr>%(hl)s</table>
<p class=muted>freelist &gt; 0 with secure_delete = 0 means deleted rows are recoverable by carving the free pages.</p>

<h2>Warnings</h2><ul>%(warn)s</ul>

<h2>Device</h2><pre class=mono>%(meta)s</pre>

<h2>SIM / subscriber</h2>%(sims)s
<h2>Timeline (most recent)</h2>%(tl)s
<h2>Calls</h2>%(calls)s
<h2>Threads whose content has aged out</h2>%(aged)s
<h2>Apps streamed to this PC</h2>%(apps)s
<h2>Photo index</h2>%(pics)s
</main>
<footer>%(tool)s %(ver)s — correlation is performed by this tool, not by the data.
Validate every join against the databases before relying on it in a report.</footer></body></html>""" % {
        "case": esc(case), "dev": esc(dev), "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "tool": TOOL, "ver": VERSION, "cnt": cnt, "acq": acq_rows, "hl": hl, "warn": warn_html,
        "digits": digits, "meta": esc(json.dumps(meta, indent=1)[:2500]),
        "sims": table(d["sims"], ["subscription_id", "own_number", "carrier", "country", "sim_slot",
                                  "roaming", "default_sms", "default_voice", "rcs_supported"]),
        "tl": table(tl, ["timestamp_utc", "event", "party", "detail", "source"], 60),
        "calls": table(d["calls"], ["start_utc", "direction", "number", "contact_name",
                                    "name_source", "duration_sec", "sim_account"], 40),
        "aged": table([t for t in d["threads"] if t["content_aged_out"]],
                      ["counterparty", "contact_name", "name_source", "last_activity_utc",
                       "msg_count_reported", "participants"], 40),
        "apps": table([a for a in d["apps"] if a["streamed_to_pc"]],
                      ["app_name", "package", "installed_on_phone", "last_window_position"], 40),
        "pics": table(d["photos"], ["taken_utc", "name", "width", "height", "size_bytes",
                                    "thumbnail_cached", "android_uri"], 30),
    }
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(doc)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Phone Link correlating parser")
    ap.add_argument("--input", required=True, help="package root, Indexed dir, device dir, or a folder of .db files")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--case", default="Phone Link", help="case or exhibit label")
    ap.add_argument("--digits", type=int, default=10, help="trailing digits used to match numbers (default 10)")
    ap.add_argument("--keep-work", action="store_true", help="keep the staged copies")
    ap.add_argument("--carve", action="store_true",
                    help="also carve deleted records from free pages, freeblocks, page slack and WAL frames")
    a = ap.parse_args()

    devs = find_device_dirs(a.input)
    if not devs:
        print("No device database folders found under: %s" % a.input, file=sys.stderr)
        print("Expected one of phone.db / calling.db / contacts.db somewhere beneath it.", file=sys.stderr)
        return 2

    ft_src = find_fulltrust(a.input)
    meta_src = find_metadata(a.input)
    meta = {}
    if meta_src:
        try:
            with open(meta_src, "r", encoding="utf-8-sig") as fh:
                raw = json.load(fh)
            for hw, entries in (raw.get("DeviceMetadatas") or {}).items():
                for e in entries or []:
                    m = e.get("Metadata", {}) or {}
                    inner = m.get("Metadata", {}) or {}
                    meta.setdefault(m.get("Id", hw), {
                        "hardware_id": hw, "linked": e.get("IsLinked"),
                        # Microsoft ships this key misspelled; read both.
                        "registered": m.get("RegitrationTime") or m.get("RegistrationTime"),
                        "last_seen": m.get("LastSeenTime"),
                        "os": "%s %s" % (inner.get("OsName", ""), inner.get("OsVersion", "")),
                        "make": inner.get("Manufacture"), "model": inner.get("ModelName"),
                        "client": inner.get("ClientType"), "client_ver": inner.get("ClientVersion"),
                        "capabilities": inner.get("Capabilities"),
                    })
        except (ValueError, OSError) as e:
            meta = {"error": "could not read DeviceMetadataStorage.json: %s" % e}

    os.makedirs(a.out, exist_ok=True)
    print("%s %s" % (TOOL, VERSION))
    print("Found %d device folder(s). FullTrust db: %s. Metadata: %s\n"
          % (len(devs), "yes" if ft_src else "no", "yes" if meta_src else "no"))

    summary = []
    for label, src in devs:
        outdir = os.path.join(a.out, re.sub(r"[^A-Za-z0-9._-]", "_", label))
        os.makedirs(outdir, exist_ok=True)
        work = tempfile.mkdtemp(prefix="plparse_")
        acq, warnings = [], []
        try:
            dbs = stage(src, work, acq)
            if not dbs:
                print("[%s] no databases — skipped" % label)
                continue
            ft_work = None
            if ft_src:
                ft_work = os.path.join(work, "ft_" + FULLTRUST_DB)
                shutil.copy2(ft_src, ft_work)
                for suf in ("-wal", "-shm"):
                    if os.path.isfile(ft_src + suf):
                        shutil.copy2(ft_src + suf, ft_work + suf)

            health = {n: db_health(p) for n, p in dbs.items()}
            for n, h in health.items():
                if h.get("journal_mode") == "wal" and not any(
                        x["file"] == n and x.get("wal") for x in acq):
                    warnings.append("%s is in WAL mode but no -wal file was present at copy time. "
                                    "If the source was live, data may be missing." % n)
                if h.get("freelist_count", 0) and h.get("secure_delete") == 0:
                    warnings.append("%s has %s free pages with secure_delete=0 - deleted rows are "
                                    "likely recoverable by carving." % (n, h["freelist_count"]))

            del QUERY_ERRORS[:]                      # per-device
            idx = build_contact_index(dbs.get("contacts.db"), a.digits)
            d = parse_all(dbs, ft_work, idx, a.digits, device_label=label)

            if d.get("_ft_scope_warning"):
                warnings.append(d["_ft_scope_warning"])
            if QUERY_ERRORS:
                warnings.append("%d query/table read(s) FAILED - an empty section below may mean "
                                "the read failed, not that the table was empty. See query_errors.csv."
                                % len(QUERY_ERRORS))
            d["query_errors"] = list(QUERY_ERRORS)

            carved, carve_stats = [], []
            if a.carve:
                if plcarve is None:
                    warnings.append("--carve requested but plcarve.py was not importable; skipped.")
                else:
                    for n, pth in dbs.items():
                        try:
                            rs, st = plcarve.carve_db(pth)
                        except (OSError, ValueError, sqlite3.Error) as e:
                            warnings.append("carve failed on %s: %s" % (n, e))
                            continue
                        carve_stats.append(st)
                        carved.extend(rs)
                    if ft_work:
                        try:
                            rs, st = plcarve.carve_db(ft_work)
                            carve_stats.append(st); carved.extend(rs)
                        except OSError:
                            pass
                    dl = sum(1 for r in carved if r.get("status") != "also_live")
                    if carved:
                        warnings.append("Carved %d candidate record(s) from dead space, %d not present "
                                        "in the live tables. Candidates are shape matches, not proof - "
                                        "check type_fit, region and offset before relying on any row."
                                        % (len(carved), dl))
            d["carved"] = carved
            d["carve_stats"] = carve_stats

            gone = []
            for n, pth in dbs.items():
                gone.extend(deleted_evidence(pth, n))
            if ft_work:
                gone.extend(deleted_evidence(ft_work, "FullTrust/phone_apps.db"))
            d["deleted_evidence"] = gone
            for g in gone:
                if g["classification"] == "churn-or-deletion":
                    warnings.append("%s.%s: %s live row(s), %s rowid(s) vacant - carve the free pages. "
                                    "Sync churn is the ordinary cause; NOT proof of user deletion."
                                    % (g["db"], g["table"], g["live_rows"], g["rowids_unused"]))
            tl = build_timeline(d)

            named = sum(1 for r in d["calls"] if r["contact_name"])
            counts = {
                "calls": len(d["calls"]), "calls named": named,
                "messages": len(d["messages"]), "threads": len(d["threads"]),
                "threads aged out": sum(1 for t in d["threads"] if t["content_aged_out"]),
                "contacts": len(d["contacts"]), "photos indexed": len(d["photos"]),
                "apps installed": sum(1 for x in d["apps"] if x["installed_on_phone"] == "yes"),
                "apps streamed": sum(1 for x in d["apps"] if x["streamed_to_pc"]),
                "notifications": len(d["notifications"]),
                "attachments": len(d["attachments"]),
                "timeline events": len(tl),
                "failed reads": len(d["query_errors"]),
                "carved candidates": len(d["carved"]),
                "carved not-live": sum(1 for r in d["carved"] if r.get("status") != "also_live"),
                "vacant-rowid signals": sum(1 for g in d["deleted_evidence"]
                                            if g["classification"] == "churn-or-deletion"),
            }

            files = {
                "calls.csv": d["calls"], "messages.csv": d["messages"], "threads.csv": d["threads"],
                "contacts.csv": d["contacts"], "photos.csv": d["photos"], "apps.csv": d["apps"],
                "notifications.csv": d["notifications"], "rcs_chat.csv": d["rcs"],
                "attachments.csv": d["attachments"], "sims.csv": d["sims"],
                "browser_handoff.csv": d["browser"], "timeline.csv": tl,
                "sent_from_pc.csv": d["sent_from_pc"], "deleted_contact_refs.csv": d["deleted_contact_refs"],
                "deleted_evidence.csv": d["deleted_evidence"],
                "carved_records.csv": d["carved"],
                "carve_stats.csv": d["carve_stats"],
                "query_errors.csv": d["query_errors"],
            }
            for fn, rows in files.items():
                write_csv(os.path.join(outdir, fn), rows)

            if d["carved"]:
                cdir = os.path.join(outdir, "carved")
                os.makedirs(cdir, exist_ok=True)
                by = {}
                for r in d["carved"]:
                    by.setdefault(r["table"], []).append(r)
                for t, rows in by.items():
                    cols, seen_c = [], set()
                    for r in rows:
                        for k in r:
                            if k not in seen_c:
                                seen_c.add(k); cols.append(k)
                    write_csv(os.path.join(cdir, "carved_%s.csv" % t), rows, cols)

            with open(os.path.join(outdir, "acquisition.json"), "w", encoding="utf-8") as fh:
                json.dump({"tool": TOOL, "version": VERSION,
                           "generated_utc": datetime.now(timezone.utc).isoformat(),
                           "source": src, "device_label": label,
                           "match_digits": a.digits, "files": acq, "health": health,
                           "warnings": warnings, "counts": counts,
                           "device_metadata": meta}, fh, indent=2, default=str)

            html_report(os.path.join(outdir, "report.html"), a.case, label, meta,
                        acq, health, d, tl, counts, a.digits, warnings)

            print("[%s]" % label)
            for k, v in counts.items():
                print("    %-20s %s" % (k, v))
            for w in warnings:
                print("    ! %s" % w)
            print("    -> %s" % outdir)
            summary.append((label, counts))
        finally:
            if a.keep_work:
                print("    staged copies kept at %s" % work)
            else:
                shutil.rmtree(work, ignore_errors=True)

    if not summary:
        print("\nNothing parsed.")
        return 1
    print("\nDone. Open report.html in each device folder.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
