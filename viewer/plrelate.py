"""
plrelate.py - relations between the Phone Link tables, and the SQLECmd map output.

Phone Link only. Standard library only. Works on COPIES: the input folder is
staged with plparse.stage() (every .db with its -wal/-shm), each copy is
re-hashed against its source, and the copies are opened read-only.

None of the nine databases has a foreign key to another, so every link here
says how it was made:

  schema    a key inside one database (for example phonenumber.contact_id)
  id-join   the same ID column in two tables of one database, not declared as a key
  examiner  a join made by this tool across databases (for example a phone number
            matched on its last 10 digits) - an inference, never stored on the device
  time      events that overlap or happen close together - a lead, not a fact
  integrity rows whose partner is missing, and feeds whose row count disagrees
            with the sync bookmark

Usage as a library:
    ev = Evidence(r"...\\System\\Database")      # stages and verifies copies
    graph = build(ev)                            # entities, edges, relation views
    graph.views["R13"].rows                      # one relation as a table
    graph.links(("call", 42))                    # every link of one row
"""
import bisect
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plparse as P  # noqa: E402  (norm_addr, conv_time, stage, find_device_dirs, sha256)
import plnotif  # noqa: E402  (notifications.db)

DIGITS = 10
DEFAULT_WINDOW_MIN = 5
HERE = os.path.dirname(os.path.abspath(__file__))
# maps used for the SQLECmd map output, first match wins:
#   repo layout:     viewer/../maps (the maps) + viewer/maps-upstream (the 4 upstream Phone Link maps)
#   project layout:  03-tools/../06-upstream-sqlecmd/maps + maps-upstream-yourphone
_REPO = [os.path.join(HERE, "..", "maps"), os.path.join(HERE, "maps-upstream")]
_PROJECT = [os.path.join(HERE, "..", "06-upstream-sqlecmd", "maps"),
            os.path.join(HERE, "..", "06-upstream-sqlecmd", "maps-upstream-yourphone")]
DEFAULT_MAP_DIRS = _REPO if os.path.isdir(_REPO[1]) else _PROJECT

# content_sequence.content_type -> (database, table) whose rows it counts
FEEDS = {
    "sms": ("phone.db", "message"), "mms": ("phone.db", "mms"), "conversation": ("phone.db", "conversation"),
    "rcs_chat": ("phone.db", "rcs_chat"), "rcs_conversation": ("phone.db", "rcs_conversation"),
    "rcs_filetransfer": ("phone.db", "rcs_filetransfer"), "subscription": ("phone.db", "subscription"),
    "call_logs": ("calling.db", "call_history"), "contact": ("contacts.db", "contact"),
    "contactdate": ("contacts.db", "contactdate"), "contacturl": ("contacts.db", "contacturl"),
    "emailaddress": ("contacts.db", "emailaddress"), "phonenumber": ("contacts.db", "phonenumber"),
    "postaladdress": ("contacts.db", "postaladdress"), "media_info": ("photos.db", "media"),
    "phoneApps": ("phoneapps.db", "phone_apps"), "recentApps": ("phoneapps.db", "recent_apps"),
    "browserHistory": ("continuity.db", "browser_history"),
    "continuityAppContext": ("continuity.db", "continue_app_context"), "wallpaper": ("deviceData.db", "wallpaper"),
}


# ---------------------------------------------------------------- time
def ts_seconds(v):
    """Unix seconds (float) from a raw Phone Link time, by the same magnitude rules as plparse.conv_time."""
    try:
        v = int(v)
        if v <= 0:
            return None
        if v > 10 ** 16:
            return v / 1e7 - 11644473600
        if v > 10 ** 11:
            return v / 1e3
        if v > 10 ** 8:
            return float(v)
    except (TypeError, ValueError, OverflowError):   # text, NULL, inf, or a number too big for a float
        pass
    return None


def whole(v):
    """int from a stored value, or None when it is NULL, text or anything else that is not a number."""
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        try:
            return int(float(v))
        except (TypeError, ValueError, OverflowError):
            return None


def iso(sec):
    if sec is None:
        return ""
    try:
        return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=sec)).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, ValueError):
        return ""


# ---------------------------------------------------------------- evidence (copies only)
class Evidence:
    """Stage one device folder to a temporary copy, verify it, and open it read-only."""

    def __init__(self, src_dir, work=None):
        self.src = os.path.abspath(src_dir)
        self.work = work or tempfile.mkdtemp(prefix="plrelate-")
        self.acq = []
        self.verify = []
        self.snapshot_note = ""
        self._con = {}
        self._cols = {}
        self.missing = {}       # (db, table) -> columns the viewer asked for that this copy does not have
        try:
            self.paths = P.stage(self.src, self.work, self.acq)
            for row in self.acq:
                if row.get("status") != "copied":
                    continue
                for suffix, key in (("", "sha256"), ("-wal", "wal_sha256"), ("-shm", "shm_sha256")):
                    if row.get(key):
                        copy = os.path.join(self.work, row["file"] + suffix)
                        ok = P.sha256(copy) == row[key]
                        self.verify.append({"file": row["file"] + suffix, "source_sha256": row[key],
                                            "copy_matches": "yes" if ok else "NO",
                                            "source_stable": row.get("source_stable", "yes")})
            if any(v["copy_matches"] != "yes" for v in self.verify):
                raise IOError("a staged copy does not match its source hash")
        except BaseException:
            shutil.rmtree(self.work, ignore_errors=True)  # do not leave a bad or partial copy behind
            raise
        moving = [r["file"] for r in self.acq if r.get("source_stable", "yes") != "yes"]
        if moving:
            self.snapshot_note = ("The source changed while it was being copied: %s. The copy may mix two moments "
                                  "and some recent rows may be missing or inconsistent." % ", ".join(moving))

    def con(self, db):
        if db not in self.paths:
            return None
        if db not in self._con:
            c = sqlite3.connect(P.ro_uri(self.paths[db]), uri=True)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA query_only = ON")
            self._con[db] = c
        return self._con[db]

    def has(self, db, table):
        c = self.con(db)
        return bool(c and c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())

    def rows(self, db, sql, *tables):
        """Rows as dicts; [] when the database or any named table is absent (schema varies by build)."""
        if not all(self.has(db, t) for t in tables):
            return []
        return [dict(r) for r in self.con(db).execute(sql)]

    def columns(self, db, table):
        """Column names of a table, [] when the database or table is absent."""
        if (db, table) not in self._cols:
            c = self.con(db)
            self._cols[(db, table)] = [r[1] for r in c.execute(f'PRAGMA table_info("{table}")')] if c else []
        return self._cols[(db, table)]

    def table(self, db, table, cols, order=()):
        """Rows as dicts holding every name in cols; a column this copy lacks comes back None and is recorded in
        self.missing, so a schema variant blanks one field instead of stopping the build. [] when the table is absent.
        order: column names (optionally 'name DESC') applied when the table has them."""
        have = self.columns(db, table)
        if not have:
            return []
        use = [c for c in cols if c in have or c == "rowid"]
        lost = [c for c in cols if c not in use]
        if lost:
            self.missing.setdefault((db, table), set()).update(lost)
        by = []
        for o in order:
            name, _, direction = o.partition(" ")
            if name in have or name == "rowid":
                by.append(f'"{name}" {direction}'.strip())
        sql = "SELECT %s FROM \"%s\"" % (", ".join(f'"{c}"' for c in use) or "1", table)
        if by:
            sql += " ORDER BY " + ", ".join(by)
        blank = dict.fromkeys(cols)
        return [{**blank, **dict(zip(use, r))} for r in self.con(db).execute(sql)]

    def missing_note(self, sources):
        """Sentence naming the columns missing from the given (db, table) sources, '' when none."""
        lost = sorted(f"{t}.{c}" for db, t in dict.fromkeys(sources) for c in self.missing.get((db, t), ()))
        return (" - NOT IN THIS COPY of the database, so these fields are blank: " + ", ".join(lost)) if lost else ""

    def count(self, db, table):
        return self.con(db).execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] if self.has(db, table) else None

    def close(self, remove=True):
        """Close and remove the copies. Returns the folder if it could not be removed, so callers can warn."""
        for c in self._con.values():
            c.close()
        self._con.clear()
        if remove:
            shutil.rmtree(self.work, ignore_errors=True)
            if os.path.exists(self.work):
                return self.work
        return None


