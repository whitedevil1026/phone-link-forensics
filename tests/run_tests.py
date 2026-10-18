"""Test the maps against synthetic Phone Link databases with a known answer key.

    python tests/run_tests.py                              # emulator only (CI)
    python tests/run_tests.py --sqlecmd C:\\Tools\\SQLECmd.exe  # also the real SQLECmd
    python tests/run_tests.py --upstream-maps <dir>        # also check no upstream map cross-matches

The emulator follows SQLECmd's rules: a map applies when IdentifyQuery's result equals
IdentifyValue as a string, and blob files are named
{time}_{CSVPrefix}_{BaseFileName}_{Id}_{NameColumn value, sanitised, 50 chars}_{BlobColumn}.{ext}
on a case-insensitive file system, so two rows that produce the same name overwrite each other.
"""
import argparse
import csv
import glob
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAPS = os.path.join(ROOT, 'maps')
INVALID = set('"<>|:*?\\/') | {chr(i) for i in range(32)}
FAILS = []


def check(ok, label):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        FAILS.append(label)


def load_maps(folder):
    out = {}
    for p in sorted(glob.glob(os.path.join(folder, '*.smap'))):
        with open(p, encoding='utf-8') as fh:
            out[os.path.basename(p)] = yaml.safe_load(fh)
    return out


def sanitize(value, max_len=50):
    """SQLECmd's SanitizeFilenamePart."""
    if value is None or str(value).strip() == '':
        return None
    s = ''.join('_' if c in INVALID else c for c in str(value)).replace(' ', '_').strip('_')
    s = s[:max_len].strip('_')
    return s if s.replace('_', '') else None


def is_sqlite(path):
    try:
        with open(path, 'rb') as fh:
            return fh.read(16) == b'SQLite format 3\x00'
    except OSError:
        return False


# ---------------------------------------------------------------- engines
def run_emulator(folder, maps):
    res = {'matches': Counter(), 'rows': {}, 'blobs': {}, 'blob_writes': Counter(), 'errors': [], 'skipped': [],
           'misaligned': Counter()}
    for path in sorted(glob.glob(os.path.join(folder, '**', '*'), recursive=True)):
        if not os.path.isfile(path) or path.endswith(('-wal', '-shm')) or not is_sqlite(path):
            continue
        try:
            con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
            con.execute('SELECT count(*) FROM sqlite_master').fetchone()
        except sqlite3.Error:
            res['skipped'].append(os.path.relpath(path, folder))  # unreadable: skipped, as SQLECmd does
            continue
        for mname, m in maps.items():
            try:
                got = con.execute(m['IdentifyQuery']).fetchone()[0]
            except sqlite3.Error:
                continue
            if str(got).lower() != str(m['IdentifyValue']).lower():
                continue
            res['matches'][(os.path.relpath(path, folder), mname)] += 1
            for q in m['Queries']:
                cur = con.execute(q['Query'])
                cols = [d[0] for d in cur.description]
                blobcfg = q.get('BlobColumns') or []
                blobcols = {b['BlobColumn'] for b in blobcfg}
                rows, counter = [], 0
                for r in cur.fetchall():
                    d = dict(zip(cols, r))
                    # SQLECmd drops a NULL blob field from that row only, shifting SourceFile: treat it as misaligned
                    res['misaligned'][(mname, q['BaseFileName'])] += any(d[b['BlobColumn']] is None for b in blobcfg)
                    rows.append({c: fixtures.text(v) for c, v in d.items() if c not in blobcols})
                    for b in blobcfg:
                        data = d[b['BlobColumn']]
                        if not data:
                            continue
                        counter += 1
                        ident = sanitize(d.get(b.get('NameColumn'))) if b.get('NameColumn') else None
                        ident = ident or str(counter)
                        ext = '.' + b['BlobExtension'] if b.get('BlobExtension') else '.blob'
                        fname = f"{m['CSVPrefix']}_{q['BaseFileName']}_{m['Id']}_{ident}_{b['BlobColumn']}{ext}"
                        res['blob_writes'][(mname, q['BaseFileName'])] += 1
                        res['blobs'].setdefault((mname, q['BaseFileName']), {})[fname.lower()] = (ident, bytes(data), fname)
                res['rows'][(mname, q['BaseFileName'])] = rows
                res.setdefault('columns', {})[(mname, q['BaseFileName'])] = [c for c in cols if c not in blobcols]
    return res


