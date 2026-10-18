#!/usr/bin/env python3
"""
plcarve.py - schema-guided SQLite record carver for the Phone Link databases.

Recovers deleted rows from three kinds of dead space inside a .db file:

  1. FREE PAGES      - pages on the freelist. Whole pages, often still holding
                       an intact run of records.
  2. FREEBLOCKS      - gaps inside a live b-tree page, chained from the page
                       header. This is where a single deleted row usually lands.
  3. PAGE SLACK      - the unallocated gap between the cell-pointer array and
                       the start of cell content on a live page.

It also carves WAL frames, which hold page images the main file may no longer
have.

WHY SCHEMA-GUIDED. A deleted cell is unlinked, so there is no pointer to it.
Carving therefore means recognising a record by its own shape. A SQLite record
is self-describing: a header of serial types followed by a body whose length is
implied by those types. A candidate is accepted only when

    payload_size == header_size + sum(field sizes implied by the serial types)

and the field count equals a known table's column count. That equation is a
strong filter - random bytes satisfy it very rarely - but it is not proof, so
every carved row is emitted with the evidence needed to judge it: which file,
which page, which region kind, and the byte offset.

READ THIS BEFORE USING THE OUTPUT
  * A carved row has NO rowid guarantee and NO timestamp guarantee. The values
    are whatever survived; adjacent bytes may have been overwritten.
  * A row matching table X's column count may actually be from table Y with the
    same shape. The `table` column is the tool's best fit, not a fact.
  * Carving finds deleted AND live records in the regions it scans. Rows are
    de-duplicated against the live table where a rowid match is possible, and
    flagged `also_live` when they are not clearly deleted.
  * Phone Link re-syncs wholesale, so a carved row is more often sync churn than
    a user deleting something. Say that in the report.

Stdlib only. Opens evidence read-only, byte-wise; never through SQLite.
"""

import os, re, struct, sqlite3

# ------------------------------------------------------------------ varint
def varint(buf, off):
    """SQLite big-endian base-128 varint. Returns (value, new_offset) or (None, off)."""
    n = 0
    for i in range(9):
        if off + i >= len(buf):
            return (None, off)
        b = buf[off + i]
        if i == 8:
            n = (n << 8) | b
            return (n, off + 9)
        n = (n << 7) | (b & 0x7F)
        if not (b & 0x80):
            return (n, off + i + 1)
    return (None, off)


# serial type -> byte length
_FIXED = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8, 7: 8, 8: 0, 9: 0}


def serial_len(t):
    if t in _FIXED:
        return _FIXED[t]
    if t in (10, 11):
        return None                 # reserved - never valid in a database file
    return (t - 12) // 2 if t % 2 == 0 else (t - 13) // 2


def decode_value(buf, off, t):
    ln = serial_len(t)
    if ln is None or off + ln > len(buf):
        return (None, off)
    if t == 0:
        return (None, off)
    if t == 8:
        return (0, off)
    if t == 9:
        return (1, off)
    if t in (1, 2, 3, 4, 5, 6):
        v = int.from_bytes(buf[off:off + ln], "big", signed=True)
        return (v, off + ln)
    if t == 7:
        return (struct.unpack(">d", buf[off:off + ln])[0], off + ln)
    raw = buf[off:off + ln]
    if t % 2 == 1:                  # TEXT
        try:
            return (raw.decode("utf-8"), off + ln)
        except UnicodeDecodeError:
            return (raw.decode("utf-8", "replace"), off + ln)
    return (raw, off + ln)          # BLOB


# ------------------------------------------------------------------ regions
def db_page_size(data):
    if len(data) < 100 or data[:16] != b"SQLite format 3\x00":
        return None
    ps = struct.unpack(">H", data[16:18])[0]
    return 65536 if ps == 1 else ps