# ---------------------------------------------------------------- graph
class View:
    """kind: table | in-table | in-db | check | cross | map | provenance."""

    def __init__(self, vid, title, kind, basis, cols, rows, db=None):
        self.vid, self.title, self.kind, self.basis, self.cols, self.rows = vid, title, kind, basis, cols, rows
        self.db = db


class Graph:
    """Entities (rows with an identity), edges between them, and table views."""

    def __init__(self):
        self.views = {}          # vid -> View
        self.home = {}           # entity -> (vid, row index)
        self.label = {}          # entity -> short text
        self.time = {}           # entity -> unix seconds
        self.edges = defaultdict(list)  # entity -> [(rid, other entity, note)]
        self._edge_set = set()   # (entity, rid, other, note) - O(1) duplicate check; a list scan was quadratic
        self.relations = []      # relation vids in display order

    def add_view(self, v, entity_col=None):
        self.views[v.vid] = v
        if entity_col:
            for i, r in enumerate(v.rows):
                e = r.get(entity_col)
                if e is not None and e not in self.home:
                    self.home[e] = (v.vid, i)

    def link(self, rid, a, b, note=""):
        if a is None or b is None or a == b:
            return
        for x, y in ((a, b), (b, a)):
            k = (x, rid, y, note)
            if k not in self._edge_set:
                self._edge_set.add(k)
                self.edges[x].append((rid, y, note))

    def links(self, entity):
        return self.edges.get(entity, [])


def _ent(kind, key):
    return (kind, key) if key is not None else None


# ---------------------------------------------------------------- contacts index
def contact_index(ev):
    """party key -> [(contact_id, name)] - every candidate, so shared numbers are visible.
    A contact without a name is 'contact <id>'. A number whose contact row no longer exists has no candidate
    (R23 lists those details)."""
    names = {c["contact_id"]: c["display_name"]
             for c in ev.table("contacts.db", "contact", ("contact_id", "display_name"))}
    known = ev.has("contacts.db", "contact")
    idx = defaultdict(list)
    for r in ev.table("contacts.db", "phonenumber", ("phone_number", "display_phone_number", "contact_id"),
                      order=("rowid",)):
        cid = r["contact_id"]
        if known and cid not in names:
            continue
        name = names.get(cid)
        cand = (cid, str(name) if name not in (None, "") else f"contact {cid}")
        for raw in (r["phone_number"], r["display_phone_number"]):
            k, kind, _ = P.norm_addr(raw, DIGITS)
            if k and kind in ("phone", "short") and cand not in idx[k]:
                idx[k].append(cand)
    return idx


def resolve(addr, idx):
    """(party key, kind, name text, candidates, basis) for one address."""
    k, kind, disp = P.norm_addr(addr, DIGITS)
    if kind == "rcs-agent":
        return k, kind, disp, [], "rcs-business-agent (name from the address)"
    cands = idx.get(k, []) if kind in ("phone", "short") else []
    if not cands:
        return k, kind, "", [], ""
    basis = f"contacts.db, last {DIGITS} digits"
    ids = {cid for cid, _ in cands}
    if len(ids) > 1:   # by contact id: two contacts both called "Mum" are still two contacts
        basis += f" - AMBIGUOUS: {len(ids)} contacts share this number"
    return k, kind, " | ".join(sorted(n for _, n in cands)), cands, basis


GROUP_SEP = P.GROUP_SEP  # same rule as plparse: commas, semicolons or 2+ spaces
# contact satellite tables: (table, the column shown as the value)
SATELLITES = (("phonenumber", "display_phone_number"), ("emailaddress", "address"),
              ("postaladdress", "display_address"), ("contactdate", "display_date"), ("contacturl", "url_address"))
R21_MAX_PER_EVENT = 50   # links one event gets in R21; a burst of n events would otherwise make n*n/2 pairs
TIMED_KINDS = ("call", "sms", "mms", "photo", "notif", "handoff", "rcs_chat", "rcs_filetransfer")


def split_recipients(recipient_list):
    s = str(recipient_list or "").strip()
    if not s:
        return []
    parts = [x.strip() for x in GROUP_SEP.split(s) if x.strip()]
    return parts or [s]


def view_sources(vid):
    """(database, table) pairs a view is made from, to say which missing columns can blank its fields."""
    def src(db, *tables):
        return [(db, t) for t in tables]
    ct, ph, nt = "contacts.db", "phone.db", "notifications.db"
    contact = src(ct, "contact", *[t for t, _ in SATELLITES])
    names = src(ct, "phonenumber", "contact")
    calls, sms, threads = src("calling.db", "call_history"), src(ph, "message"), src(ph, "conversation")
    part, mms, sims, rcs = src(ph, "participant"), src(ph, "mms"), src(ph, "subscription"), src(ph, "rcs_conversation")
    rcs_rows = src(ph, "rcs_chat", "rcs_filetransfer")
    photo, apps = src("photos.db", "media"), src("phoneapps.db", "phone_apps")
    cont = src("continuity.db", "browser_history", "continue_app_context")
    timeline = calls + sms + mms + photo + rcs_rows + src(nt, "notifications") + src("continuity.db", "continue_app_context")
    table_of = {
        "E_contacts": contact, "E_calls": calls, "E_threads": threads, "E_sms": sms, "E_mms": mms, "E_participants": part,
        "E_rcs": rcs, "E_sims": sims, "E_photos": photo, "E_apps": apps, "E_recent": src("phoneapps.db", "recent_apps"),
        "E_pcapp": src("phoneapps.db", "pcapp_data"), "E_browser": src("continuity.db", "browser_history"),
        "E_handoff": src("continuity.db", "continue_app_context"), "E_settings": src("settings.db", "settings"),
        "R01": contact, "R02": src(ct, "contact_index", "contact"), "R03": sms + threads, "R04": part + threads,
        "R05": mms + src(ph, "mms_part"), "R06": rcs_rows + rcs, "R07": rcs + threads,
        "R08": src(ph, "conversation_attribute"), "R09": src(ph, "sending_message") + sms,
        "R10": sms + sims, "R11": apps + src("phoneapps.db", "recent_apps", "pcapp_data"),
        "R13": calls + names, "R14": calls + sims, "R15": sms + names, "R16": threads + names, "R17": part + names,
        "R18": calls + sms + threads + names,
        "R19": src(nt, "notifications") + apps + src("settings.db", "settings") + cont,
        "R20": calls + photo, "R21": timeline, "R22": timeline,
        "R23": part + sms + threads + rcs + contact, "R24": calls + sms + threads + names + contact,
        "R25": [(db, "content_sequence") for db in P.DB_NAMES],
        "N_toasts": src(nt, "notification_toastmappings"), "N_requests": src(nt, "phone_requests"),
        "ND1": src(nt, "notifications", "notification_toastmappings"),
        "ND2": src(nt, "notifications", "phone_requests"),
    }
    if vid in table_of:
        return table_of[vid]
    return src(nt, "notifications") if vid.startswith(("N_", "NR", "ND")) else []