def run_sqlecmd(exe, folder, maps_dir, maps):
    out = tempfile.mkdtemp(prefix='sqlecmd_out_')
    p = subprocess.run([exe, '-d', folder, '--hunt', '--maps', maps_dir, '--csv', os.path.join(out, 'csv'),
                        '--blobdir', os.path.join(out, 'blobs'), '--debug'],
                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3600, cwd=out)
    log = p.stdout + p.stderr
    res = {'matches': Counter(), 'rows': {}, 'blobs': {}, 'blob_writes': Counter(), 'errors': [],
           'exit': p.returncode, 'log': log, 'misaligned': Counter(), 'columns': {}}
    by_id = {m['Id']: (n, m) for n, m in maps.items()}
    cur_file, last_id = None, None
    for line in log.splitlines():
        mf = re.search(r'INF\] Processing (.+?)\.\.\.$', line)
        if mf:
            cur_file = os.path.relpath(mf.group(1).replace('/', os.sep), folder)
        mi = re.search(r'Processing map .+ with Id ([0-9a-f-]{36})', line)
        if mi:
            last_id = mi.group(1)
        if 'Map queries found' in line and cur_file and last_id in by_id:
            res['matches'][(cur_file, by_id[last_id][0])] += 1
        if re.search(r'Unhandled|System\.\w+Exception', line):
            res['errors'].append(line[-200:])
    for f in glob.glob(os.path.join(out, 'csv', '*.csv')):
        for mid, (mname, m) in by_id.items():
            for q in m['Queries']:
                if f.endswith(f"_{m['CSVPrefix']}_{q['BaseFileName']}_{mid}.csv"):
                    with open(f, encoding='utf-8-sig', newline='') as fh:
                        raw = list(csv.reader(fh))
                    hdr, body = raw[0], raw[1:]
                    key = (mname, q['BaseFileName'])
                    # every row must have exactly the header's fields, and SourceFile must hold the database path
                    si = hdr.index('SourceFile') if 'SourceFile' in hdr else None
                    res['misaligned'][key] += sum(1 for r in body if len(r) != len(hdr) or si is None
                                                  or not r[si].lower().endswith('.db'))
                    res['columns'][key] = [h for h in hdr if h != 'SourceFile']
                    res['rows'].setdefault(key, []).extend(
                        {h: (r[i] if i < len(r) else '<missing>') for i, h in enumerate(hdr) if h != 'SourceFile'} for r in body)
    for f in glob.glob(os.path.join(out, 'blobs', '*')):
        base = os.path.basename(f)
        for mid, (mname, m) in by_id.items():
            for q in m['Queries']:
                for b in q.get('BlobColumns') or []:
                    mm = re.match(rf"\d+_{re.escape(m['CSVPrefix'])}_{re.escape(q['BaseFileName'])}_{mid}_(.+)_{re.escape(b['BlobColumn'])}\.\w+$", base)
                    if mm:
                        with open(f, 'rb') as fh:
                            res['blobs'].setdefault((mname, q['BaseFileName']), {})[base.lower()] = (mm.group(1), fh.read(), base)
    shutil.rmtree(out, ignore_errors=True)
    return res


# ---------------------------------------------------------------- comparisons
def compare(res, expected, label, maps):
    for key, exp in expected.items():
        got = res['rows'].get(key, [])
        cols = list(exp['rows'][0].keys()) if exp['rows'] else []
        lists = set(exp.get('list_cols', ()))

        def cell(r, c):
            v = r.get(c, '<missing>')
            return ''.join(sorted(re.split(r', | \| ', v))) if c in lists else v
        if exp['rows']:
            have_cols = res.get('columns', {}).get(key, [])
            check(sorted(have_cols) == sorted(cols), f'{label} {key[1]}: output columns are exactly the expected set '
                                                     f'(extra {sorted(set(have_cols) - set(cols))}, missing {sorted(set(cols) - set(have_cols))})')
        want = Counter(tuple(cell(r, c) for c in cols) for r in exp['rows'])
        have = Counter(tuple(cell(r, c) for c in cols) for r in got)
        check(want == have, f'{label} {key[1]}: rows match answer key '
                            f'(expected {sum(want.values())}, got {sum(have.values())}, '
                            f'{sum((want - have).values())} missing, {sum((have - want).values())} unexpected)')
        if exp.get('ordered'):
            check([tuple(cell(r, c) for c in cols) for r in exp['rows']] == [tuple(cell(r, c) for c in cols) for r in got],
                  f'{label} {key[1]}: rows come out in the ORDER BY order of the map')
        if 'misaligned' in res:
            check(res['misaligned'][key] == 0, f'{label} {key[1]}: every CSV row aligned with the header, SourceFile intact '
                                               f'({res["misaligned"][key]} misaligned)')
        if exp['blobs'] or key in res['blobs']:
            files = res['blobs'].get(key, {})
            want_b = Counter((sanitize(k), v) for k, v in exp['blobs'].items())
            have_b = Counter((v[0], v[1]) for v in files.values())
            check(want_b == have_b, f'{label} {key[1]}: every blob extracted once, right name, identical bytes '
                                    f'(expected {sum(want_b.values())}, files {len(files)})')
            want_ext = exp.get('ext', '.blob')  # from the answer key, not from the map under test
            check(all(v[2].lower().endswith(want_ext) for v in files.values()),
                  f'{label} {key[1]}: blob files have the expected extension {want_ext}')


