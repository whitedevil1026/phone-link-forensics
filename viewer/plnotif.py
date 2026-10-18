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


def decode_flags(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return ""
    names = [n for bit, n in FLAGS if v & bit]
    rest = v & ~sum(b for b, _ in FLAGS)
    return f"{v} = " + (" | ".join(names) or "none") + (f" | +0x{rest:x}" if rest else "")


def decode_enum(v, table):
    if v is None or v == "":
        return ""
    try:
        return f"{v} ({table.get(int(v), 'unknown')})"
    except (TypeError, ValueError):
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
    for r in ev.rows(DB, "SELECT * FROM notifications ORDER BY post_time", "notifications"):
        raw.append(("live", r["id"], r, ""))
    recs, _ = plcarve.carve_db(ev.paths[DB], include_wal=True)
    toasts_raw = [("live", r) for r in ev.rows(DB, "SELECT * FROM notification_toastmappings",
                                              "notification_toastmappings")]
    reqs_raw = [("live", r) for r in ev.rows(DB, "SELECT * FROM phone_requests", "phone_requests")]
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
            row[col] = ("yes" if v else "no") if isinstance(v, bool) else v
        rows.append(row)
        g.time[e] = t_col or ts_seconds(o.get("postTime"))
        g.label[e] = f"{source} notification: {row['AppName'] or row['Package']} - {row['Title'] or ''}".strip()
        for i, m in enumerate(msgs):
            sender = senders[i] if i < len(senders) else ""
            me = ("notifmsg", f"{e[1]}:{i}")
            msg_rows.append({"_e": me, "Notification": g.label[e], "Source": source, "Position": i + 1,
                             "Sender": sender, "Message": m})
            g.label[me] = f"message {i + 1} from {sender or 'unknown'}"
            g.link("NR5", e, me, "This message is inside the notification (messages[] / senderNames[])")
        for a in actions:
            if not isinstance(a, dict):
                continue
            ae = ("notifact", f"{e[1]}:{a.get('actionIndex')}")
            act_rows.append({"_e": ae, "Notification": g.label[e], "Source": source,
                             "ActionIndex": a.get("actionIndex"), "ActionName": a.get("actionName"),
                             "InlineReply": "yes" if a.get("isActionInlineReply") else "no"})
            g.label[ae] = f"button '{a.get('actionName')}'"
            g.link("NR6", e, ae, "This button was on the notification (notificationActions[])")

    def add(vid, title, scope, basis, cols, rs, entity=True):
        v = View(vid, title, scope, basis, cols, rs)
        v.db = DB
        g.add_view(v, "_e" if entity else None)
        return vid

    vids = [add("N_notifications", "notifications - live and deleted, JSON unpacked", "table",
                "notifications table + rows carved from free space; every json field as a column", COLS, rows),
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
                    "notification_toastmappings, live + carved", ["Source", "ID", "NotificationID", "ToastID"],
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
                    "phone_requests, live + carved; times by magnitude (epoch unconfirmed)",
                    ["Source", "UniqueID", "RequestID", "ExternalID1", "ExternalID2", "ContentType", "Status", "State",
                     "AddedUTC", "StartedUTC", "FinishedUTC", "ExpiresUTC", "Payload"], req_rows))

    # ---------- relations inside the notifications table
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
        for i, m in enumerate(members):
            for other in members[i + 1:]:
                g.link("NR1", m["_e"], other["_e"], "Same notification group (groupKey)")
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
        for m in members:
            for other in members:
                if other is not m:
                    g.link("NR2", m["_e"], other["_e"], f"Same conversation '{conv}' in the same app")
            rel.append({"_e": m["_e"], "App": m["AppName"] or pkg, "Conversation": conv, "Row": g.label[m["_e"]],
                        "Source": m["Source"], "Messages": m["Messages"], "PostTimeUTC": m["PostTimeUTC"]})
    vids.append(add("NR2", "Same conversation (app + conversationTitle)", "in-table",
                    "rows with the same package and json conversationTitle - one chat, several notifications",
                    ["App", "Conversation", "Row", "Source", "Messages", "PostTimeUTC"], rel, entity=False))

    rel = []
    for key, members in groups(lambda r: r["Key"]).items():
        members = sorted(members, key=lambda r: (r["JSONPostTimeUTC"] or "", r["EventCount"] or 0))
        for i, m in enumerate(members):
            for other in members:
                if other is not m:
                    g.link("NR3", m["_e"], other["_e"], "Same Android notification (key) - another version of it")
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
        except (TypeError, ValueError):
            pass
        if r["PostTimeUTC"] and r["JSONPostTimeUTC"]:
            checks.append(("post_time column = json postTime (to the second)", r["PostTimeUTC"] == r["JSONPostTimeUTC"]))
        if r["Messages"] or r["Senders"]:
            checks.append(("one sender per message", len(o.get("senderNames") or []) == len(o.get("messages") or [])))
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
    keys = {r["Key"]: r for r in rows if r["Key"]}
    for q in req_rows:
        for col in ("ExternalID1", "ExternalID2"):
            v = q[col]
            hits = by_nid.get(v, []) + ([keys[v]] if v in keys else [])
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