def near_pairs(events, w, cap):
    """Pairs (i, j), i < j, of events (sorted (time, entity)) at most w seconds apart, photo-photo pairs left out.
    An event is in at most cap pairs; the nearest pairs win. Returns (pairs sorted, number left out by the cap).
    Work is about n * cap, not n * n: a burst of thousands of events must not make millions of pairs."""
    n = len(events)
    times = [t for t, _ in events]
    photo = [e[0] == "photo" for _, e in events]
    non_photo = [i for i in range(n) if not photo[i]]
    np_times = [times[i] for i in non_photo]

    def nearest(i):
        t = times[i]
        if photo[i]:       # a photo pairs with non-photos only, so look in that list (the photo is not in it)
            ids, ts = non_photo, np_times
            lo = bisect.bisect_left(ts, t) - 1
            hi = lo + 1
        else:
            ids, ts = range(n), times
            lo, hi = i - 1, i + 1
        out = []
        while len(out) < cap:
            before = t - ts[lo] if lo >= 0 and t - ts[lo] <= w else None
            after = ts[hi] - t if hi < len(ts) and ts[hi] - t <= w else None
            if before is None and after is None:
                break
            if after is None or (before is not None and before <= after):
                out.append(ids[lo])
                lo -= 1
            else:
                out.append(ids[hi])
                hi += 1
        return out

    cand = set()
    for i in range(n):
        for j in nearest(i):
            cand.add((min(i, j), max(i, j)))
    used, kept = defaultdict(int), []
    for i, j in sorted(cand, key=lambda ij: (times[ij[1]] - times[ij[0]], ij)):
        if used[i] < cap and used[j] < cap:
            used[i] += 1
            used[j] += 1
            kept.append((i, j))
    # every pair inside the window, counted without listing them
    photos_before = [0]
    for is_photo in photo:
        photos_before.append(photos_before[-1] + is_photo)
    total = 0
    for i in range(n):
        end = bisect.bisect_right(times, times[i] + w)
        while end < n and times[end] - times[i] <= w:
            end += 1
        while end > i + 1 and times[end - 1] - times[i] > w:
            end -= 1
        total += end - i - 1 - (photos_before[end] - photos_before[i + 1] if photo[i] else 0)
    return sorted(kept), total - len(kept)


