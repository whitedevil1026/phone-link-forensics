"""
pldemo.py - build a synthetic Phone Link device folder with KNOWN relations.

Everything is invented (555-01xx numbers, example.com, com.example packages,
dates in 2030). Used by the tests and by `plmapview.py --demo`, so the viewer
can be shown without real data.

    python pldemo.py <empty folder>
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

BASE = datetime(2030, 1, 1, 12, 0, 0)
FT_EPOCH = datetime(1601, 1, 1)


def ft(t):
    return int((t - FT_EPOCH).total_seconds()) * 10_000_000


def ms(t):
    return int((t - datetime(1970, 1, 1)).total_seconds()) * 1000


def at(minutes):
    return BASE + timedelta(minutes=minutes)


SEQ = ("CREATE TABLE content_sequence (content_sequence_id INTEGER PRIMARY KEY AUTOINCREMENT, content_type TEXT UNIQUE"
       " NOT NULL, sequence INTEGER NOT NULL DEFAULT -1, expected INTEGER NOT NULL DEFAULT -1)")

DDL = {
    "contacts.db": [
        "CREATE TABLE contact (contact_id INTEGER PRIMARY KEY, display_name TEXT, nickname TEXT, last_updated_time"
        " INTEGER, thumbnail BLOB, company TEXT, job_title TEXT, notes TEXT, name_prefix TEXT, name_suffix TEXT,"
        " given_name TEXT, middle_name TEXT, family_name TEXT, checksum INTEGER DEFAULT 0)",
        "CREATE TABLE phonenumber (phone_number_id INTEGER PRIMARY KEY, contact_id INTEGER, phone_number TEXT,"
        " display_phone_number TEXT, phone_number_type INTEGER DEFAULT 0, label TEXT, checksum INTEGER DEFAULT 0)",
        "CREATE TABLE emailaddress (email_address_id INTEGER PRIMARY KEY, contact_id INTEGER, type INTEGER DEFAULT 0,"
        " label TEXT, address TEXT, checksum INTEGER DEFAULT 0)",
        "CREATE TABLE postaladdress (postal_address_id INTEGER PRIMARY KEY, contact_id INTEGER, type INTEGER DEFAULT 0,"
        " label TEXT, street TEXT, city TEXT, region TEXT, postal_code TEXT, country_code TEXT,"
        " sub_administrative_area TEXT, sub_locality TEXT, display_address TEXT, checksum INTEGER DEFAULT 0)",
        "CREATE TABLE contactdate (contact_date_id INTEGER PRIMARY KEY, contact_id INTEGER, date_type INTEGER DEFAULT 0,"
        " label TEXT, display_date TEXT, checksum INTEGER DEFAULT 0)",
        "CREATE TABLE contacturl (contact_url_id INTEGER PRIMARY KEY, contact_id INTEGER, type INTEGER DEFAULT 0,"
        " label TEXT, url_address TEXT, checksum INTEGER DEFAULT 0)",
        "CREATE TABLE contact_index (id INTEGER PRIMARY KEY, contact_id INTEGER, foreign_id INTEGER,"
        " foreign_table_name TEXT)",
    ],
    "calling.db": [
        "CREATE TABLE call_history (call_id INTEGER PRIMARY KEY, phone_number TEXT NOT NULL, duration INTEGER NOT NULL"
        " DEFAULT 0, call_type INTEGER NOT NULL, start_time INTEGER NOT NULL, is_read INTEGER NOT NULL DEFAULT 0,"
        " phone_account_id TEXT, last_updated_time INTEGER NOT NULL DEFAULT 0)",
    ],
    "phone.db": [
        "CREATE TABLE conversation (thread_id INTEGER PRIMARY KEY, recipient_list TEXT, timestamp INTEGER, msg_count"
        " INTEGER, unread_count INTEGER, has_rcs INTEGER, checksum INTEGER, phone_unread_count INTEGER)",
        "CREATE TABLE message (message_id INTEGER PRIMARY KEY, thread_id INTEGER, status INTEGER, from_address TEXT,"
        " type INTEGER, subject TEXT, body TEXT, timestamp INTEGER, pc_status INTEGER, subscription_id INTEGER,"
        " checksum INTEGER, group_id INTEGER, to_address TEXT)",
        "CREATE TABLE participant (id INTEGER PRIMARY KEY, thread_id INTEGER, send_address TEXT,"
        " normalized_address TEXT)",
        "CREATE TABLE mms (message_id INTEGER PRIMARY KEY, thread_id INTEGER, status INTEGER, type INTEGER,"
        " subscription_id INTEGER, subject TEXT, charset INTEGER, timestamp INTEGER, pc_status INTEGER,"
        " from_address TEXT, checksum INTEGER, to_addresses TEXT)",
        "CREATE TABLE mms_part (part_id INTEGER PRIMARY KEY, message_id INTEGER, sequence_num INTEGER, content_id TEXT,"
        " content_type TEXT, text TEXT, name TEXT, charset INTEGER, blob BLOB, size INTEGER)",
        "CREATE TABLE rcs_conversation (thread_id INTEGER PRIMARY KEY, fallback_id INTEGER, recipient_list TEXT,"
        " timestamp INTEGER, msg_count INTEGER, unread_count INTEGER, type INTEGER, checksum INTEGER,"
        " phone_unread_count INTEGER)",
        "CREATE TABLE rcs_chat (message_id INTEGER PRIMARY KEY, thread_id INTEGER, content_type TEXT, status INTEGER,"
        " from_address TEXT, type INTEGER, body TEXT, timestamp INTEGER, pc_status INTEGER, subscription_id INTEGER,"
        " message_type INTEGER, session_id TEXT, is_user_action_required INTEGER, checksum INTEGER)",
        "CREATE TABLE rcs_filetransfer (message_id INTEGER PRIMARY KEY, thread_id INTEGER, content_type TEXT, status"
        " INTEGER, from_address TEXT, type INTEGER, timestamp INTEGER, pc_status INTEGER, subscription_id INTEGER,"
        " message_type INTEGER, session_id TEXT, name TEXT, size INTEGER, blob BLOB, is_user_action_required INTEGER,"
        " checksum INTEGER)",
        "CREATE TABLE conversation_attribute (thread_id INTEGER, rcs_thread_id INTEGER DEFAULT -1, position INTEGER"
        " DEFAULT -1, is_muted INTEGER DEFAULT -1, is_hidden INTEGER DEFAULT -1)",
        "CREATE TABLE sending_message (id INTEGER PRIMARY KEY, sending_message_id TEXT, result_code INTEGER,"
        " message_id INTEGER, message_kind INTEGER, thread_id INTEGER)",
        "CREATE TABLE subscription (subscription_id INTEGER PRIMARY KEY, sim_slot_index INTEGER, country_iso TEXT,"
        " name TEXT, is_roaming INTEGER, number TEXT, is_mms_enabled INTEGER, is_audio_attachment_allowed INTEGER,"
        " is_multipart_sms_enabled INTEGER, is_group_mms_enabled INTEGER,"
        " should_send_multipart_sms_as_separate_messages INTEGER, max_message_size INTEGER, recipient_limit INTEGER,"
        " max_image_height INTEGER, max_image_width INTEGER, sms_multipart_to_mms_text_threshold INTEGER,"
        " sms_to_mms_text_length_threshold INTEGER, max_message_text_length INTEGER, max_subject_length INTEGER,"
        " is_default_data_subscription INTEGER, is_default_sms_subscription INTEGER, is_default_subscription INTEGER,"
        " is_default_voice_subscription INTEGER, is_rcs_supported INTEGER, max_rcs_message_size INTEGER,"
        " max_rcs_file_size INTEGER, checksum INTEGER)",
        "CREATE TABLE sync (sync_timestamp INTEGER, table_data_id INTEGER, last_updated_time INTEGER)",
    ],
    "photos.db": [
        "CREATE TABLE media (id INTEGER PRIMARY KEY, name TEXT NOT NULL, last_updated_time INTEGER NOT NULL, taken_time"
        " INTEGER NOT NULL, orientation TEXT, last_seen_time INTEGER NOT NULL DEFAULT 0, mime_type TEXT NOT NULL,"
        " height INTEGER NOT NULL, width INTEGER NOT NULL, size INTEGER NOT NULL, uri TEXT NOT NULL, thumbnail BLOB,"
        " media BLOB, checksum INTEGER NOT NULL DEFAULT 0)",
    ],
    "phoneapps.db": [
        "CREATE TABLE phone_apps (app_id INTEGER PRIMARY KEY AUTOINCREMENT, app_name TEXT NOT NULL, package_name TEXT"
        " NOT NULL UNIQUE, version TEXT NOT NULL, etag INTEGER NOT NULL DEFAULT 0, favorite_rank INTEGER NOT NULL"
        " DEFAULT -1, blob BLOB, last_updated_time INTEGER NOT NULL DEFAULT 0, checksum INTEGER NOT NULL DEFAULT 0,"
        " phoneapp_web_url TEXT)",
        "CREATE TABLE recent_apps (app_id INTEGER PRIMARY KEY AUTOINCREMENT, package_name TEXT NOT NULL, app_name TEXT"
        " NOT NULL, checksum INTEGER NOT NULL DEFAULT 0, recent_app_rank INTEGER NOT NULL DEFAULT 0, intent_action"
        " TEXT, task_id INTEGER NOT NULL, blob BLOB, web_uri TEXT, desktop_uri TEXT)",
        "CREATE TABLE pcapp_data (id INTEGER PRIMARY KEY AUTOINCREMENT, package_name TEXT NOT NULL UNIQUE, is_installed"
        " INTEGER NOT NULL DEFAULT 0, platformapp_url TEXT, platformapp_id TEXT, last_fetched INTEGER NOT NULL DEFAULT"
        " 0, platformapp_package TEXT, FOREIGN KEY (package_name) REFERENCES phone_apps (package_name))",
    ],
    "notifications.db": [
        "CREATE TABLE notifications (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, notification_id TEXT NOT NULL"
        " UNIQUE, package_name TEXT NOT NULL, json TEXT NOT NULL, post_time INTEGER NOT NULL, state INTEGER NOT NULL"
        " DEFAULT 0, anonymous_id TEXT)",
        "CREATE TABLE notification_toastmappings (id INTEGER PRIMARY KEY AUTOINCREMENT, notification_id TEXT NOT NULL,"
        " toast_id TEXT NOT NULL)",
        "CREATE TABLE phone_requests (unique_id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL UNIQUE,"
        " request_external_id1 TEXT NOT NULL, request_external_id2 TEXT, request_payload TEXT NOT NULL,"
        " request_content_type TEXT NOT NULL, request_status INTEGER, request_time_added INTEGER,"
        " request_time_started INTEGER, request_time_finished INTEGER, request_expiration_time INTEGER,"
        " request_state INTEGER)",
    ],
    "continuity.db": [
        "CREATE TABLE browser_history (browser_history_id INTEGER PRIMARY KEY AUTOINCREMENT, package_name TEXT NOT NULL,"
        " browser_history_title TEXT NOT NULL, checksum INTEGER NOT NULL DEFAULT 0, browser_history_rank INTEGER NOT"
        " NULL DEFAULT 0, task_id INTEGER NOT NULL, favicon BLOB, web_uri TEXT, source TEXT)",
        "CREATE TABLE continue_app_context (id INTEGER PRIMARY KEY AUTOINCREMENT, context_id TEXT UNIQUE NOT NULL,"
        " task_id INTEGER NOT NULL DEFAULT 0, type TEXT NOT NULL, team_id TEXT, intent_uri TEXT, app_id TEXT NOT NULL,"
        " title TEXT, web_link TEXT, preview BLOB, extras TEXT, create_time INTEGER NOT NULL DEFAULT 0,"
        " last_updated_time INTEGER NOT NULL DEFAULT 0, life_time INTEGER NOT NULL DEFAULT -1)",
    ],
    "deviceData.db": [
        "CREATE TABLE wallpaper (wallpaper_type INTEGER PRIMARY KEY NOT NULL, wallpaper_id INTEGER NOT NULL, blob BLOB)",
    ],
    "settings.db": [
        "CREATE TABLE settings (setting_group_id TEXT NOT NULL, setting_key TEXT NOT NULL, setting_type INTEGER NOT"
        " NULL, setting_value TEXT NOT NULL, setting_mute_expiration_timestamp INTEGER NOT NULL DEFAULT 0, PRIMARY KEY"
        " (setting_group_id, setting_key))",
        "CREATE TABLE phone_apps (app_id INTEGER PRIMARY KEY AUTOINCREMENT, app_name TEXT NOT NULL, package_name TEXT"
        " NOT NULL UNIQUE, version TEXT NOT NULL, etag INTEGER NOT NULL, favorite_rank INTEGER, blob BLOB,"
        " last_updated_time INTEGER, checksum INTEGER NOT NULL DEFAULT 0)",
    ],
}

ALICE, BOB, SHARED, UNKNOWN, FRANK = "+1-555-0101", "+1-555-0102", "+1-555-0103", "+1-555-0177", "+1-555-0109"

ROWS = {
    "contacts.db": {
        "contact": [(1, "Alice Example"), (2, "Bob Example"), (3, "Carol Example"), (4, "Dave Example"),
                    (5, "Erin Example"), (6, "Frank Example")],
        "phonenumber": [(1, 1, ALICE, ALICE), (2, 2, BOB, BOB), (3, 3, SHARED, SHARED), (4, 4, SHARED, SHARED),
                        (5, 6, FRANK, FRANK), (6, 99, "+1-555-0199", "+1-555-0199")],   # 6: orphan detail
        "emailaddress": [(1, 5, "erin@example.com")],
        "contact_index": [(1, 1, 7, "phonenumber")],                                       # deleted detail of Alice
        "content_sequence": [("contact", 6, 6)],
    },
    "calling.db": {
        "call_history": [(1, ALICE, 600, 2, ft(at(0)), 1, "1"), (2, ALICE, 30, 1, ft(at(120)), 1, "1"),
                         (3, BOB, 45, 1, ft(at(240)), 1, "1"), (4, UNKNOWN, 0, 3, ft(at(360)), 0, "7"),
                         (5, SHARED, 20, 1, ft(at(480)), 1, "1")],
        "content_sequence": [("call_logs", 5, 5)],
    },
    "phone.db": {
        "conversation": [(1, ALICE, ft(at(10))), (2, f"{ALICE}, {BOB}", ft(at(20))), (3, "EXAMPLEBANK", ft(at(30)))],
        "message": [(1, 1, ALICE, 1, "hello", ft(at(10)), 1), (2, 3, "EXAMPLEBANK", 1, "code", ft(at(30)), 1),
                    (3, 999, UNKNOWN, 1, "orphan", ft(at(700)), 2)],                     # 3: thread missing
        "participant": [(1, 1, ALICE, ALICE), (2, 2, ALICE, ALICE), (3, 2, BOB, BOB), (4, 998, "+1-555-0188", None)],
        "mms": [(1, 1, ft(at(11)), ALICE, 1)],
        "mms_part": [(1, 1, "image/jpeg", "example.jpg", 1234)],
        "rcs_conversation": [(50, 1, ALICE, ft(at(12))), (51, 12345, BOB, ft(at(13)))],     # 51: fallback missing
        "rcs_chat": [(1, 50, ALICE, ft(at(12)))],
        "conversation_attribute": [(1, -1, 1, 0, 0)],
        "sending_message": [(1, "s-1", 0, 1, 1, 1)],
        "subscription": [(1, 0, "us", "Example Carrier", "+1-555-0100")],
        "content_sequence": [("sms", 3, 5), ("conversation", 3, 3)],                        # sms: INCOMPLETE
    },
    "photos.db": {
        "media": [(1, "IMG_during_call.jpg", ft(at(5))), (2, "IMG_after_bob.jpg", ft(at(243))),
                  (3, "IMG_far_away.jpg", ft(at(5000)))],
        "content_sequence": [("media_info", 3, 3)],
    },
    "phoneapps.db": {
        "phone_apps": [("Example Chat", "com.example.chat"), ("Example Maps", "com.example.maps")],
        "recent_apps": [("com.example.chat", "Example Chat", 1, 300)],
        "pcapp_data": [("com.example.chat", 1, "ExampleChatWindows")],
        "content_sequence": [("phoneApps", 2, 2), ("recentApps", 1, 1)],
    },
    "notifications.db": {
        "notification_toastmappings": [(1, "n-1", "toast-1")],
        "phone_requests": [(1, "req-1", "n-1", None, "{}", "example-reply", 1, ms(at(2)), ms(at(2)), ms(at(3)),
                            ms(at(60)), 2)],
    },
    "continuity.db": {
        "browser_history": [("com.example.browser", "Example page", 1, 10, "https://example.com/")],
        "continue_app_context": [("ctx-1", "example", "com.example.chat", "Example task", ft(at(2)))],
        "content_sequence": [("browserHistory", 1, 1), ("continuityAppContext", 1, 1)],
    },
    "deviceData.db": {"wallpaper": [(0, 1, b"\xff\xd8\xff\xe0demo")]},
    "settings.db": {"settings": [("notifications", "com.example.chat", 1, "1", 0)]},
}

INSERT = {
    "contact": "INSERT INTO contact (contact_id, display_name, last_updated_time) VALUES (?, ?, %d)" % ft(BASE),
    "phonenumber": "INSERT INTO phonenumber (phone_number_id, contact_id, phone_number, display_phone_number)"
                   " VALUES (?, ?, ?, ?)",
    "emailaddress": "INSERT INTO emailaddress (email_address_id, contact_id, address) VALUES (?, ?, ?)",
    "contact_index": "INSERT INTO contact_index (id, contact_id, foreign_id, foreign_table_name) VALUES (?, ?, ?, ?)",
    "content_sequence": "INSERT INTO content_sequence (content_type, sequence, expected) VALUES (?, ?, ?)",
    "call_history": "INSERT INTO call_history (call_id, phone_number, duration, call_type, start_time, is_read,"
                    " phone_account_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
    "conversation": "INSERT INTO conversation (thread_id, recipient_list, timestamp, msg_count, unread_count)"
                    " VALUES (?, ?, ?, 1, 0)",
    "message": "INSERT INTO message (message_id, thread_id, from_address, type, body, timestamp, subscription_id)"
               " VALUES (?, ?, ?, ?, ?, ?, ?)",
    "participant": "INSERT INTO participant (id, thread_id, send_address, normalized_address) VALUES (?, ?, ?, ?)",
    "mms": "INSERT INTO mms (message_id, thread_id, timestamp, from_address, subscription_id) VALUES (?, ?, ?, ?, ?)",
    "mms_part": "INSERT INTO mms_part (part_id, message_id, content_type, name, size) VALUES (?, ?, ?, ?, ?)",
    "rcs_conversation": "INSERT INTO rcs_conversation (thread_id, fallback_id, recipient_list, timestamp, msg_count)"
                        " VALUES (?, ?, ?, ?, 1)",
    "rcs_chat": "INSERT INTO rcs_chat (message_id, thread_id, from_address, timestamp) VALUES (?, ?, ?, ?)",
    "conversation_attribute": "INSERT INTO conversation_attribute VALUES (?, ?, ?, ?, ?)",
    "sending_message": "INSERT INTO sending_message VALUES (?, ?, ?, ?, ?, ?)",
    "subscription": "INSERT INTO subscription (subscription_id, sim_slot_index, country_iso, name, is_roaming,"
                    " number) VALUES (?, ?, ?, ?, 0, ?)",
    "media": "INSERT INTO media (id, name, last_updated_time, taken_time, mime_type, height, width, size, uri)"
             " VALUES (?, ?, %d, ?, 'image/jpeg', 10, 10, 1000, 'example://photo')" % ft(BASE),
    "phone_apps": "INSERT INTO phone_apps (app_name, package_name, version, last_updated_time) VALUES (?, ?, '1', %d)"
                  % ms(BASE),
    "recent_apps": "INSERT INTO recent_apps (package_name, app_name, recent_app_rank, task_id) VALUES (?, ?, ?, ?)",
    "pcapp_data": "INSERT INTO pcapp_data (package_name, is_installed, platformapp_id) VALUES (?, ?, ?)",
    "phone_requests": "INSERT INTO phone_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    "notification_toastmappings": "INSERT INTO notification_toastmappings VALUES (?, ?, ?)",
    "browser_history": "INSERT INTO browser_history (package_name, browser_history_title, browser_history_rank,"
                       " task_id, web_uri) VALUES (?, ?, ?, ?, ?)",
    "continue_app_context": "INSERT INTO continue_app_context (context_id, type, app_id, title, create_time)"
                            " VALUES (?, ?, ?, ?, ?)",
    "wallpaper": "INSERT INTO wallpaper VALUES (?, ?, ?)",
    "settings": "INSERT INTO settings (setting_group_id, setting_key, setting_type, setting_value,"
                " setting_mute_expiration_timestamp) VALUES (?, ?, ?, ?, ?)",
}


def njson(pkg, key, minute, **extra):
    o = {"appName": extra.pop("appName", "Example Chat"), "packageName": pkg, "key": key, "id": 101,
         "postTime": ms(at(minute)), "timestamp": ms(at(minute)), "title": extra.pop("title", "Example title"),
         "text": extra.pop("text", "example text"), "isGroup": False, "isOngoing": False, "isClearable": True,
         "eventCount": 1, "flags": 16, "priority": 0, "importance": 3, "groupKey": key, "version": "1",
         "platform": 1, "notificationClass": 0, "featureFlags": 0, "showWhen": True}
    o.update(extra)
    return json.dumps(o)


CHAT_KEY = "0|com.example.chat|101|null|10001"
# (notification_id, package_name, json, post_minute, delete_afterwards)
NOTIFICATIONS = [
    ("n-1-v1", "com.example.chat", njson("com.example.chat", CHAT_KEY, 1, conversationTitle="Example Family",
                                         groupKey="g:chat", isGroup=True, messages=["hi"], senderNames=["Alice Example"],
                                         category="msg", template="ExampleStyle"), 1, True),   # older version
    ("n-2", "com.example.chat", njson("com.example.chat", "0|com.example.chat|0|summary|10001", 1, isGroup=True,
                                      flags=528, groupKey="g:chat", title="2 new messages"), 1, True),
    ("n-music", "com.example.music", njson("com.example.music", "0|com.example.music|7|null|10002", 3,
                                           appName="Example Music", isOngoing=True, isClearable=False, flags=34,
                                           category="transport"), 3, True),
    ("n-1", "com.example.chat", njson("com.example.chat", CHAT_KEY, 2, conversationTitle="Example Family",
                                      groupKey="g:chat", isGroup=True, messages=["hi", "see you"],
                                      senderNames=["Alice Example", "Bob Example"], eventCount=2, flags=24,
                                      importance=4, priority=1, category="msg", template="ExampleStyle",
                                      notificationActions=[{"actionIndex": 0, "actionName": "Reply",
                                                            "isActionInlineReply": True},
                                                           {"actionIndex": 1, "actionName": "Mark as read",
                                                            "isActionInlineReply": False}]), 2, False),
    ("n-mismatch", "com.example.chat", njson("com.example.other", "0|com.example.other|5|null|10003", 4,
                                             appName="Example Other"), 4, False),
    ("n-2", "com.example.chat", njson("com.example.chat", "0|com.example.chat|0|summary|10001", 2, isGroup=True,
                                      flags=528, groupKey="g:chat", title="2 new messages"), 2, False),  # re-posted
]


def plant_notifications(con):
    """Leave deleted notifications where a real carver finds them: on whole free pages.

    A row deleted from a page that keeps other rows becomes a freeblock; SQLite overwrites
    its first 4 bytes (the record header) and may later defragment the page, so it is rarely
    recoverable. Each planted row is padded to fill a page on its own, so deleting it frees
    the whole page with the record intact - the way real deleted notifications survive.
    The re-posted notification is small and lands on an existing page, so no freed page is
    reused.
    """
    con.execute("PRAGMA secure_delete = OFF")
    keep = [n for n in NOTIFICATIONS[:-1] if not n[4]]
    planted = [n for n in NOTIFICATIONS[:-1] if n[4]]

    def insert(nid, pkg, js, minute, pad=False):
        if pad:
            o = json.loads(js)
            o["bigText"] = "deleted example " + "x" * 2800
            js = json.dumps(o)
        return con.execute("INSERT INTO notifications (notification_id, package_name, json, post_time) VALUES"
                           " (?, ?, ?, ?)", (nid, pkg, js, ms(at(minute)))).lastrowid

    for nid, pkg, js, minute, _ in keep:
        insert(nid, pkg, js, minute)
    gone = [insert(nid, pkg, js, minute, pad=True) for nid, pkg, js, minute, _ in planted]
    con.commit()
    con.executemany("DELETE FROM notifications WHERE id = ?", [(i,) for i in gone])
    con.commit()
    nid, pkg, js, minute, _ = NOTIFICATIONS[-1]
    insert(nid, pkg, js, minute)
    con.commit()


def build(folder):
    os.makedirs(folder, exist_ok=True)
    for db, ddl in DDL.items():
        con = sqlite3.connect(os.path.join(folder, db))
        con.execute(SEQ)
        for s in ddl:
            con.execute(s)
        for table, rows in ROWS.get(db, {}).items():
            con.executemany(INSERT[table], rows)
        con.commit()
        if db == "notifications.db":
            plant_notifications(con)
        con.close()
    return folder


PAIRING_GUIDS = ["e1e1e1e1-e1e1-4e1e-ae1e-e1e1e1e1e1e1",   # oldest, folder only
                 "e2e2e2e2-e2e2-4e2e-ae2e-e2e2e2e2e2e2",   # databases left but empty
                 "e3e3e3e3-e3e3-4e3e-ae3e-e3e3e3e3e3e3"]   # the current phone, full demo data
DEMO_PHONE_ID = "demo-metadata-id-0001"


def build_package(root):
    """A synthetic Phone Link package: three pairings, metadata, trust file, a key file that must NOT be copied."""
    import time
    lc = os.path.join(root, "Microsoft.YourPhone_8wekyb3d8bbwe", "LocalCache")
    idx = os.path.join(lc, "Indexed")
    os.makedirs(os.path.join(idx, PAIRING_GUIDS[0]), exist_ok=True)
    empty = os.path.join(idx, PAIRING_GUIDS[1], "System", "Database")
    os.makedirs(empty, exist_ok=True)
    con = sqlite3.connect(os.path.join(empty, "settings.db"))
    for s in DDL["settings.db"]:
        con.execute(s)
    con.commit()
    con.close()
    time.sleep(1.2)                                           # the older pairings exist before this phone registers
    reg = datetime.now(timezone.utc).replace(tzinfo=None)     # naive UTC, as the rest of this file uses
    time.sleep(0.3)
    build(os.path.join(idx, PAIRING_GUIDS[2], "System", "Database"))   # current pairing folder appears just after
    ft = os.path.join(idx, "FullTrust")
    os.makedirs(ft, exist_ok=True)
    con = sqlite3.connect(os.path.join(ft, "phone_apps.db"))
    con.execute("CREATE TABLE hosted_app_ids (row_id INTEGER PRIMARY KEY, package_id TEXT, last_window_position TEXT,"
                " last_notification_count INTEGER, last_volume_level INTEGER, last_is_muted_state INTEGER,"
                " last_screen_mode INTEGER, last_window_position_in_expanded_layout TEXT, device_id TEXT)")
    con.executemany("INSERT INTO hosted_app_ids (package_id, last_notification_count, device_id) VALUES (?, ?, ?)",
                    [("com.example.chat", 2, PAIRING_GUIDS[2]), ("com.example.maps", 0, PAIRING_GUIDS[2])])
    con.commit()
    con.close()
    iso = reg.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    meta = {"ConfigVersion": 1, "DeviceMetadatas": {
        "00000000EXAMPLE1": [],                                  # a phone whose details were removed
        "00000000EXAMPLE2": [{"Certificates": {"SelfSigned": ["example-cert"]}, "IsLinked": True, "Metadata": {
            "Id": DEMO_PHONE_ID, "DistinguishedDeviceId": "demo-distinguished-0001", "RegitrationTime": iso,
            "LastSeenTime": iso, "Metadata": {
                "ClientType": "Android", "ClientVersion": "1.0.example", "DisplayName": "Example Phone",
                "OsName": "Android", "OsVersion": "99", "Capabilities": ["Messaging", "Photos"],
                "Manufacture": "ExampleCorp", "ModelName": "EX-1", "ModelVersion": "1", "ConsentVersion": 1,
                "IsEnabled": True, "CustomData": ""}}}]}}
    with open(os.path.join(lc, "DeviceMetadataStorage.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    trust = {"ConfigVersion": 1, "TrustedClients": {DEMO_PHONE_ID: {
        "SelfClientId": "demo-self-0001", "PartnerClientId": "demox_" + DEMO_PHONE_ID, "PartnerCertificate": "example-cert",
        "PartnerTemporaryCertificate": None, "PartnerKeyExpirationTime": iso, "RelationshipLastAccessTime": iso,
        "Attributes": {"dcg_prod_client_id": "demo-dcg-0001"}, "MsaCid": "demo-msa-0001", "TrustType": 3}}}
    with open(os.path.join(lc, "YppCryptoTrustRelationships"), "w", encoding="utf-8") as f:
        json.dump(trust, f)
    with open(os.path.join(lc, "PlatformEncryptedKeyStorage.json"), "w", encoding="utf-8") as f:
        json.dump({"ConfigVersion": 1, "EncryptedKeys": {"f0f0f0f0-f0f0-4f0f-af0f-f0f0f0f0f0f0": "secret"}}, f)
    return os.path.join(root, "Microsoft.YourPhone_8wekyb3d8bbwe")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print("built", build(sys.argv[1]))
