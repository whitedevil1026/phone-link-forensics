"""Synthetic Phone Link databases with a known answer key.

Every value written here is invented: example.com URLs, com.example.* packages and
555-01xx numbers (reserved for fiction). No real device data is used or needed.

Each scenario is a folder of databases plus `expected`, a dict of
    (map_file, BaseFileName) -> {"rows": [ {column: csv_text}, ... ],
                                 "blobs": {identifier: bytes}}
computed in Python, independently of the SQL in the maps. The tests compare what
SQLECmd (or the emulator in run_tests.py) produces against this answer key.
"""
import os
import random
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

EPOCH_1601 = datetime(1601, 1, 1, tzinfo=timezone.utc)
EPOCH_1970 = datetime(1970, 1, 1, tzinfo=timezone.utc)
EPOCH_0001 = datetime(1, 1, 1, tzinfo=timezone.utc)
CALL_TYPES = {1: 'Incoming', 2: 'Outgoing', 3: 'Missed', 4: 'Voicemail', 5: 'Rejected',
              6: 'Blocked', 7: 'AnsweredExternally'}

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 24
JPG = b'\xff\xd8\xff\xe0' + b'\x00' * 24

# Text that has broken CSV writers, file namers and spreadsheet viewers before.
NASTY_TEXT = [
    'comma, inside', 'quote " inside', 'new\nline', 'tab\tinside', 'emoji \U0001F600',
    'rtl שלום', 'nul-free \u0000-less'.replace('\u0000', ''),
    '=HYPERLINK("https://example.com")', '+1+1', '@SUM(1)', '-2+3', ' leading space',
]
NASTY_NAMES = [
    '..\\..\\..\\escape', '../../escape', 'a/b\\c', 'CON', 'NUL.txt', '<>:"|?*',
    'com.example.' + 'x' * 60 + '.one', 'com.example.' + 'x' * 60 + '.two',  # same first 50 chars
    'com.example.dup', 'com.example.dup',                                     # clone apps
    'Com.Example.Case', 'com.example.case',                                   # differ by case only
    '', None, '___', 'space name',
]


def ts(dt):
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def filetime(dt):
    return int((dt - EPOCH_1601).total_seconds()) * 10_000_000 + random.randint(0, 9_999_999)


MAX_FT = 2650467743990000000  # 9999-12-31: SQLite datetime() returns NULL beyond this