# ---------------------------------------------------------------- build
def build(ev, window_min=DEFAULT_WINDOW_MIN, map_dirs=None):
    g = Graph()
    idx = contact_index(ev)
    add = g.add_view

    # ---------- entity views
    T = ev.table   # rows as dicts; a column this copy lacks is None and is named in the view's basis
    contacts = T("contacts.db", "contact", ("contact_id", "display_name", "company", "last_updated_time"),
                 order=("display_name",))
    contact_name = {c["contact_id"]: c["display_name"] for c in contacts}
    sat = defaultdict(lambda: defaultdict(list))
    for table, col in SATELLITES:
        for r in T("contacts.db", table, ("contact_id", col)):
            sat[r["contact_id"]][table].append(str(r[col] or ""))
    rows = []
    for c in contacts:
        e = _ent("contact", c["contact_id"])
        g.label[e] = c["display_name"] or f"contact {c['contact_id']}"
        rows.append({"_e": e, "ContactID": c["contact_id"], "Name": c["display_name"], "Company": c["company"],
                     "Numbers": "; ".join(sat[c["contact_id"]]["phonenumber"]),
                     "Emails": "; ".join(sat[c["contact_id"]]["emailaddress"]),
                     "LastUpdatedUTC": iso(ts_seconds(c["last_updated_time"]))})
    add(View("E_contacts", "Contacts", "entity", "contacts.db contact + satellite tables",
             ["ContactID", "Name", "Company", "Numbers", "Emails", "LastUpdatedUTC"], rows), "_e")

    calls = T("calling.db", "call_history", ("call_id", "start_time", "phone_number", "call_type", "duration",
                                             "phone_account_id"), order=("start_time",))
    rows = []
    for c in calls:
        e = _ent("call", c["call_id"])
        t = ts_seconds(c["start_time"])
        k, kind, name, cands, basis = resolve(c["phone_number"], idx)
        g.time[e] = t
        g.label[e] = f"{P.CALL_TYPE.get(c['call_type'], c['call_type'])} call {name or c['phone_number']}"
        rows.append({"_e": e, "_party": k, "_cands": cands, "_basis": basis, "_dur": c["duration"],
                     "CallID": c["call_id"], "StartUTC": iso(t),
                     "Type": P.CALL_TYPE.get(c["call_type"], c["call_type"]), "Number": c["phone_number"],
                     "ContactName": name, "NameBasis": basis, "DurationSec": c["duration"],
                     "SIM": c["phone_account_id"]})
    add(View("E_calls", "Calls", "entity", "calling.db call_history",
             ["CallID", "StartUTC", "Type", "Number", "ContactName", "NameBasis", "DurationSec", "SIM"], rows), "_e")
    call_rows = rows

    threads = T("phone.db", "conversation", ("thread_id", "recipient_list", "timestamp", "msg_count", "unread_count"),
                order=("timestamp DESC",))
    rows = []
    for t in threads:
        e = _ent("thread", t["thread_id"])
        members = split_recipients(t["recipient_list"])
        resolved = [resolve(m, idx) for m in members]
        names = " | ".join(r[2] or (m) for r, m in zip(resolved, members)) if len(members) > 1 else (
            resolved[0][2] if resolved else "")
        g.time[e] = ts_seconds(t["timestamp"])
        g.label[e] = f"SMS thread {names or t['recipient_list']}"
        rows.append({"_e": e, "_members": resolved, "ThreadID": t["thread_id"], "Recipients": t["recipient_list"],
                     "ContactNames": names, "Group": "yes" if len(members) > 1 else "",
                     "LastUTC": iso(g.time[e]), "Messages": t["msg_count"], "Unread": t["unread_count"]})
    add(View("E_threads", "SMS threads", "entity", "phone.db conversation",
             ["ThreadID", "Recipients", "ContactNames", "Group", "LastUTC", "Messages", "Unread"], rows), "_e")
    thread_rows = rows

    msgs = T("phone.db", "message", ("message_id", "thread_id", "from_address", "to_address", "type", "body",
                                     "timestamp", "subscription_id"), order=("timestamp",))
    rows = []
    for m in msgs:
        e = _ent("sms", m["message_id"])
        addr = m["from_address"] or m["to_address"]
        k, kind, name, cands, basis = resolve(addr, idx)
        g.time[e] = ts_seconds(m["timestamp"])
        g.label[e] = f"SMS {P.MSG_TYPE.get(m['type'], m['type'])} {name or addr or ''}"
        rows.append({"_e": e, "_party": k, "_cands": cands, "MessageID": m["message_id"], "ThreadID": m["thread_id"],
                     "TimeUTC": iso(g.time[e]), "Type": P.MSG_TYPE.get(m["type"], m["type"]), "Address": addr,
                     "ContactName": name, "NameBasis": basis, "Body": m["body"], "SIM": m["subscription_id"]})
    add(View("E_sms", "SMS messages", "entity", "phone.db message",
             ["MessageID", "ThreadID", "TimeUTC", "Type", "Address", "ContactName", "NameBasis", "Body", "SIM"], rows),
        "_e")
    sms_rows = rows

    mms = T("phone.db", "mms", ("message_id", "thread_id", "timestamp", "from_address", "to_addresses", "subject",
                                "subscription_id"), order=("timestamp",))
    rows = [{"_e": _ent("mms", m["message_id"]), "MessageID": m["message_id"], "ThreadID": m["thread_id"],
             "TimeUTC": iso(ts_seconds(m["timestamp"])), "From": m["from_address"], "To": m["to_addresses"],
             "Subject": m["subject"], "SIM": m["subscription_id"]} for m in mms]
    for r, m in zip(rows, mms):
        g.time[r["_e"]] = ts_seconds(m["timestamp"])
        g.label[r["_e"]] = f"MMS {m['from_address'] or ''}"
    add(View("E_mms", "MMS", "entity", "phone.db mms",
             ["MessageID", "ThreadID", "TimeUTC", "From", "To", "Subject", "SIM"], rows), "_e")

    parts = T("phone.db", "participant", ("id", "thread_id", "send_address", "normalized_address"))
    rows = []
    for p in parts:
        e = _ent("participant", p["id"])
        k, kind, name, cands, basis = resolve(p["normalized_address"] or p["send_address"], idx)
        g.label[e] = f"participant {name or p['send_address']}"
        rows.append({"_e": e, "_party": k, "_cands": cands, "ParticipantID": p["id"], "ThreadID": p["thread_id"],
                     "Address": p["send_address"], "Normalized": p["normalized_address"], "ContactName": name,
                     "NameBasis": basis})
    add(View("E_participants", "Participants", "entity", "phone.db participant",
             ["ParticipantID", "ThreadID", "Address", "Normalized", "ContactName", "NameBasis"], rows), "_e")
    part_rows = rows

    rcs = T("phone.db", "rcs_conversation", ("thread_id", "fallback_id", "recipient_list", "timestamp", "msg_count"))
    rows = [{"_e": _ent("rcsthread", r["thread_id"]), "RCSThreadID": r["thread_id"], "FallbackID": r["fallback_id"],
             "Recipients": r["recipient_list"], "LastUTC": iso(ts_seconds(r["timestamp"])), "Messages": r["msg_count"]}
            for r in rcs]
    for r in rows:
        g.label[r["_e"]] = f"RCS thread {r['Recipients']}"
    add(View("E_rcs", "RCS threads", "entity", "phone.db rcs_conversation",
             ["RCSThreadID", "FallbackID", "Recipients", "LastUTC", "Messages"], rows), "_e")

    subs = T("phone.db", "subscription", ("subscription_id", "sim_slot_index", "name", "number", "country_iso"))
    rows = [{"_e": _ent("sim", str(s["subscription_id"])), "SubscriptionID": s["subscription_id"],
             "Slot": s["sim_slot_index"], "Carrier": s["name"], "Number": s["number"], "Country": s["country_iso"]}
            for s in subs]
    for r in rows:
        g.label[r["_e"]] = f"SIM {r['Carrier'] or r['SubscriptionID']}"
    add(View("E_sims", "SIMs", "entity", "phone.db subscription",
             ["SubscriptionID", "Slot", "Carrier", "Number", "Country"], rows), "_e")

    photos = T("photos.db", "media", ("id", "name", "taken_time", "last_updated_time", "size", "uri"),
               order=("taken_time",))
    rows = []
    for p in photos:
        e = _ent("photo", p["id"])
        g.time[e] = ts_seconds(p["taken_time"])
        g.label[e] = f"photo {p['name']}"
        rows.append({"_e": e, "MediaID": p["id"], "Name": p["name"], "TakenUTC": iso(g.time[e]),
                     "SyncedUTC": iso(ts_seconds(p["last_updated_time"])), "SizeBytes": p["size"], "URI": p["uri"]})
    add(View("E_photos", "Photos", "entity", "photos.db media",
             ["MediaID", "Name", "TakenUTC", "SyncedUTC", "SizeBytes", "URI"], rows), "_e")
    photo_rows = rows

    apps = T("phoneapps.db", "phone_apps", ("app_name", "package_name", "version", "favorite_rank",
                                            "last_updated_time"), order=("app_name",))
    rows = []
    for a in apps:
        e = _ent("app", a["package_name"])
        g.label[e] = f"app {a['app_name']}"
        rows.append({"_e": e, "AppName": a["app_name"], "Package": a["package_name"], "Version": a["version"],
                     "FavoriteRank": a["favorite_rank"], "SyncedUTC": iso(ts_seconds(a["last_updated_time"]))})
    add(View("E_apps", "Installed apps", "entity", "phoneapps.db phone_apps",
             ["AppName", "Package", "Version", "FavoriteRank", "SyncedUTC"], rows), "_e")

    simple = [
        ("E_recent", "Recent apps", "phoneapps.db", "recent_apps", "recentapp", "app_id", ("recent_app_rank",),
         ["app_id", "recent_app_rank", "app_name", "package_name", "intent_action", "task_id"]),
        ("E_pcapp", "PC counterparts", "phoneapps.db", "pcapp_data", "pcapp", "id", (),
         ["id", "package_name", "is_installed", "platformapp_id", "platformapp_package", "platformapp_url"]),
        ("E_browser", "Phone browser history", "continuity.db", "browser_history", "browser", "browser_history_id",
         ("browser_history_rank",),
         ["browser_history_id", "browser_history_rank", "browser_history_title", "web_uri", "package_name"]),
        ("E_handoff", "Cross-device handoff", "continuity.db", "continue_app_context", "handoff", "id",
         ("create_time",), ["id", "context_id", "app_id", "title", "web_link", "create_time"]),
        ("E_settings", "App settings", "settings.db", "settings", "setting", "setting_key", (),
         ["setting_group_id", "setting_key", "setting_type", "setting_value", "setting_mute_expiration_timestamp"]),
    ]
    simple_rows = {}
    for vid, title, db, table, kind, key, order, cols in simple:
        rs = T(db, table, cols, order=order)
        rows = []
        for r in rs:
            e = _ent(kind, r[key])
            out = {"_e": e}
            for c in cols:
                v = r.get(c)
                out[c] = iso(ts_seconds(v)) if c in ("post_time", "create_time") else v
            rows.append(out)
            g.label[e] = f"{title.lower()} {r.get('app_name') or r.get('title') or r.get('package_name') or r[key]}"
            if "post_time" in cols:
                g.time[e] = ts_seconds(r["post_time"])
            if "create_time" in cols:
                g.time[e] = ts_seconds(r["create_time"])
        simple_rows[vid] = (rs, rows)
        add(View(vid, title, "entity", f"{db} {table}", cols, rows), "_e")

    # notifications.db: live + carved rows, json unpacked, its own relations (plnotif)
    plnotif.build(ev, g, View, ts_seconds, iso)

    # ---------- relations
    def rel(rid, title, kind, basis, cols, rows):
        add(View(rid, f"{rid}  {title}", kind, basis, cols, rows))
        g.relations.append(rid)

    # R01 contact -> satellite rows
    rows = []
    for table, col in SATELLITES:
        for r in T("contacts.db", table, ("contact_id", col)):
            rows.append({"ContactID": r["contact_id"], "Contact": contact_name.get(r["contact_id"]), "Table": table,
                         "Value": r[col]})
    rel("R01", "Contact -> numbers, emails, addresses, dates, URLs", "schema",
        "contact_id in each satellite table = contact.contact_id (contacts.db)", ["ContactID", "Contact", "Table", "Value"],
        rows)

    # R02 deleted contact details (tombstones)
    rows = []
    for r in T("contacts.db", "contact_index", ("contact_id", "foreign_table_name", "foreign_id")):
        rows.append({"ContactID": r["contact_id"], "Contact": contact_name.get(r["contact_id"]),
                     "DeletedFrom": r["foreign_table_name"], "DeletedRowID": r["foreign_id"]})
    rel("R02", "Deleted contact details (tombstones)", "schema",
        "contact_index rows written by BEFORE DELETE triggers on the satellite tables (contacts.db)",
        ["ContactID", "Contact", "DeletedFrom", "DeletedRowID"], rows)

    # R03 thread -> messages, R04 thread -> participants
    rows = []
    for m in sms_rows:
        g.link("R03", _ent("thread", m["ThreadID"]), m["_e"])
        rows.append({"ThreadID": m["ThreadID"], "MessageID": m["MessageID"], "TimeUTC": m["TimeUTC"], "Type": m["Type"],
                     "Address": m["Address"]})
    rel("R03", "SMS thread -> messages", "id-join", "message.thread_id = conversation.thread_id (phone.db)",
        ["ThreadID", "MessageID", "TimeUTC", "Type", "Address"], rows)
    rows = []
    for p in part_rows:
        g.link("R04", _ent("thread", p["ThreadID"]), p["_e"])
        rows.append({"ThreadID": p["ThreadID"], "ParticipantID": p["ParticipantID"], "Address": p["Address"],
                     "ContactName": p["ContactName"]})
    rel("R04", "SMS thread -> participants", "id-join", "participant.thread_id = conversation.thread_id (phone.db)",
        ["ThreadID", "ParticipantID", "Address", "ContactName"], rows)

    # R05 thread -> MMS -> parts
    rows = []
    mms_parts = defaultdict(list)
    for p in T("phone.db", "mms_part", ("message_id", "part_id", "content_type", "name", "size")):
        mms_parts[p["message_id"]].append(p)
    for m in T("phone.db", "mms", ("thread_id", "message_id")):
        g.link("R05", _ent("thread", m["thread_id"]), _ent("mms", m["message_id"]))
        for r in mms_parts.get(m["message_id"]) or [{}]:    # a message without parts still gets its row
            rows.append({"ThreadID": m["thread_id"], "MMSID": m["message_id"], "PartID": r.get("part_id"),
                         "ContentType": r.get("content_type"), "Name": r.get("name"), "Size": r.get("size")})
    rel("R05", "SMS thread -> MMS -> attachments", "id-join",
        "mms.thread_id = conversation.thread_id; mms_part.message_id = mms.message_id (phone.db)",
        ["ThreadID", "MMSID", "PartID", "ContentType", "Name", "Size"], rows)

    # R06 RCS thread -> chats / file transfers
    rows = []
    for table, what in (("rcs_chat", "RCS chat"), ("rcs_filetransfer", "RCS file transfer")):
        for r in T("phone.db", table, ("thread_id", "message_id", "from_address", "timestamp")):
            e = _ent(table, r["message_id"])
            g.link("R06", _ent("rcsthread", r["thread_id"]), e)
            if e:   # a timed row of its own: it joins the timeline (R21, R22); its home is this row
                g.time[e] = ts_seconds(r["timestamp"])
                g.label[e] = f"{what} {r['from_address'] or ''}".strip()
                g.home.setdefault(e, ("R06", len(rows)))
            rows.append({"_e": e, "RCSThreadID": r["thread_id"], "Table": table, "MessageID": r["message_id"],
                         "From": r["from_address"], "TimeUTC": iso(ts_seconds(r["timestamp"]))})
    rel("R06", "RCS thread -> chats and file transfers", "id-join",
        "rcs_chat / rcs_filetransfer.thread_id = rcs_conversation.thread_id (phone.db)",
        ["RCSThreadID", "Table", "MessageID", "From", "TimeUTC"], rows)

    # R07 RCS thread -> SMS fallback thread
    sms_threads = {t["ThreadID"] for t in thread_rows}
    rows = []
    for r in rcs:
        found = r["fallback_id"] in sms_threads
        if found:
            g.link("R07", _ent("rcsthread", r["thread_id"]), _ent("thread", r["fallback_id"]))
        rows.append({"RCSThreadID": r["thread_id"], "FallbackID": r["fallback_id"],
                     "SMSThreadFound": "yes" if found else "no", "Recipients": r["recipient_list"]})
    rel("R07", "RCS thread -> SMS fallback thread", "id-join",
        "rcs_conversation.fallback_id = conversation.thread_id (phone.db) - meaning inferred from the column name",
        ["RCSThreadID", "FallbackID", "SMSThreadFound", "Recipients"], rows)

    # R08 thread -> pin/mute/hide
    rows = []
    for r in T("phone.db", "conversation_attribute", ("thread_id", "rcs_thread_id", "position", "is_muted",
                                                       "is_hidden")):
        rows.append({"ThreadID": r["thread_id"], "RCSThreadID": r["rcs_thread_id"], "PinPosition": r["position"],
                     "Muted": r["is_muted"], "Hidden": r["is_hidden"]})
    rel("R08", "Thread -> pinned / muted / hidden state", "id-join",
        "conversation_attribute.thread_id = conversation.thread_id (phone.db); -1 = never set",
        ["ThreadID", "RCSThreadID", "PinPosition", "Muted", "Hidden"], rows)

    # R09 outbound queue
    rows = []
    for r in T("phone.db", "sending_message", ("id", "thread_id", "message_id", "message_kind", "result_code")):
        g.link("R09", _ent("thread", r["thread_id"]), _ent("sms", r["message_id"]))
        rows.append({"QueueID": r["id"], "ThreadID": r["thread_id"], "MessageID": r["message_id"],
                     "Kind": r["message_kind"], "ResultCode": r["result_code"]})
    rel("R09", "Outbound queue -> thread and message", "id-join",
        "sending_message.thread_id / message_id (phone.db) - messages the PC was sending",
        ["QueueID", "ThreadID", "MessageID", "Kind", "ResultCode"], rows)

    # R10 messages -> SIM ; R14 calls -> SIM
    sims = {str(s["subscription_id"]) for s in subs}
    rows = []
    for m in sms_rows:
        hit = str(m["SIM"]) in sims
        if hit:
            g.link("R10", m["_e"], _ent("sim", str(m["SIM"])))
        rows.append({"MessageID": m["MessageID"], "SubscriptionID": m["SIM"], "SIMFound": "yes" if hit else "no"})
    rel("R10", "SMS -> SIM", "id-join", "message.subscription_id = subscription.subscription_id (phone.db)",
        ["MessageID", "SubscriptionID", "SIMFound"], rows)

    # R11 installed app -> recent app / PC counterpart
    app_pk = {a["package_name"] for a in apps}
    rows = []
    for vid, kind, col in (("E_recent", "recent app", "package_name"), ("E_pcapp", "PC counterpart", "package_name")):
        raw, view_rows = simple_rows[vid]
        for r, vr in zip(raw, view_rows):
            hit = r[col] in app_pk
            if hit:
                g.link("R11", _ent("app", r[col]), vr["_e"])
            rows.append({"Package": r[col], "Link": kind, "InstalledAppFound": "yes" if hit else "no"})
    rel("R11", "Installed app -> recent app / PC counterpart", "schema",
        "recent_apps / pcapp_data.package_name = phone_apps.package_name (phoneapps.db; pcapp_data declares it a key)",
        ["Package", "Link", "InstalledAppFound"], rows)

    # R13 call -> contact
    rows = []
    for c in call_rows:
        for cid, name in c["_cands"]:
            g.link("R13", c["_e"], _ent("contact", cid), c["_basis"])
        rows.append({"CallID": c["CallID"], "StartUTC": c["StartUTC"], "Number": c["Number"],
                     "ContactName": c["ContactName"], "Candidates": len(c["_cands"]), "Basis": c["_basis"]})
    rel("R13", "Call -> contact", "examiner",
        f"last {DIGITS} digits of call_history.phone_number = contacts.db phonenumber (examiner's join)",
        ["CallID", "StartUTC", "Number", "ContactName", "Candidates", "Basis"], rows)

    # R14 call -> SIM
    rows = []
    for c in call_rows:
        hit = str(c["SIM"]) in sims
        if hit:
            g.link("R14", c["_e"], _ent("sim", str(c["SIM"])))
        rows.append({"CallID": c["CallID"], "PhoneAccountID": c["SIM"], "SIMFound": "yes" if hit else "no"})
    rel("R14", "Call -> SIM", "examiner",
        "call_history.phone_account_id = phone.db subscription.subscription_id (examiner's join across databases)",
        ["CallID", "PhoneAccountID", "SIMFound"], rows)

    # R15 SMS -> contact ; R16 thread -> contacts ; R17 participant -> contact
    rows = []
    for m in sms_rows:
        for cid, name in m["_cands"]:
            g.link("R15", m["_e"], _ent("contact", cid))
        rows.append({"MessageID": m["MessageID"], "Address": m["Address"], "ContactName": m["ContactName"],
                     "Candidates": len(m["_cands"])})
    rel("R15", "SMS message -> contact", "examiner",
        f"last {DIGITS} digits of message.from_address / to_address = contacts.db phonenumber (examiner's join)",
        ["MessageID", "Address", "ContactName", "Candidates"], rows)
    rows = []
    for t in thread_rows:
        for member, (k, kind, name, cands, basis) in zip(split_recipients(t["Recipients"]), t["_members"]):
            for cid, _ in cands:
                g.link("R16", t["_e"], _ent("contact", cid))
            rows.append({"ThreadID": t["ThreadID"], "Member": member, "Kind": kind, "ContactName": name,
                         "Candidates": len(cands)})
    rel("R16", "SMS thread -> contact (each member)", "examiner",
        "each recipient in conversation.recipient_list matched on its last digits; group threads are split per member",
        ["ThreadID", "Member", "Kind", "ContactName", "Candidates"], rows)
    rows = []
    for p in part_rows:
        for cid, _ in p["_cands"]:
            g.link("R17", p["_e"], _ent("contact", cid))
        rows.append({"ParticipantID": p["ParticipantID"], "Address": p["Address"], "ContactName": p["ContactName"],
                     "Candidates": len(p["_cands"])})
    rel("R17", "Participant -> contact", "examiner",
        "participant.normalized_address (or send_address) matched on its last digits to contacts.db (examiner's join)",
        ["ParticipantID", "Address", "ContactName", "Candidates"], rows)

    # R18 parties across calls and SMS
    party = defaultdict(lambda: {"calls": [], "sms": [], "threads": set(), "cands": [], "names": set(), "disp": ""})
    for c in call_rows:
        if c["_party"]:
            p = party[c["_party"]]
            p["calls"].append(c)
            p["disp"] = p["disp"] or c["Number"]
            p["cands"], p["names"] = c["_cands"], {n for _, n in c["_cands"] if n}
    for m in sms_rows:
        if m["_party"]:
            p = party[m["_party"]]
            p["sms"].append(m)
            p["threads"].add(m["ThreadID"])
            p["disp"] = p["disp"] or m["Address"]
            if m["_cands"]:
                p["cands"], p["names"] = m["_cands"], {n for _, n in m["_cands"] if n}
    for t in thread_rows:
        for k, kind, name, cands, basis in t["_members"]:
            if k and k in party:
                party[k]["threads"].add(t["ThreadID"])
    rows = []
    for k, p in sorted(party.items(), key=lambda kv: -(len(kv[1]["calls"]) + len(kv[1]["sms"]))):
        e = _ent("party", k)
        g.label[e] = f"party {' | '.join(sorted(p['names'])) or p['disp']}"
        for c in p["calls"]:
            g.link("R18", e, c["_e"])
        for m in p["sms"]:
            g.link("R18", e, m["_e"])
        for th in p["threads"]:
            g.link("R18", e, _ent("thread", th))
        for cid, _ in p["cands"]:
            g.link("R18", e, _ent("contact", cid))
        times = [x for x in [g.time.get(c["_e"]) for c in p["calls"]] + [g.time.get(m["_e"]) for m in p["sms"]] if x]
        rows.append({"_e": e, "Party": p["disp"], "ContactName": " | ".join(sorted(p["names"])),
                     "Calls": len(p["calls"]), "TalkSec": sum(whole(c["_dur"]) or 0 for c in p["calls"]),
                     "SMS": len(p["sms"]), "Threads": len(p["threads"]),
                     "BothChannels": "yes" if p["calls"] and (p["sms"] or p["threads"]) else "",
                     "FirstUTC": iso(min(times)) if times else "", "LastUTC": iso(max(times)) if times else ""})
    rel("R18", "Same party across calls and SMS", "examiner",
        f"calls, messages and thread members grouped by the last {DIGITS} digits of the address (examiner's join)",
        ["Party", "ContactName", "Calls", "TalkSec", "SMS", "Threads", "BothChannels", "FirstUTC", "LastUTC"], rows)
    g.home.update({r["_e"]: ("R18", i) for i, r in enumerate(rows)})

    # R19 notification / setting / browser page / handoff -> installed app
    rows = []
    nrows = g.views["N_notifications"].rows if "N_notifications" in g.views else []
    sources = [("notification", [{"package_name": r["Package"]} for r in nrows], nrows, "package_name")]
    for vid, col, what in (("E_settings", "setting_key", "app setting"), ("E_browser", "package_name", "browser page"),
                           ("E_handoff", "app_id", "handoff")):
        sources.append((what, simple_rows[vid][0], simple_rows[vid][1], col))
    for what, raw, view_rows, col in sources:
        for r, vr in zip(raw, view_rows):
            hit = r.get(col) in app_pk
            if hit:
                g.link("R19", vr["_e"], _ent("app", r[col]))
            rows.append({"Source": what, "Package": r.get(col), "InstalledAppFound": "yes" if hit else "no"})
    rel("R19", "Notification / setting / browser page / handoff -> installed app", "examiner",
        "package name in notifications.db, settings.db and continuity.db = phoneapps.db phone_apps.package_name",
        ["Source", "Package", "InstalledAppFound"], rows)

    # R20 photo taken during a call
    timed_calls = [(g.time[c["_e"]], g.time[c["_e"]] + (whole(c["_dur"]) or 0), c) for c in call_rows
                   if g.time.get(c["_e"]) and (whole(c["_dur"]) or 0) > 0]
    # sorted by start + bisect instead of photos x calls; candidates are put back in call order so the
    # output is the same as the plain double loop
    by_start = sorted(range(len(timed_calls)), key=lambda i: timed_calls[i][0])
    starts = [timed_calls[i][0] for i in by_start]
    longest = max((e_ - s for s, e_, _ in timed_calls), default=0)
    rows = []
    for p in photo_rows:
        t = g.time.get(p["_e"])
        if t is None:
            continue
        lo, hi = bisect.bisect_left(starts, t - longest), bisect.bisect_right(starts, t)
        for s, e_, c in (timed_calls[i] for i in sorted(by_start[lo:hi])):
            if s <= t <= e_:
                g.link("R20", p["_e"], c["_e"], "photo taken during the call")
                rows.append({"MediaID": p["MediaID"], "Photo": p["Name"], "TakenUTC": p["TakenUTC"],
                             "CallID": c["CallID"], "CallStartUTC": c["StartUTC"], "CallSec": c["_dur"],
                             "Party": c["ContactName"] or c["Number"]})
    rel("R20", "Photo taken during a call", "time",
        "media.taken_time between call start and start + duration. Both clocks are the phone's",
        ["MediaID", "Photo", "TakenUTC", "CallID", "CallStartUTC", "CallSec", "Party"], rows)

    # R21 events near each other ; R22 unified timeline
    events = sorted(((t, e) for e, t in g.time.items() if t and e[0] in TIMED_KINDS), key=lambda x: x[0])
    pairs, left_out = near_pairs(events, window_min * 60, R21_MAX_PER_EVENT)
    rows = []
    for i, j in pairs:
        (t, e), (t2, e2) = events[i], events[j]
        g.link("R21", e, e2, f"{int(t2 - t)} s apart")
        rows.append({"FirstUTC": iso(t), "First": g.label.get(e, str(e)), "SecondUTC": iso(t2),
                     "Second": g.label.get(e2, str(e2)), "GapSec": int(t2 - t)})
    rel("R21", f"Events within {window_min} minutes of each other", "time",
        f"any two of call / SMS / MMS / RCS chat / RCS file / photo / notification / handoff at most {window_min}"
        " minutes apart (photo-photo bursts left out). Each event is linked to its"
        f" {R21_MAX_PER_EVENT} nearest at most; pairs left out by that limit: {left_out}."
        " A lead to check, not proof of a link",
        ["FirstUTC", "First", "SecondUTC", "Second", "GapSec"], rows)
    rows = [{"_e": e, "TimeUTC": iso(t), "Kind": e[0], "What": g.label.get(e, str(e))} for t, e in events]
    rel("R22", "Unified timeline", "time", "every timed row from every database, phone clock, sorted",
        ["TimeUTC", "Kind", "What"], rows)

    # R23 orphans. A key column this copy lacks is not a finding (it would flag every row), so such tables are skipped
    def has_col(db, table, col):
        return col in ev.columns(db, table)

    rows = []
    for p in part_rows:
        if has_col("phone.db", "participant", "thread_id") and p["ThreadID"] not in sms_threads:
            rows.append({"Problem": "participant without thread", "Table": "participant", "ID": p["ParticipantID"],
                         "MissingKey": p["ThreadID"]})
    for m in sms_rows:
        if has_col("phone.db", "message", "thread_id") and m["ThreadID"] not in sms_threads:
            rows.append({"Problem": "message without thread", "Table": "message", "ID": m["MessageID"],
                         "MissingKey": m["ThreadID"]})
    contact_ids = {c["ContactID"] for c in g.views["E_contacts"].rows}
    for table, _ in SATELLITES:
        if not has_col("contacts.db", table, "contact_id"):
            continue
        for r in T("contacts.db", table, ("rowid", "contact_id")):
            if r["contact_id"] not in contact_ids:
                rows.append({"Problem": "contact detail without contact", "Table": table, "ID": r["rowid"],
                             "MissingKey": r["contact_id"]})
    for r in rcs:
        if has_col("phone.db", "rcs_conversation", "fallback_id") and r["fallback_id"] not in sms_threads:
            rows.append({"Problem": "RCS thread without SMS fallback thread", "Table": "rcs_conversation",
                         "ID": r["thread_id"], "MissingKey": r["fallback_id"]})
    rel("R23", "Orphans: rows whose partner is missing", "integrity",
        "a key that points at a row which no longer exists - deletion, partial sync or pruning",
        ["Problem", "Table", "ID", "MissingKey"], rows)

    # R24 unknown parties and dormant contacts
    rows = []
    for r in g.views["R18"].rows:
        if not r["ContactName"]:
            rows.append({"Finding": "communicated with, not in contacts", "Party": r["Party"], "Contact": "",
                         "Calls": r["Calls"], "SMS": r["SMS"]})
    linked = {e2 for e, lst in g.edges.items() if e[0] in ("call", "sms", "thread") for rid, e2, _ in lst
              if e2[0] == "contact"}
    for c in g.views["E_contacts"].rows:
        if c["_e"] not in linked and c["Numbers"]:
            rows.append({"Finding": "contact with no call or SMS on this PC", "Party": c["Numbers"],
                         "Contact": c["Name"], "Calls": 0, "SMS": 0})
    rel("R24", "Unknown parties and dormant contacts", "integrity",
        "derived from R13-R18: parties with no contact match, and contacts no call or message points to",
        ["Finding", "Party", "Contact", "Calls", "SMS"], rows)

    # R25 sync completeness
    rows = []
    for db in P.DB_NAMES:
        for r in T(db, "content_sequence", ("content_type", "sequence", "expected")):
            fdb, table = FEEDS.get(r["content_type"], (db, None))
            present = ev.count(fdb, table) if table else None
            status = ""
            seq, exp = whole(r["sequence"]), whole(r["expected"])
            if seq is not None and exp is not None:
                if seq >= 0 and exp >= 0:
                    status = "complete" if seq == exp else "INCOMPLETE (sequence != expected)"
            elif r["sequence"] is not None and r["expected"] is not None:
                status = "cannot compare (a value is not a number)"
            rows.append({"Database": db, "Feed": r["content_type"], "Table": table or "?", "Sequence": r["sequence"],
                         "Expected": r["expected"], "RowsPresent": present, "Status": status})
    rel("R25", "Sync completeness per feed", "integrity",
        "content_sequence.sequence (received) vs expected (phone's count) vs rows present now. Undocumented field: a"
        " mismatch is a lead, not proof", ["Database", "Feed", "Table", "Sequence", "Expected", "RowsPresent", "Status"],
        rows)

    # ---------- database + scope of every view
    DBOF = {"E_contacts": "contacts.db", "E_calls": "calling.db", "E_threads": "phone.db", "E_sms": "phone.db",
            "E_mms": "phone.db", "E_participants": "phone.db", "E_rcs": "phone.db", "E_sims": "phone.db",
            "E_photos": "photos.db", "E_apps": "phoneapps.db", "E_recent": "phoneapps.db", "E_pcapp": "phoneapps.db",
            "E_browser": "continuity.db", "E_handoff": "continuity.db", "E_settings": "settings.db",
            "R01": "contacts.db", "R02": "contacts.db", "R03": "phone.db", "R04": "phone.db", "R05": "phone.db",
            "R06": "phone.db", "R07": "phone.db", "R08": "phone.db", "R09": "phone.db", "R10": "phone.db",
            "R11": "phoneapps.db"}
    for vid, v in g.views.items():
        v.basis += ev.missing_note(view_sources(vid))
        if v.db is None:
            v.db = DBOF.get(vid)
        if v.kind == "entity":
            v.kind = "table"
        elif v.kind in ("schema", "id-join"):
            v.kind = "in-db"
        elif v.kind in ("examiner", "time") or vid == "R24":
            v.kind = "cross"
        elif v.kind == "integrity":
            v.kind = "check"

    # ---------- provenance + map output
    add(View("P_copies", "Copies and hashes", "provenance", f"staged from {ev.src} to {ev.work}",
             ["file", "source_sha256", "copy_matches"], ev.verify))
    for vid, view in run_maps(ev, map_dirs or DEFAULT_MAP_DIRS).items():
        add(view)
    return g


