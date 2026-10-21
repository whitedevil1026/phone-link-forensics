"""
plnotif.py - notifications.db: every row (live and carved), the JSON unpacked,
and the relations inside the database. Used by plrelate / plmapview.

What is in notifications.db
  notifications               one row per phone notification mirrored to the PC; the
                              whole Android notification is packed into the json column
  notification_toastmappings  notification_id -> the Windows toast that showed it
  phone_requests              requests the PC queued for the phone, with lifecycle times

Phone Link deletes a notification when it is dismissed, so the live table is often
empty. Deleted rows survive in free pages (secure_delete is off), so the table is
also carved with plcarve and the recovered rows are shown with Source = deleted.

Decodes marked [published] follow the Android API reference (Notification.flags,
NotificationManager importance, Notification priority, StatusBarNotification.getKey).
Everything else is shown raw.

json isGroup is NOT the group-summary flag: on real data it was true exactly when the
notification's groupKey differed from its own key (4 of 4 rows; false on the 1 row whose
groupKey was its own key), i.e. "belongs to an app-defined group" [measured, n=5 - inferred].
The group summary is identified by Notification.FLAG_GROUP_SUMMARY [published].
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plcarve  # noqa: E402

DB = "notifications.db"

# [published] android.app.Notification FLAG_* constants
FLAGS = [(0x1, "SHOW_LIGHTS"), (0x2, "ONGOING_EVENT"), (0x4, "INSISTENT"), (0x8, "ONLY_ALERT_ONCE"),
         (0x10, "AUTO_CANCEL"), (0x20, "NO_CLEAR"), (0x40, "FOREGROUND_SERVICE"), (0x80, "HIGH_PRIORITY"),
         (0x100, "LOCAL_ONLY"), (0x200, "GROUP_SUMMARY"), (0x1000, "BUBBLE")]
# [published] NotificationManager.IMPORTANCE_* and Notification.PRIORITY_*
IMPORTANCE = {-1000: "UNSPECIFIED", 0: "NONE", 1: "MIN", 2: "LOW", 3: "DEFAULT", 4: "HIGH", 5: "MAX"}
PRIORITY = {-2: "MIN", -1: "LOW", 0: "DEFAULT", 1: "HIGH", 2: "MAX"}

JSON_COLS = [  # (output column, json key)
    ("AppName", "appName"), ("Title", "title"), ("Text", "text"), ("BigTitle", "bigTitle"), ("BigText", "bigText"),
    ("SubText", "subText"), ("TickerText", "tickerText"), ("ConversationTitle", "conversationTitle"),
    ("Category", "category"), ("Template", "template"), ("GroupKey", "groupKey"), ("Key", "key"), ("Tag", "tag"),
    ("AndroidID", "id"), ("EventCount", "eventCount"), ("IsGroup", "isGroup"), ("IsOngoing", "isOngoing"),
    ("IsClearable", "isClearable"), ("ShowWhen", "showWhen"), ("Version", "version"), ("Platform", "platform"),
    ("NotificationClass", "notificationClass"), ("FeatureFlags", "featureFlags"), ("JSONPackage", "packageName"),
]
COLS = (["Source", "RowID", "NotificationID", "Package", "PostTimeUTC", "JSONPostTimeUTC", "JSONTimestampUTC"]
        + [c for c, _ in JSON_COLS[:9]]
        + ["Messages", "Senders", "Actions", "InlineReply", "Importance", "Priority", "Flags"]
        + [c for c, _ in JSON_COLS[9:]]
        + ["KeyUser", "KeyPackage", "KeyID", "KeyTag", "KeyUID", "State", "AnonymousID", "JSONStatus", "CarvedFrom"])

GROUP_PAIRS_MAX = 50   # see link_group()


def cell(v):
    """A value for a table cell: yes/no for booleans, JSON text for a list or object (so it can be a group key)."""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return v


def count_key(v):
    """Sort key for a json count that may be a number, text or NULL: numbers first, text after."""
    try:
        return (0, float(v or 0), "")
    except (TypeError, ValueError, OverflowError):
        return (1, 0.0, str(v))


def decode_flags(v):
    try:
        v = int(v)
    except (TypeError, ValueError, OverflowError):
        return ""
    names = [n for bit, n in FLAGS if v & bit]
    rest = v & ~sum(b for b, _ in FLAGS)
    return f"{v} = " + (" | ".join(names) or "none") + (f" | +0x{rest:x}" if rest else "")


def decode_enum(v, table):
    if v is None or v == "":
        return ""
    try:
        return f"{v} ({table.get(int(v), 'unknown')})"
    except (TypeError, ValueError, OverflowError):
        return str(v)


def decode_key(key):
    """[published] StatusBarNotification key = userId|package|id|tag|uid."""
    parts = str(key or "").split("|")
    if len(parts) != 5:
        return ["", "", "", "", ""]
    return [p if p != "null" else "" for p in parts]


def parse_json(text):
    """(dict, status). Carved JSON can be cut off; keep whatever parses."""
    if not text:
        return {}, "empty"
    try:
        o = json.loads(text)
        return (o, "parsed") if isinstance(o, dict) else ({}, "not an object")
    except (ValueError, TypeError):
        return {}, "damaged (cut off or overwritten) - raw text kept in the database"


def build(ev, g, View, ts_seconds, iso):
    """Add the notifications.db views and relations to graph g. Returns the list of view ids."""
    if DB not in ev.paths:
        return []
    raw = []  # (source, rowid, dict of columns, carved_from)
    for r in ev.table(DB, "notifications", ("id", "notification_id", "package_name", "json", "post_time", "state",
                                            "anonymous_id"), order=("post_time",)):
        raw.append(("live", r["id"], r, ""))
    try:
        recs, stats = plcarve.carve_db(ev.paths[DB], include_wal=True)
        errors = list(stats.get("errors") or [])
    except Exception as e:  # noqa: BLE001 - a carving failure must show, not hide the live rows
        recs, errors = [], [f"{type(e).__name__}: {e}"]
    carve_note = ""
    if errors:   # the carver reports what it could not read; deleted rows may then be missing
        carve_note = (" - CARVING ERRORS (deleted rows may be missing): " + "; ".join(errors[:5])
                      + (f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""))
    toasts_raw = [("live", r) for r in ev.table(DB, "notification_toastmappings", ("id", "notification_id", "toast_id"))]
    reqs_raw = [("live", r) for r in ev.table(
        DB, "phone_requests", ("unique_id", "request_id", "request_external_id1", "request_external_id2",
                               "request_payload", "request_content_type", "request_status", "request_state",
                               "request_time_added", "request_time_started", "request_time_finished",
                               "request_expiration_time"))]
    for c in recs:
        where = f"{c.get('source_file')} page {c.get('page')} {c.get('region')} @{c.get('offset')}"
        if c["table"] == "notifications" and c.get("status") != "also_live":
            raw.append(("deleted", c.get("rowid"), c, where))
        elif c["table"] == "notification_toastmappings" and c.get("status") != "also_live":
            toasts_raw.append(("deleted", c))
        elif c["table"] == "phone_requests" and c.get("status") != "also_live":
            reqs_raw.append(("deleted", c))

    rows, msg_rows, act_rows = [], [], []
    for n, (source, rowid, r, where) in enumerate(raw):
        o, status = parse_json(r.get("json"))
        e = ("notif", f"{source}:{rowid if source == 'live' else n}")
        msgs = o.get("messages") if isinstance(o.get("messages"), list) else []
        senders = o.get("senderNames") if isinstance(o.get("senderNames"), list) else []
        actions = o.get("notificationActions") if isinstance(o.get("notificationActions"), list) else []
        k = decode_key(o.get("key"))
        t_col = ts_seconds(r.get("post_time"))
        row = {"_e": e, "_json": o, "Source": source, "RowID": rowid, "NotificationID": r.get("notification_id"),
               "Package": r.get("package_name"), "PostTimeUTC": iso(t_col),
               "JSONPostTimeUTC": iso(ts_seconds(o.get("postTime"))),
               "JSONTimestampUTC": iso(ts_seconds(o.get("timestamp"))),
               "Messages": len(msgs), "Senders": " | ".join(str(s) for s in senders if s),
               "Actions": " | ".join(str(a.get("actionName", "")) for a in actions if isinstance(a, dict)),
               "InlineReply": "yes" if any(isinstance(a, dict) and a.get("isActionInlineReply") for a in actions)
               else ("no" if actions else ""),
               "Importance": decode_enum(o.get("importance"), IMPORTANCE),
               "Priority": decode_enum(o.get("priority"), PRIORITY), "Flags": decode_flags(o.get("flags")),
               "KeyUser": k[0], "KeyPackage": k[1], "KeyID": k[2], "KeyTag": k[3], "KeyUID": k[4],
               "State": r.get("state"), "AnonymousID": r.get("anonymous_id"), "JSONStatus": status,
               "CarvedFrom": where}
        for col, key in JSON_COLS:
            v = o.get(key)
            row[col] = cell(v)
        rows.append(row)
        g.time[e] = t_col or ts_seconds(o.get("postTime"))
        g.label[e] = f"{source} notification: {row['AppName'] or row['Package']} - {row['Title'] or ''}".strip()
        for i, m in enumerate(msgs):
            sender = senders[i] if i < len(senders) else ""
            me = ("notifmsg", f"{e[1]}:{i}")
            msg_rows.append({"_e": me, "Notification": g.label[e], "Source": source, "Position": i + 1,
                             "Sender": cell(sender), "Message": cell(m)})
            g.label[me] = f"message {i + 1} from {sender or 'unknown'}"
            g.link("NR5", e, me, "This message is inside the notification (messages[] / senderNames[])")
        for a in actions:
            if not isinstance(a, dict):
                continue
            ae = ("notifact", f"{e[1]}:{a.get('actionIndex')}")
            act_rows.append({"_e": ae, "Notification": g.label[e], "Source": source,
                             "ActionIndex": cell(a.get("actionIndex")), "ActionName": cell(a.get("actionName")),
                             "InlineReply": "yes" if a.get("isActionInlineReply") else "no"})
            g.label[ae] = f"button '{a.get('actionName')}'"
            g.link("NR6", e, ae, "This button was on the notification (notificationActions[])")

    def add(vid, title, scope, basis, cols, rs, entity=True):
        v = View(vid, title, scope, basis, cols, rs)
        v.db = DB
        g.add_view(v, "_e" if entity else None)
        return vid

    vids = [add("N_notifications", "notifications - live and deleted, JSON unpacked", "table",
                "notifications table + rows carved from free space; every json field as a column" + carve_note,
                COLS, rows),
            add("N_messages", "notifications - chat messages inside each notification", "table",
                "messages[] and senderNames[] from the json column, one row per message",
                ["Notification", "Source", "Position", "Sender", "Message"], msg_rows),
            add("N_actions", "notifications - buttons on each notification", "table",
                "notificationActions[] from the json column; InlineReply = could be answered from the PC",
                ["Notification", "Source", "ActionIndex", "ActionName", "InlineReply"], act_rows)]

    toast_rows = []
    for i, (source, t) in enumerate(toasts_raw):
        te = ("toast", f"{source}:{t.get('id') if source == 'live' else i}")
        toast_rows.append({"_e": te, "Source": source, "ID": t.get("id"), "NotificationID": t.get("notification_id"),
                           "ToastID": t.get("toast_id")})
        g.label[te] = f"{source} Windows toast {t.get('toast_id')}"
    vids.append(add("N_toasts", "notification_toastmappings", "table",
                    "notification_toastmappings, live + carved" + carve_note,
                    ["Source", "ID", "NotificationID", "ToastID"],
                    toast_rows))
    req_rows = []
    for i, (source, q) in enumerate(reqs_raw):
        qe = ("request", f"{source}:{q.get('unique_id') if source == 'live' else i}")
        req_rows.append({"_e": qe, "Source": source, "UniqueID": q.get("unique_id"), "RequestID": q.get("request_id"),
                         "ExternalID1": q.get("request_external_id1"), "ExternalID2": q.get("request_external_id2"),
                         "ContentType": q.get("request_content_type"), "Status": q.get("request_status"),
                         "State": q.get("request_state"), "AddedUTC": iso(ts_seconds(q.get("request_time_added"))),
                         "StartedUTC": iso(ts_seconds(q.get("request_time_started"))),
                         "FinishedUTC": iso(ts_seconds(q.get("request_time_finished"))),
                         "ExpiresUTC": iso(ts_seconds(q.get("request_expiration_time"))),
                         "Payload": q.get("request_payload")})
        g.label[qe] = f"{source} phone request {q.get('request_content_type') or ''} {q.get('request_id') or ''}"
    vids.append(add("N_requests", "phone_requests", "table",
                    "phone_requests, live + carved; times by magnitude (epoch unconfirmed)" + carve_note,
                    ["Source", "UniqueID", "RequestID", "ExternalID1", "ExternalID2", "ContentType", "Status", "State",
                     "AddedUTC", "StartedUTC", "FinishedUTC", "ExpiresUTC", "Payload"], req_rows))

    # ---------- relations inside the notifications table
    def link_group(rid, members, note):
        """Every pair in a group of up to GROUP_PAIRS_MAX rows. A bigger group (one chat app can file hundreds
        of notifications under one groupKey) would mean n*n/2 links, so its rows are chained in time order."""
        if len(members) <= GROUP_PAIRS_MAX:
            for i, m in enumerate(members):
                for other in members[i + 1:]:
                    g.link(rid, m["_e"], other["_e"], note)
            return
        chain = sorted(members, key=lambda r: (r["JSONPostTimeUTC"] or r["PostTimeUTC"] or "",
                                               count_key(r["EventCount"])))
        for m, nxt in zip(chain, chain[1:]):
            g.link(rid, m["_e"], nxt["_e"], f"{note} - group of {len(members)}, linked to the next one in time")

    def groups(keyfn):
        out = {}
        for r in rows:
            k = keyfn(r)
            if k:
                out.setdefault(k, []).append(r)
        return {k: v for k, v in out.items() if len(v) > 1}

    rel = []
    for gk, members in groups(lambda r: r["GroupKey"]).items():
        summary = [m for m in members if "GROUP_SUMMARY" in (m["Flags"] or "")]
        link_group("NR1", members, "Same notification group (groupKey)")
        for m in members:
            rel.append({"_e": m["_e"], "GroupKey": gk, "Row": g.label[m["_e"]], "Source": m["Source"],
                        "Role": "summary" if m in summary else "member", "PostTimeUTC": m["PostTimeUTC"]})
    vids.append(add("NR1", "Same notification group (groupKey)", "in-table",
                    "rows of the notifications table with the same json groupKey; the row whose flags include "
                    "GROUP_SUMMARY [published] is the summary Android shows above the others",
                    ["GroupKey", "Row", "Source", "Role", "PostTimeUTC"],
                    rel, entity=False))

    rel = []
    for (pkg, conv), members in groups(lambda r: (r["Package"], r["ConversationTitle"])
                                       if r["ConversationTitle"] else None).items():
        link_group("NR2", members, f"Same conversation '{conv}' in the same app")
        for m in members:
            rel.append({"_e": m["_e"], "App": m["AppName"] or pkg, "Conversation": conv, "Row": g.label[m["_e"]],
                        "Source": m["Source"], "Messages": m["Messages"], "PostTimeUTC": m["PostTimeUTC"]})
    vids.append(add("NR2", "Same conversation (app + conversationTitle)", "in-table",
                    "rows with the same package and json conversationTitle - one chat, several notifications",
                    ["App", "Conversation", "Row", "Source", "Messages", "PostTimeUTC"], rel, entity=False))

    rel = []
    for key, members in groups(lambda r: r["Key"]).items():
        members = sorted(members, key=lambda r: (r["JSONPostTimeUTC"] or "", count_key(r["EventCount"])))
        link_group("NR3", members, "Same Android notification (key) - another version of it")
        for i, m in enumerate(members):
            rel.append({"_e": m["_e"], "Key": key, "Version": i + 1, "Row": g.label[m["_e"]], "Source": m["Source"],
                        "EventCount": m["EventCount"], "JSONPostTimeUTC": m["JSONPostTimeUTC"],
                        "Text": m["Text"]})
    vids.append(add("NR3", "Same notification, several versions (key)", "in-table",
                    "rows with the same json key (userId|package|id|tag|uid) - the app updated one notification, "
                    "e.g. a new message arrived; deleted rows can be earlier versions",
                    ["Key", "Version", "Row", "Source", "EventCount", "JSONPostTimeUTC", "Text"], rel, entity=False))

    rel = []
    for r in rows:
        o = r["_json"]
        if not o:
            rel.append({"_e": r["_e"], "Row": g.label[r["_e"]], "Check": "json readable",
                        "Result": r["JSONStatus"]})
            continue
        checks = [("package_name column = json packageName", r["Package"] == o.get("packageName")),
                  ("package_name = package in json key", not r["KeyPackage"] or r["Package"] == r["KeyPackage"])]
        try:
            fl = int(o.get("flags"))
            if "isOngoing" in o:
                checks.append(("isOngoing = flag ONGOING_EVENT", bool(fl & 0x2) == bool(o["isOngoing"])))
            if "isGroup" in o and o.get("groupKey") and o.get("key"):
                checks.append(("isGroup = belongs to an app group (groupKey is not its own key) [inferred]",
                               bool(o["isGroup"]) == (o["groupKey"] != o["key"])))
        except (TypeError, ValueError, OverflowError):
            pass
        if r["PostTimeUTC"] and r["JSONPostTimeUTC"]:
            checks.append(("post_time column = json postTime (to the second)", r["PostTimeUTC"] == r["JSONPostTimeUTC"]))
        if r["Messages"] or r["Senders"]:
            n_msgs, n_senders = (len(o[k]) if isinstance(o.get(k), list) else 0 for k in ("messages", "senderNames"))
            checks.append(("one sender per message", n_senders == n_msgs))
        for name, ok in checks:
            rel.append({"_e": r["_e"], "Row": g.label[r["_e"]], "Check": name,
                        "Result": "agrees" if ok else "DISAGREES"})
    vids.append(add("NR4", "Columns vs packed JSON - consistency checks", "in-table",
                    "each row's plain columns compared with its own json, and json fields with each other "
                    "(flags decoded [published]); a disagreement is worth explaining", ["Row", "Check", "Result"],
                    rel, entity=False))
    vids.append(add("NR5", "Notification -> its chat messages", "in-table",
                    "messages[i] was sent by senderNames[i] inside the same json", ["Notification", "Position",
                                                                                     "Sender", "Message"],
                    msg_rows, entity=False))
    vids.append(add("NR6", "Notification -> its buttons", "in-table", "notificationActions[] inside the same json",
                    ["Notification", "ActionName", "InlineReply"], act_rows, entity=False))

    # ---------- relations between the tables of notifications.db
    by_nid = {}
    for r in rows:
        if r["NotificationID"]:
            by_nid.setdefault(r["NotificationID"], []).append(r)
    rel = []
    for t in toast_rows:
        hits = by_nid.get(t["NotificationID"], [])
        for h in hits:
            g.link("ND1", h["_e"], t["_e"], "Windows showed this notification as toast " + str(t["ToastID"]))
        rel.append({"_e": t["_e"], "ToastID": t["ToastID"], "NotificationID": t["NotificationID"], "Source": t["Source"],
                    "Notification": " | ".join(g.label[h["_e"]] for h in hits) or "(no notification row - deleted"
                                                                                  " and not recovered)"})
    vids.append(add("ND1", "Notification -> Windows toast", "in-db",
                    "notification_toastmappings.notification_id = notifications.notification_id", ["ToastID",
                                                                                                  "NotificationID",
                                                                                                  "Source",
                                                                                                  "Notification"],
                    rel, entity=False))
    rel = []
    keys = {}   # json key -> every row with it (a live row and its deleted earlier versions share one key)
    for r in rows:
        if r["Key"]:
            keys.setdefault(r["Key"], []).append(r)
    for q in req_rows:
        for col in ("ExternalID1", "ExternalID2"):
            v = q[col]
            hits = by_nid.get(v, []) + [k for k in keys.get(v, []) if k not in by_nid.get(v, [])]
            for h in hits:
                g.link("ND2", h["_e"], q["_e"], f"A phone request names this notification in {col}")
                rel.append({"_e": q["_e"], "RequestID": q["RequestID"], "MatchedColumn": col, "ContentType": q["ContentType"],
                            "Notification": g.label[h["_e"]], "AddedUTC": q["AddedUTC"]})
    vids.append(add("ND2", "Phone request -> notification (only where the IDs match)", "in-db",
                    "phone_requests.request_external_id1/2 equal to a notification_id or json key. The meaning of"
                    " these columns is not documented: the link is shown only when the values are identical",
                    ["RequestID", "MatchedColumn", "ContentType", "Notification", "AddedUTC"], rel, entity=False))
    rel = []
    for nid, members in by_nid.items():
        live = [m for m in members if m["Source"] == "live"]
        dead = [m for m in members if m["Source"] == "deleted"]
        for d in dead:
            for lv in live:
                g.link("ND3", d["_e"], lv["_e"], "A deleted copy of this notification (same notification_id)")
            rel.append({"_e": d["_e"], "NotificationID": nid, "Deleted": g.label[d["_e"]],
                        "LiveCopy": " | ".join(g.label[x["_e"]] for x in live) or "(none - only the deleted copy)",
                        "DeletedFrom": d["CarvedFrom"]})
    vids.append(add("ND3", "Deleted rows and their live copy", "in-db",
                    "carved notification rows matched to live rows by notification_id", ["NotificationID", "Deleted",
                                                                                          "LiveCopy", "DeletedFrom"],
                    rel, entity=False))
    return vids