def from_filetime(ft):
    return ts(EPOCH_1601 + timedelta(seconds=ft // 10_000_000))


def ft_or_empty(v):
    """Oracle for the maps' guarded FILETIME conversion: numbers in range only."""
    return from_filetime(v) if isinstance(v, int) and 0 < v <= MAX_FT else ''


def unix_ms(dt):
    return int((dt - EPOCH_1970).total_seconds()) * 1000 + random.randint(0, 999)


def size_epoch(v):
    """Python oracle for the maps' 'convert by size' CASE (ticks, FILETIME, us, ms, s)."""
    if not isinstance(v, (int, float)) or v <= 0:
        return ''
    if v >= 400_000_000_000_000_000:
        return ts(EPOCH_0001 + timedelta(seconds=v // 10_000_000))
    if v >= 10_000_000_000_000_000:
        return from_filetime(v)
    if v >= 100_000_000_000_000:
        return ts(EPOCH_1970 + timedelta(seconds=v // 1_000_000))
    if v >= 100_000_000_000:
        return ts(EPOCH_1970 + timedelta(seconds=v // 1000))
    return ts(EPOCH_1970 + timedelta(seconds=v))


def text(v):
    """How a value appears in SQLECmd's CSV (and in the emulator)."""
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def rand_dt(rng):
    return datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=rng.randint(0, 60_000_000))


def phone(rng):
    return f'+1 555 01{rng.randint(0, 99):02d}'


def mkdb(path, ddl, wal=False):
    con = sqlite3.connect(path)
    if wal:
        con.execute('PRAGMA journal_mode=WAL')
    con.executescript(ddl)
    return con


DDL = {
    'calling': '''CREATE TABLE call_history(call_id INTEGER PRIMARY KEY, phone_number TEXT, duration INTEGER,
        call_type INTEGER, start_time INTEGER, is_read INTEGER, phone_account_id TEXT, last_updated_time INTEGER);
        CREATE TABLE content_sequence(type TEXT, sequence INTEGER);''',
    'photos': '''CREATE TABLE media(id INTEGER PRIMARY KEY, name TEXT, mime_type TEXT, last_updated_time INTEGER,
        taken_time INTEGER, last_seen_time INTEGER, height INTEGER, width INTEGER, orientation INTEGER,
        size INTEGER, uri TEXT, thumbnail BLOB, media BLOB);
        CREATE TABLE content_sequence(type TEXT, sequence INTEGER);''',
    'photo_legacy': '''CREATE TABLE photo(photo_id INTEGER PRIMARY KEY, name TEXT, last_updated_time INTEGER,
        size INTEGER, uri TEXT, thumbnail BLOB, blob BLOB);''',
    'phoneapps': '''CREATE TABLE phone_apps(app_id INTEGER PRIMARY KEY, app_name TEXT, package_name TEXT,
        version TEXT, favorite_rank INTEGER, blob BLOB, last_updated_time INTEGER, phoneapp_web_url TEXT);
        CREATE TABLE recent_apps(app_id INTEGER PRIMARY KEY, package_name TEXT, app_name TEXT, recent_app_rank INTEGER,
        intent_action TEXT, task_id INTEGER, blob BLOB, web_uri TEXT, desktop_uri TEXT);
        CREATE TABLE pcapp_data(id INTEGER PRIMARY KEY, package_name TEXT, is_installed INTEGER,
        platformapp_url TEXT, platformapp_id TEXT, last_fetched INTEGER, platformapp_package TEXT);
        CREATE TABLE content_sequence(type TEXT, sequence INTEGER);''',
    'continuity': '''CREATE TABLE browser_history(browser_history_id INTEGER PRIMARY KEY, package_name TEXT,
        browser_history_title TEXT, browser_history_rank INTEGER, task_id INTEGER, favicon BLOB, web_uri TEXT, source INTEGER);
        CREATE TABLE continue_app_context(id INTEGER PRIMARY KEY, context_id TEXT, task_id INTEGER, type INTEGER,
        team_id TEXT, intent_uri TEXT, app_id TEXT, title TEXT, web_link TEXT, preview BLOB, extras TEXT,
        create_time INTEGER, last_updated_time INTEGER, life_time INTEGER);
        CREATE TABLE content_sequence(type TEXT, sequence INTEGER);''',
    'devicedata': '''CREATE TABLE wallpaper(wallpaper_type INTEGER PRIMARY KEY, wallpaper_id INTEGER, blob BLOB);
        CREATE TABLE content_sequence(type TEXT, sequence INTEGER);''',
    # Older settings.db layout: phone_apps/recent_apps without phoneapp_web_url and no pcapp_data.
    'settings_like': '''CREATE TABLE settings(setting_key TEXT, setting_value TEXT);
        CREATE TABLE phone_apps(app_id INTEGER PRIMARY KEY, app_name TEXT, package_name TEXT, version TEXT,
        favorite_rank INTEGER, blob BLOB, last_updated_time INTEGER);
        CREATE TABLE recent_apps(app_id INTEGER PRIMARY KEY, package_name TEXT, app_name TEXT, recent_app_rank INTEGER,
        intent_action TEXT, task_id INTEGER, blob BLOB, web_uri TEXT, desktop_uri TEXT);''',
    'generic_media': 'CREATE TABLE media(id INTEGER PRIMARY KEY, name TEXT, uri TEXT, size INTEGER);',
    'generic_wallpaper': 'CREATE TABLE wallpaper(wallpaper_type INTEGER, wallpaper_id TEXT, blob BLOB);',
}

M_CALL = 'Windows_YourPhone_CallingDB.smap'
M_PHOTO = 'Windows_YourPhone_PhotosDB.smap'
M_APPS = 'Windows_YourPhone_PhoneAppsDB.smap'
M_CONT = 'Windows_YourPhone_ContinuityDB.smap'
M_DEV = 'Windows_YourPhone_DeviceDataDB.smap'


def _calls(con, rng, n, edge):
    rows = []
    for i in range(1, n + 1):
        dt = rand_dt(rng)
        st, lu = filetime(dt), filetime(dt + timedelta(minutes=5))
        ctype, dur, read = rng.choice(list(CALL_TYPES) + [9]), rng.randint(0, 7200), rng.choice([0, 1])
        if edge and i <= 7:  # zero / NULL / negative / text / out-of-range times and durations, unknown type
            st, lu, dur, ctype = [(0, 0, 0, 1), (None, None, None, None), (st, -5, -1, 99),
                                  (st, lu, 86400 * 2 + 5, 2), (st, 0, 0, 3), (st, lu, 59, 5),
                                  ('2024-01-01T00:00:00', 2 ** 62, 'thirty', 1)][i - 1]
        num = phone(rng) if not edge else NASTY_TEXT[i % len(NASTY_TEXT)]
        con.execute('INSERT INTO call_history VALUES(?,?,?,?,?,?,?,?)', (i, num, dur, ctype, st, read, f'sim{i % 2}', lu))
        rows.append({
            '_sort': ((0, 0) if st is None else (1, st) if isinstance(st, int) else (2, 0), i),
            'CallID': text(i),
            'StartTimeUTC': ft_or_empty(st),
            'PhoneNumber': text(num),
            'CallType': CALL_TYPES.get(ctype, text(ctype)),
            'DurationSeconds': text(dur),
            'Duration': f'{dur // 3600:02d}:{(dur % 3600) // 60:02d}:{dur % 60:02d}' if isinstance(dur, int) and dur >= 0 else '',
            'IsRead': {0: 'No', 1: 'Yes'}[read],
            'PhoneAccountID': f'sim{i % 2}',
            'LastUpdatedTimeUTC': ft_or_empty(lu),
            'StartTimeRaw': text(st), 'LastUpdatedTimeRaw': text(lu),
        })
    rows.sort(key=lambda r: r.pop('_sort'))
    return {(M_CALL, 'CallingDB_CallHistory'): {'rows': rows, 'blobs': {}, 'ordered': True}}


def _photos(con, rng, n, edge):
    rows, thumbs, medias, tb, mb = [], [], [], {}, {}
    for i in range(1, n + 1):
        dt = rand_dt(rng)
        lu, tk, ls = filetime(dt + timedelta(days=1)), filetime(dt), filetime(dt + timedelta(days=2))
        if edge and i <= 4:
            lu, tk, ls = [(0, tk, ls), (lu, 0, None), (-5, None, 0), ('2024-01-01', 2 ** 62, tk)][i - 1]
        size = rng.randint(1, 9_000_000)
        name = f'IMG_{i:05d}.jpg' if not edge else (NASTY_TEXT[i % len(NASTY_TEXT)] + '.jpg')
        thumb = (JPG + bytes([i % 256]) * 3) if i % 3 == 0 else (b'' if i % 3 == 1 else None)
        mblob = (b'MEDIA' + bytes([i % 256])) if i % 7 == 0 else None
        con.execute('INSERT INTO media VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (i, name, 'image/jpeg', lu, tk, ls, 3000, 4000, rng.choice([0, 90, 180, 270]), size,
                     f'content://media/external/images/media/{1000 + i}', thumb, mblob))
        o = con.execute('SELECT orientation FROM media WHERE id=?', (i,)).fetchone()[0]
        g = ft_or_empty
        rows.append({'Media ID': text(i), 'MIME Type': 'image/jpeg', 'Name': name,
                     'Last Updated Time': g(lu), 'Taken Time': g(tk), 'Last Seen Time': g(ls),
                     'Height': '3000', 'Width': '4000', 'Orientation': text(o), 'Size (kb)': text(size / 1000.0),
                     'URI': f'content://media/external/images/media/{1000 + i}',
                     'Last Updated Time Raw': text(lu), 'Taken Time Raw': text(tk), 'Last Seen Time Raw': text(ls)})
        if thumb:
            thumbs.append({'MediaID': text(i), 'Name': name, 'ThumbnailBytes': text(len(thumb))})
            tb[str(i)] = thumb
        if mblob:
            medias.append({'MediaID': text(i), 'Name': name, 'MediaBytes': text(len(mblob))})
            mb[str(i)] = mblob
    return {(M_PHOTO, 'PhotosDB'): {'rows': rows, 'blobs': {}, 'ordered': True},
            (M_PHOTO, 'PhotosDB_Thumbnails'): {'rows': thumbs, 'blobs': tb, 'ordered': True, 'ext': '.jpg'},
            (M_PHOTO, 'PhotosDB_MediaBlobs'): {'rows': medias, 'blobs': mb, 'ordered': True}}


def _apps(con, rng, n, edge):
    inst, rec, pc, blobs_i, blobs_r = [], [], [], {}, {}
    names = NASTY_NAMES if edge else [f'com.example.app{i}' for i in range(1, n + 1)]
    by_pkg = {}
    for i, pkg in enumerate(names, start=1):
        icon = PNG + bytes([i % 256]) * (i % 7) + i.to_bytes(4, 'little')
        lu = unix_ms(rand_dt(rng)) if i % 5 else 0
        app = f'App {i}'
        con.execute('INSERT INTO phone_apps VALUES(?,?,?,?,?,?,?,?)',
                    (i, app, pkg, f'1.{i}', -1 if i % 3 else 7, icon, lu, f'https://example.com/app/{i}'))
        by_pkg.setdefault(pkg, []).append(app)
        inst.append({'AppID': text(i), 'AppName': app, 'PackageName': text(pkg), 'Version': f'1.{i}',
                     'LastUpdatedTimeUTC': ts(EPOCH_1970 + timedelta(seconds=lu // 1000)) if lu > 0 else '',
                     'FavoriteRank': '-1' if i % 3 else '7', 'WebURL': f'https://example.com/app/{i}',
                     'IconFileName': f'{i}_{pkg or ""}'})
        blobs_i[f'{i}_{pkg or ""}'] = icon
    for i in range(1, 4):
        pkg = names[i - 1]
        icon = (PNG + b'r' + bytes([i])) if i != 2 else None
        con.execute('INSERT INTO recent_apps VALUES(?,?,?,?,?,?,?,?,?)',
                    (i, pkg, f'Recent {i}', i, 'android.intent.action.MAIN', 100 + i, icon,
                     f'https://example.com/r/{i}', None))
        rec.append({'AppID': text(i), 'Rank': text(i), 'AppName': f'Recent {i}', 'PackageName': text(pkg),
                    'IntentAction': 'android.intent.action.MAIN', 'WebURI': f'https://example.com/r/{i}',
                    'DesktopURI': '', 'AndroidTaskID': text(100 + i), 'IconFileName': f'{i}_{pkg or ""}'})
        if icon:
            blobs_r[f'{i}_{pkg or ""}'] = icon
    now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    stamps = [int((now - EPOCH_0001).total_seconds()) * 10_000_000, filetime(now),
              int((now - EPOCH_1970).total_seconds()) * 1_000_000, unix_ms(now),
              int((now - EPOCH_1970).total_seconds()), 0, None, 'not-a-time']
    for i, v in enumerate(stamps, start=1):
        pkg = names[(i - 1) % len(names)]
        if edge and i == 1:
            pkg = 'com.example.dup'  # joins to two phone_apps rows in the edge scenario
        con.execute('INSERT INTO pcapp_data VALUES(?,?,?,?,?,?,?)',
                    (i, pkg, i % 2, f'https://example.com/pc/{i}', f'9NBLGGH{i:05d}', v, f'Example.App{i}'))
        pc.append({'ID': text(i), 'PackageName': text(pkg),
                   'AppName': ', '.join(sorted(set(by_pkg.get(pkg, [])))) if pkg is not None else '',
                   'IsInstalled': {0: 'No', 1: 'Yes'}[i % 2], 'PlatformAppID': f'9NBLGGH{i:05d}',
                   'PlatformAppPackage': f'Example.App{i}', 'PlatformAppURL': f'https://example.com/pc/{i}',
                   'LastFetchedUTC': size_epoch(v), 'LastFetchedRaw': text(v)})
    return {(M_APPS, 'PhoneAppsDB_InstalledApps'): {'rows': inst, 'blobs': blobs_i, 'ext': '.png'},
            (M_APPS, 'PhoneAppsDB_RecentApps'): {'rows': rec, 'blobs': blobs_r},
            (M_APPS, 'PhoneAppsDB_PCAppData'): {'rows': pc, 'blobs': {}, 'list_cols': ['AppName']}}


def _continuity(con, rng, n, edge):
    bh, ctx, bb, cb = [], [], {}, {}
    for i in range(1, n + 1):
        fav = b'\x00\x00\x01\x00' + bytes([i % 256]) if i % 2 else None
        title = NASTY_TEXT[i % len(NASTY_TEXT)] if edge else f'Example page {i}'
        con.execute('INSERT INTO browser_history VALUES(?,?,?,?,?,?,?,?)',
                    (i, 'com.example.browser', title, n - i, 500 + i, fav, f'https://example.com/p/{i}', 1))
        bh.append({'BrowserHistoryID': text(i), 'Rank': text(n - i), 'Title': title,
                   'URL': f'https://example.com/p/{i}', 'BrowserPackageName': 'com.example.browser',
                   'Source': '1', 'AndroidTaskID': text(500 + i)})
        if fav:
            bb[str(i)] = fav
    now = datetime(2025, 3, 3, 3, 3, 3, tzinfo=timezone.utc)
    units = [int((now - EPOCH_0001).total_seconds()) * 10_000_000, filetime(now),
             int((now - EPOCH_1970).total_seconds()) * 1_000_000, unix_ms(now),
             int((now - EPOCH_1970).total_seconds()), 0, -1, None, '2025-01-01T00:00:00Z', 'abc']
    for i, v in enumerate(units, start=1):
        prev = b'PREVIEW' + bytes([i]) if i % 2 else None
        con.execute('INSERT INTO continue_app_context VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (i, f'ctx-{i}', 700 + i, i % 3, 'team', 'intent://example', 'com.example.app',
                     f'Context {i}', f'https://example.com/c/{i}', prev, '{"k":"v"}', v, v, -1))
        ctx.append({'ID': text(i), 'ContextID': f'ctx-{i}', 'CreateTimeUTC': size_epoch(v), 'CreateTimeRaw': text(v),
                    'LastUpdatedTimeUTC': size_epoch(v), 'LastUpdatedTimeRaw': text(v), 'LifeTimeRaw': '-1',
                    'Type': text(i % 3), 'AppID': 'com.example.app', 'Title': f'Context {i}',
                    'WebLink': f'https://example.com/c/{i}', 'IntentURI': 'intent://example', 'TeamID': 'team',
                    'AndroidTaskID': text(700 + i), 'Extras': '{"k":"v"}'})
        if prev:
            cb[str(i)] = prev
    return {(M_CONT, 'ContinuityDB_BrowserHistory'): {'rows': bh, 'blobs': bb},
            (M_CONT, 'ContinuityDB_ContinueAppContext'): {'rows': ctx, 'blobs': cb}}


def _devicedata(con, rng, n, edge):
    rows, blobs = [], {}
    # wallpaper_type is the table's primary key, so it is unique and safe as the blob file name.
    for rid, (wtype, wid) in enumerate([(0, 10), (1, 11), (2, None)], start=1):
        img = JPG + bytes([rid]) * rid
        con.execute('INSERT INTO wallpaper VALUES(?,?,?)', (wtype, wid, img))
        rows.append({'WallpaperType': text(wtype), 'WallpaperID': text(wid), 'SizeBytes': text(len(img))})
        blobs[str(wtype)] = img
    return {(M_DEV, 'DeviceDataDB_Wallpaper'): {'rows': rows, 'blobs': blobs, 'ext': '.jpg'}}


BUILDERS = [('calling.db', 'calling', _calls), ('photos.db', 'photos', _photos), ('phoneapps.db', 'phoneapps', _apps),
            ('continuity.db', 'continuity', _continuity), ('deviceData.db', 'devicedata', _devicedata)]


def build_current(folder, n=25, edge=False, seed=1, wal_only=False):
    """All five databases in the current layout. wal_only leaves every row un-checkpointed in -wal."""
    random.seed(seed)
    rng = random.Random(seed)
    os.makedirs(folder, exist_ok=True)
    expected = {}
    for fname, ddl, fn in BUILDERS:
        path = os.path.join(folder, fname)
        if wal_only:
            tmp = path + '.building'
            con = mkdb(tmp, DDL[ddl], wal=True)
            con.commit()
            con.execute('PRAGMA wal_autocheckpoint=0')
            con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            expected.update(fn(con, rng, n, edge))
            con.commit()
            for ext in ('', '-wal', '-shm'):  # copy while open: rows exist only in the -wal copy
                shutil.copyfile(tmp + ext, path + ext)
            con.close()
            for ext in ('', '-wal', '-shm'):
                if os.path.exists(tmp + ext):
                    os.remove(tmp + ext)
        else:
            con = mkdb(path, DDL[ddl])
            expected.update(fn(con, rng, n, edge))
            con.commit()
            con.close()
    return expected


def build_legacy_photos(folder, n=10, seed=2):
    """2021-era photos.db: media plus the photo table that v1.1 required."""
    random.seed(seed)
    os.makedirs(folder, exist_ok=True)
    con = mkdb(os.path.join(folder, 'photos.db'), DDL['photos'] + DDL['photo_legacy'])
    exp = _photos(con, random.Random(seed), n, False)
    con.commit()
    con.close()
    return exp


def build_negatives(folder):
    """Files that must NOT match any of these maps, and must not crash the run."""
    os.makedirs(folder, exist_ok=True)
    for name, ddl in [('settings.db', 'settings_like'), ('media_other_app.db', 'generic_media'),
                      ('wallpaper_other_app.db', 'generic_wallpaper')]:
        mkdb(os.path.join(folder, name), DDL[ddl]).close()
    sqlite3.connect(os.path.join(folder, 'empty.db')).execute('PRAGMA user_version=1').connection.close()
    views = os.path.join(folder, 'views')
    os.makedirs(views, exist_ok=True)
    for name, ddl in [('calling.db', 'calling'), ('photos.db', 'photos')]:
        con = sqlite3.connect(os.path.join(views, name))
        con.executescript(DDL[ddl].replace('CREATE TABLE call_history(', 'CREATE TABLE real_calls(')
                          .replace('CREATE TABLE media(', 'CREATE TABLE real_media('))
        con.execute('CREATE VIEW call_history AS SELECT * FROM real_calls' if ddl == 'calling'
                    else 'CREATE VIEW media AS SELECT * FROM real_media')
        con.close()
    sub = os.path.join(folder, 'broken')
    os.makedirs(sub, exist_ok=True)
    open(os.path.join(sub, 'photos.db'), 'wb').close()                                   # zero bytes
    open(os.path.join(sub, 'calling.db'), 'w').write('not a database\n')                 # text file
    good = os.path.join(folder, '_good.tmp')
    mkdb(good, DDL['calling']).close()
    data = open(good, 'rb').read()
    os.remove(good)
    trunc = os.path.join(folder, 'truncated')
    os.makedirs(trunc, exist_ok=True)
    open(os.path.join(trunc, 'calling.db'), 'wb').write(data[:1024])                    # header only
    return {}


def build_stress(folder, calls=1_000_000, photos=500_000, apps=20_000, icon_bytes=16_384, seed=3):
    random.seed(seed)
    rng = random.Random(seed)
    os.makedirs(folder, exist_ok=True)
    con = mkdb(os.path.join(folder, 'calling.db'), DDL['calling'])
    con.executemany('INSERT INTO call_history VALUES(?,?,?,?,?,?,?,?)',
                    ((i, phone(rng), rng.randint(0, 3600), rng.randint(1, 7), filetime(rand_dt(rng)), 1, 'sim0',
                      filetime(rand_dt(rng))) for i in range(1, calls + 1)))
    con.commit(); con.close()
    con = mkdb(os.path.join(folder, 'photos.db'), DDL['photos'])
    con.executemany('INSERT INTO media VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    ((i, f'IMG_{i}.jpg', 'image/jpeg', filetime(rand_dt(rng)), filetime(rand_dt(rng)),
                      filetime(rand_dt(rng)), 3000, 4000, 0, rng.randint(1, 9_000_000),
                      f'content://media/{i}', None) for i in range(1, photos + 1)))
    con.commit(); con.close()
    con = mkdb(os.path.join(folder, 'phoneapps.db'), DDL['phoneapps'])
    icon = PNG + os.urandom(icon_bytes)
    con.executemany('INSERT INTO phone_apps VALUES(?,?,?,?,?,?,?,?)',
                    ((i, f'App {i}', f'com.example.stress{i}', '1.0', -1, icon, unix_ms(rand_dt(rng)),
                      f'https://example.com/{i}') for i in range(1, apps + 1)))
    con.executemany('INSERT INTO pcapp_data VALUES(?,?,?,?,?,?,?)',
                    ((i, f'com.example.stress{i}', 1, f'https://example.com/pc/{i}', f'ID{i}', unix_ms(rand_dt(rng)),
                      f'Example.Stress{i}') for i in range(1, apps + 1)))
    con.commit(); con.close()
    return {'calls': calls, 'photos': photos, 'apps': apps}