# ---------------------------------------------------------------- SQLECmd map output (stdlib .smap reader)
def read_smap(path):
    """Minimal reader for the SQLECmd .smap layout (no YAML library needed)."""
    m, queries, cur, in_query, query_lines = {}, [], None, False, []
    with open(path, encoding="utf-8-sig") as f:
        lines = f.read().split("\n")

    def flush_query():
        nonlocal in_query, query_lines
        if in_query and cur is not None:
            cur["Query"] = "\n".join(l[16:] if l.startswith(" " * 16) else l.strip() for l in query_lines).strip()
        in_query, query_lines = False, []

    for line in lines:
        if in_query:
            if line.startswith(" " * 16) or not line.strip():
                query_lines.append(line)
                continue
            flush_query()
        if line.startswith("#") or not line.strip():
            continue
        if not line.startswith(" ") and ":" in line:
            k, v = line.split(":", 1)
            m[k.strip()] = v.strip()
        elif line.strip() == "-" and line.startswith("    -"):
            cur = {}
            queries.append(cur)
        elif line.startswith(" " * 8) and not line.startswith(" " * 9) and ":" in line and cur is not None:
            k, v = line.strip().split(":", 1)
            if k == "Query" and v.strip() == "|":
                in_query = True
            elif k != "BlobColumns":
                cur[k] = v.strip()
        elif line.strip().startswith("- BlobColumn:") and cur is not None:
            cur.setdefault("BlobColumns", []).append({"BlobColumn": line.split(":", 1)[1].strip()})
        elif line.startswith(" " * 14) and ":" in line and cur is not None and cur.get("BlobColumns"):
            k, v = line.strip().split(":", 1)
            cur["BlobColumns"][-1][k] = v.strip()
    flush_query()
    m["Queries"] = queries
    return m