def free_pages(data, ps):
    """Walk the freelist trunk chain from the file header."""
    out = set()
    if len(data) < 40:
        return out
    trunk = struct.unpack(">I", data[32:36])[0]
    seen = set()
    while trunk and trunk not in seen:
        seen.add(trunk)
        off = (trunk - 1) * ps
        if off + 8 > len(data):
            break
        out.add(trunk)
        nxt, cnt = struct.unpack(">II", data[off:off + 8])
        cnt = min(cnt, (ps - 8) // 4)
        for i in range(cnt):
            p = struct.unpack(">I", data[off + 8 + i * 4: off + 12 + i * 4])[0]
            if p:
                out.add(p)
        trunk = nxt
    return out


def page_regions(data, ps, pno, freelist, hdr_offset=None):
    """
    Yield (kind, absolute_offset, length) of every dead-space region on a page.

    hdr_offset lets a caller override the 100-byte file-header skip. It matters for
    WAL frames: a frame is a standalone page image, so it sits at buffer offset 0
    regardless of which page number it holds. Passing pno=1 to get base=0 would
    otherwise also apply the file-header skip and misread the page header.
    """
    base = (pno - 1) * ps
    if base + ps > len(data):
        return
    if pno in freelist:
        yield ("free-page", base + 8, ps - 8)
        return
    hdr = base + ((100 if pno == 1 else 0) if hdr_offset is None else hdr_offset)
    if hdr + 8 > len(data):
        return
    ptype = data[hdr]
    if ptype not in (0x02, 0x05, 0x0A, 0x0D):
        return                                        # not a b-tree page
    ncells = struct.unpack(">H", data[hdr + 3:hdr + 5])[0]
    content = struct.unpack(">H", data[hdr + 5:hdr + 7])[0]
    if content == 0:
        content = 65536
    hlen = 12 if ptype in (0x02, 0x05) else 8
    ptr_end = hdr + hlen + ncells * 2
    cstart = base + content
    if cstart > ptr_end and cstart - ptr_end > 8:     # unallocated slack
        yield ("page-slack", ptr_end, cstart - ptr_end)
    fb = struct.unpack(">H", data[hdr + 1:hdr + 3])[0]  # freeblock chain
    guard = 0
    while fb and guard < 512:
        guard += 1
        o = base + fb
        if o + 4 > len(data):
            break
        nxt, sz = struct.unpack(">HH", data[o:o + 4])
        if sz >= 4:
            yield ("freeblock", o + 4, sz - 4)
        fb = nxt


def wal_frames(path, ps):
    """Yield (frame_index, page_number, page_bytes) from a -wal file."""
    if not os.path.isfile(path):
        return
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 32:
        return
    magic, ver, fps = struct.unpack(">III", data[0:12])
    if magic not in (0x377F0682, 0x377F0683):
        return
    if fps:
        ps = fps
    off, i = 32, 0
    while off + 24 + ps <= len(data):
        pno = struct.unpack(">I", data[off:off + 4])[0]
        yield (i, pno, data[off + 24: off + 24 + ps])
        off += 24 + ps
        i += 1


# ------------------------------------------------------------------ carving
PLUMBING = {"_change_notifications", "content_sequence"}


def affinity(decl):
    d = (decl or "").upper()
    if "INT" in d:
        return "int"
    if "BLOB" in d:
        return "blob"
    if any(k in d for k in ("CHAR", "CLOB", "TEXT")):
        return "text"
    return "any"


def type_fits(serial, aff):
    """Does a serial type look like it belongs to a column of this affinity?"""
    if serial == 0:
        return True                       # NULL fits anywhere
    if aff == "int":
        return serial in (1, 2, 3, 4, 5, 6, 8, 9)
    if aff == "text":
        return serial >= 13 and serial % 2 == 1
    if aff == "blob":
        return serial >= 12 and serial % 2 == 0
    return True


def table_shapes(db_path, include_plumbing=False):
    """Column names AND affinities per table, straight from the live schema."""
    shapes = {}
    try:
        c = sqlite3.connect("file:" + db_path.replace("\\", "/") + "?mode=ro", uri=True)
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            if name.startswith("sqlite_") or name.startswith("fts_"):
                continue
            info = list(c.execute('PRAGMA table_info("%s")' % name))
            if info:
                shapes.setdefault(len(info), []).append(
                    (name, [r[1] for r in info], [affinity(r[2]) for r in info]))
        c.close()
    except sqlite3.Error:
        pass
    return shapes


def carve_region(buf, start, length, shapes, ps, src, pno, kind, min_text=1):
    """Scan one dead-space region for records whose self-description is consistent."""
    out = []
    end = min(start + length, len(buf))
    i = start
    while i < end:
        psz, o1 = varint(buf, i)
        if psz is None or psz < 4 or psz > ps * 8:
            i += 1
            continue
        rowid, o2 = varint(buf, o1)
        if rowid is None or rowid < 0:
            i += 1
            continue
        hsz, o3 = varint(buf, o2)
        if hsz is None or hsz < 2 or hsz > psz or o2 + psz > len(buf):
            i += 1
            continue
        # decode the serial-type array
        types, p = [], o3
        bad = False
        while p < o2 + hsz:
            t, p = varint(buf, p)
            if t is None or t in (10, 11):
                bad = True
                break
            types.append(t)
        if bad or p != o2 + hsz or not types:
            i += 1
            continue
        body = sum((serial_len(t) or 0) for t in types)
        # THE FILTER: the record must account for exactly its own payload length
        if hsz + body != psz:
            i += 1
            continue
        cand = shapes.get(len(types))
        if not cand:
            i += 1
            continue
        vals, q = [], o2 + hsz
        for t in types:
            v, q = decode_value(buf, q, t)
            vals.append(v)
        texts = [v for v in vals if isinstance(v, str) and v.strip()]
        if len(texts) < min_text:
            i += 1
            continue
        # Column count alone is ambiguous. Score each candidate table by how well
        # the observed serial types match its declared column affinities.
        scored = []
        for nm, cols_, affs in cand:
            hits = sum(1 for t, af in zip(types, affs) if type_fits(t, af))
            scored.append((hits / float(len(types)), nm, cols_))
        scored.sort(key=lambda x: -x[0])
        fit, tname, cols = scored[0]
        if fit < 0.6:                     # shape does not really look like any table
            i += 1
            continue
        rec = {"table": tname, "type_fit": round(fit, 2),
               "candidates": "|".join("%s:%.2f" % (n, f) for f, n, _ in scored[:3]),
               "source_file": src, "page": pno, "region": kind,
               "offset": i, "rowid": rowid, "cols": len(types)}
        for cn, v in zip(cols, vals):
            if isinstance(v, bytes):
                rec[cn] = "<blob %d bytes>" % len(v)
            elif isinstance(v, str):
                rec[cn] = v.replace("\r", " ").replace("\n", " ")
            else:
                rec[cn] = v
        out.append(rec)
        i = o2 + psz
    return out


def live_rowids(db_path, table):
    try:
        c = sqlite3.connect("file:" + db_path.replace("\\", "/") + "?mode=ro", uri=True)
        ids = {r[0] for r in c.execute('SELECT rowid FROM "%s"' % table)}
        c.close()
        return ids
    except sqlite3.Error:
        return set()


def carve_db(db_path, include_wal=True, min_text=1, include_plumbing=False):
    """Carve one database. Returns (records, stats)."""
    with open(db_path, "rb") as fh:
        data = fh.read()
    ps = db_page_size(data)
    stats = {"file": os.path.basename(db_path), "page_size": ps,
             "pages": 0, "free_pages": 0, "regions": 0, "wal_frames": 0, "records": 0}
    if not ps:
        return ([], stats)
    shapes = table_shapes(db_path)   # match against ALL tables, incl. plumbing
    npages = len(data) // ps
    fl = free_pages(data, ps)
    stats["pages"], stats["free_pages"] = npages, len(fl)

    recs = []
    for pno in range(1, npages + 1):
        for kind, off, ln in page_regions(data, ps, pno, fl):
            stats["regions"] += 1
            recs += carve_region(data, off, ln, shapes, ps,
                                 os.path.basename(db_path), pno, kind, min_text)

    if include_wal:
        wp = db_path + "-wal"
        for idx, pno, page in wal_frames(wp, ps):
            stats["wal_frames"] += 1
            # A frame is a standalone page image at buffer offset 0, so walk it as
            # page 1 (base=0) but apply the file-header skip only if it really IS
            # page 1. Getting this wrong misreads every frame's page header.
            hdr_off = 100 if pno == 1 else 0
            for kind, off, ln in page_regions(page, ps, 1, set(), hdr_offset=hdr_off):
                recs += carve_region(page, off, ln, shapes, ps,
                                     os.path.basename(wp), pno, "wal-" + kind, min_text)

    # Plumbing tables are matched (so their records are not mislabelled as some
    # other table of the same width) but dropped here unless explicitly wanted.
    if not include_plumbing:
        recs = [r for r in recs if r["table"] not in PLUMBING]

    # de-duplicate, and mark anything whose rowid is still live
    seen, uniq = set(), []
    live_cache = {}
    for r in recs:
        key = (r["table"], r["rowid"], tuple(sorted((k, str(v)) for k, v in r.items()
                                                    if k not in ("page", "offset", "region", "source_file"))))
        if key in seen:
            continue
        seen.add(key)
        t = r["table"]
        if t not in live_cache:
            live_cache[t] = live_rowids(db_path, t)
        r["status"] = "also_live" if r["rowid"] in live_cache[t] else "deleted_or_overwritten"
        uniq.append(r)
    stats["records"] = len(uniq)
    stats["deleted_only"] = sum(1 for r in uniq if r["status"] != "also_live")
    return (uniq, stats)


if __name__ == "__main__":
    import argparse, csv, sys, collections
    ap = argparse.ArgumentParser(description="Schema-guided SQLite carver for Phone Link databases")
    ap.add_argument("db", nargs="+", help="one or more .db files")
    ap.add_argument("--out", help="write carved_<db>_<table>.csv into this directory")
    ap.add_argument("--min-text", type=int, default=1, help="require at least N non-empty text fields")
    ap.add_argument("--no-wal", action="store_true")
    ap.add_argument("--include-plumbing", action="store_true",
                    help="also carve _change_notifications / content_sequence (noisy churn tables)")
    a = ap.parse_args()
    total = 0
    for p in a.db:
        recs, st = carve_db(p, include_wal=not a.no_wal, min_text=a.min_text,
                            include_plumbing=a.include_plumbing)
        print("%-20s pages=%-6s free=%-4s regions=%-5s wal=%-5s carved=%-5s deleted-only=%s"
              % (st["file"], st["pages"], st["free_pages"], st["regions"],
                 st["wal_frames"], st["records"], st.get("deleted_only", 0)))
        by = collections.Counter(r["table"] for r in recs)
        for t, n in by.most_common():
            print("      %-26s %d" % (t, n))
        total += len(recs)
        if a.out and recs:
            os.makedirs(a.out, exist_ok=True)
            groups = collections.defaultdict(list)
            for r in recs:
                groups[r["table"]].append(r)
            for t, rows in groups.items():
                cols, seen = [], set()
                for r in rows:
                    for k in r:
                        if k not in seen:
                            seen.add(k); cols.append(k)
                fn = os.path.join(a.out, "carved_%s_%s.csv" %
                                  (os.path.splitext(os.path.basename(p))[0], t))
                with open(fn, "w", newline="", encoding="utf-8-sig") as fh:
                    w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
                    w.writeheader()
                    ctrl = re.compile("[" + "".join(chr(c) for c in list(range(0, 9)) + [11, 12] + list(range(14, 32))) + "]")
                    for r in rows:
                        w.writerow({k: (ctrl.sub(chr(0xFFFD), v) if isinstance(v, str) else v)
                                    for k, v in r.items()})
    print("\ntotal carved: %d" % total)