def scenario(name, builder, engine, maps, expect_matches, **kw):
    tmp = tempfile.mkdtemp(prefix=f'pl_{name}_')
    try:
        expected = builder(tmp, **kw)
        res = engine(tmp)
        label = f'[{name}]'
        check(not res['errors'] and res.get('exit', 0) == 0,
              f'{label} no crash / unhandled error ({len(res["errors"])} errors)')
        got_matches = {(f.replace('\\', '/'), m) for (f, m) in res['matches']}
        ours = {k for k in got_matches if k[1] in maps}
        check(ours == expect_matches, f'{label} map matching: expected {sorted(expect_matches)}, got {sorted(ours)}')
        dup = [k for k, v in res['matches'].items() if v > 1]
        check(not dup, f'{label} no database matched the same map twice')
        compare(res, expected, label, maps)
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def static_checks(maps):
    print('== static checks')
    ids = [m['Id'] for m in maps.values()]
    check(len(ids) == len(set(ids)), 'map Ids are unique')
    for n, m in maps.items():
        for k in ('Description', 'Author', 'Email', 'Id', 'Version', 'CSVPrefix', 'FileName', 'IdentifyQuery', 'IdentifyValue', 'Queries'):
            if k not in m:
                check(False, f'{n}: has {k}')
        check(isinstance(m['Version'], (int, float)), f'{n}: Version is numeric')
        check('YOUR_' not in yaml.safe_dump(m), f'{n}: no placeholder text')
        for q in m['Queries']:
            check(bool(q.get('Name')) and bool(q.get('BaseFileName')), f"{n}/{q.get('BaseFileName')}: Name and BaseFileName set")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sqlecmd', help='path to SQLECmd.exe to test the real tool as well')
    ap.add_argument('--upstream-maps', help='folder of upstream SQLECmd maps, for the cross-match test')
    ap.add_argument('--maps', default=MAPS, help='map folder to test (default: ./maps)')
    args = ap.parse_args()
    maps_dir = args.maps
    maps = load_maps(maps_dir)
    static_checks(maps)

    current = {(f, m) for f, m in [('calling.db', fixtures.M_CALL), ('photos.db', fixtures.M_PHOTO),
                                   ('phoneapps.db', fixtures.M_APPS), ('continuity.db', fixtures.M_CONT),
                                   ('deviceData.db', fixtures.M_DEV)]}
    engines = [('emulator', lambda d: run_emulator(d, maps))]
    if args.sqlecmd:
        engines.append(('sqlecmd', lambda d: run_sqlecmd(args.sqlecmd, d, maps_dir, maps)))
    for ename, eng in engines:
        print(f'== engine: {ename}')
        scenario('current', fixtures.build_current, eng, maps, current)
        scenario('edge-values', fixtures.build_current, eng, maps, current, edge=True)
        scenario('wal-only', fixtures.build_current, eng, maps, current, wal_only=True)
        scenario('legacy-photos', fixtures.build_legacy_photos, eng, maps, {('photos.db', fixtures.M_PHOTO)})
        scenario('negatives', fixtures.build_negatives, eng, maps, set())

    if args.upstream_maps:
        print('== cross-match against upstream maps')
        both = tempfile.mkdtemp(prefix='maps_all_')
        for p in glob.glob(os.path.join(args.upstream_maps, '*.smap')):
            shutil.copy(p, both)
        for p in glob.glob(os.path.join(maps_dir, '*.smap')):
            shutil.copy(p, both)
        allmaps = load_maps(both)
        tmp = tempfile.mkdtemp(prefix='pl_cross_')
        fixtures.build_current(tmp)
        res = run_emulator(tmp, allmaps)
        per_db = Counter(f for f, _ in res['matches'])
        check(all(v == 1 for v in per_db.values()) and len(per_db) == 5,
              f'each synthetic database matches exactly one map out of {len(allmaps)} ({dict(per_db)})')
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(both, ignore_errors=True)

    print(f'\nRESULT: {len(FAILS)} failure(s)')
    for f in FAILS:
        print('  -', f)
    sys.exit(1 if FAILS else 0)


if __name__ == '__main__':
    main()