def run_maps(ev, map_dirs):
    """Run every Phone Link map whose IdentifyQuery matches a staged database (as SQLECmd --hunt does)."""
    views = {}
    maps = []
    for d in map_dirs:
        if os.path.isdir(d):
            maps += [read_smap(os.path.join(d, f)) for f in sorted(os.listdir(d))
                     if f.endswith(".smap") and "YourPhone" in f]
    for db in P.DB_NAMES:
        c = ev.con(db)
        if c is None:
            continue
        for m in maps:
            try:
                got = c.execute(m["IdentifyQuery"]).fetchone()[0]
            except sqlite3.Error:
                continue
            if str(got).lower() != str(m["IdentifyValue"]).lower():
                continue
            for q in m["Queries"]:
                vid = f"M_{q['BaseFileName']}"
                blob_cols = {b["BlobColumn"] for b in q.get("BlobColumns", [])}
                try:
                    cur = c.execute(q["Query"])
                except sqlite3.Error as e:
                    views[vid] = View(vid, q["BaseFileName"], "map", f"{m.get('Description')} - ERROR {e}", ["Error"],
                                      [{"Error": str(e)}])
                    continue
                cols = [d[0] for d in cur.description]
                rows = []
                for r in cur.fetchall():
                    rows.append({k: (f"<{len(v)} bytes>" if isinstance(v, bytes) else v)
                                 for k, v in zip(cols, r)})
                views[vid] = View(vid, q["BaseFileName"], "map",
                                  f"SQLECmd map: {m.get('Description')} (blob columns shown as size: "
                                  f"{', '.join(sorted(blob_cols)) or 'none'})", cols, rows)
    return views
