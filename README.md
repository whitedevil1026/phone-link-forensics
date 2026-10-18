# Phone Link forensics: SQLECmd maps and a relations viewer

One tool for the Microsoft Phone Link (formerly Your Phone) databases on Windows 11, in two parts:

- **`maps/`**: five [SQLECmd](https://github.com/EricZimmerman/SQLECmd) maps (one fix, four new) for running inside KAPE / SQLECmd.
- **`viewer/`**: a desktop viewer that shows every table, recovers deleted rows, and links rows to each other, inside each table and between the tables of one database. It also shows the SQLECmd map output. See [Relations viewer](#relations-viewer).

## The maps

Five SQLECmd maps for Microsoft Phone Link (formerly Your Phone) on Windows 11. The stock map set no longer parses some of these databases and has no map at all for others. Every map is tested against synthetic databases with a known answer key, on both an emulator of SQLECmd's matching rules and the real SQLECmd.

## Why these maps exist

- **`photos.db` is silently skipped by the stock map.** Current Phone Link builds no longer have the `photo` table, so `Windows_YourPhone_PhotosDB.smap` v1.1 stops matching and SQLECmd lists the database as having no map. v1.2 here matches by the columns its query actually reads.
- **Four databases have no map at all:**
  - `calling.db`: call history
  - `continuity.db`: phone browser pages and cross-device app handoff
  - `phoneapps.db`: installed and recent apps, with icons
  - `deviceData.db`: the phone's wallpaper

## The maps

| Map | Database | What you get |
|---|---|---|
| `Windows_YourPhone_PhotosDB.smap` (v1.2) | `photos.db` | Photo index with taken / seen / sync times; thumbnails exported as JPEG |
| `Windows_YourPhone_CallingDB.smap` | `calling.db` | Calls: number, direction, start time, duration, SIM, read flag, raw times |
| `Windows_YourPhone_ContinuityDB.smap` | `continuity.db` | Phone browser history offered to the PC; cross-device app context |
| `Windows_YourPhone_PhoneAppsDB.smap` | `phoneapps.db` | Installed apps (icons as PNG), recent apps, Android ↔ Windows app links |
| `Windows_YourPhone_DeviceDataDB.smap` | `deviceData.db` | The linked phone's wallpaper image |

Location: `%LOCALAPPDATA%\Packages\Microsoft.YourPhone_8wekyb3d8bbwe\LocalCache\Indexed\<device GUID>\System\Database\`

The existing stock maps for `contacts.db`, `phone.db`, `notifications.db` and `settings.db` still match current builds and are not duplicated here.

## Use

1. Copy the `.smap` files into SQLECmd's `Maps` folder, or point `--maps` at this repo's `maps` folder.
2. **Work on copies.** SQLECmd writes `-shm`/`-wal` files beside the databases it opens. Collect each `.db` together with its `-wal` and `-shm`, because recent rows can exist only in the `-wal`.
3. Run it:
   ```
   SQLECmd.exe -d <copied folder> --hunt --maps <maps folder> --csv <out>\csv --blobdir <out>\blobs
   ```

## Design rules the maps follow

- **A map only matches a database its queries can run on.** Every IdentifyQuery checks the exact columns the queries read, and that those tables are real tables (a planted view does not match).
- **Times are never invented.** A timestamp of 0, a negative value, text, or a value past year 9999 gives an empty time, never 1601-01-01. Raw columns keep the stored value as text.
- **Nothing is dropped or duplicated.** Joins to related tables are pre-grouped, so one source row gives exactly one output row.
- **Every blob is written, once.**
  - File names come from a unique key: `{app_id}_{package}` for icons, `MediaID` for thumbnails, the primary key elsewhere. SQLECmd otherwise cuts names to 50 characters and silently overwrites duplicates.
  - Blob columns are always real BLOB values, so SQLECmd's CSV columns stay aligned. With a NULL or text value in a blob column, SQLECmd shifts `SourceFile` out of place.
- **Unconfirmed epochs are labelled.** They are converted by size, with the raw value kept beside them.
- **Fast on large databases.** In a stress test, 1,000,000 calls, 500,000 photos and 20,000 apps with icons ran through SQLECmd in about 85 s.

## SQLECmd behaviours to know (not fixable in a map)

- It writes `-shm`/`-wal` files beside its input, so run it on copies.
- It skips a database that is locked, or held open by another process, and still exits with code 0.
- It ignores a `-wal` whose header doesn't match the database, without warning.
- It has no query timeout, so a crafted view can make it run forever.
- It converts a value whose stored type differs from the column's declared type: text in an INTEGER column becomes 0 or its leading number. The `*Raw` columns are cast to text so the stored value survives. Other plain columns (ranks, heights, task IDs) would show the converted value if a database ever held text there.
- CSV cells hold values exactly as stored, including leading `=` `+` `-` `@` and NUL characters. Open the CSVs as text or in Timeline Explorer, not by double-clicking them into Excel.

## Limitations

- Tested against one current Windows 11 build (October 2026) and one linked Android phone. Older or newer Phone Link builds may change the schema; the IdentifyQueries then fail to match rather than give wrong output.
- `continuity.db` and `pcapp_data` were empty on the test system, so their queries are tested on synthetic rows only, and their epochs are inferred by size.
- The main PhotosDB CSV does not flag which photos have a thumbnail; match the Thumbnails CSV on `MediaID`.
- iPhone pairings were not tested.

## Relations viewer

Python 3.8+, standard library only (Tkinter). Nothing to install.

```
python viewer/plmapview.py --demo                     # synthetic data, nothing real
python viewer/plmapview.py --input "<...\LocalCache\Indexed\<device GUID>\System\Database>"
python viewer/plmapview.py --input "%LOCALAPPDATA%\Packages\Microsoft.YourPhone_8wekyb3d8bbwe"
```

**Evidence handling:**
- The input folder is **copied** to a temporary folder (every `.db` with its `-wal` and `-shm`).
- Every copy is **re-hashed (SHA-256) against its source**, and a mismatch stops the load.
- The copies are opened **read-only** and deleted when the window closes.
- Deleted rows are **carved from the copy**, never from the source.

**Layout:** the window is organised **by database**. Open a database to see:
1. **Its tables:** packed data is unpacked. For example, a notification's `json` column becomes 36 columns.
2. **Relations inside one table:** rows that belong together, or data packed inside a row.
3. **Relations between the tables of that database:** joined by an ID column.

Select a row and the panel lists every linked row. It says in plain words how each one is linked and which table it is in; double-click a link to jump to it. Links **between different databases** (for example a call matched to a contact by number) are inferences, hidden until you tick *Show cross-database links*.

**notifications.db (first database done):**
- **Rows:** live rows plus rows carved from free pages (Source = deleted, with page and offset).
- **JSON:** every field becomes a column. `flags`, `importance`, `priority` and the Android `key` are decoded as the Android API defines them.
- **Sub-tables:** chat messages with their senders, and notification buttons (inline reply).
- **Relations inside the table:** same group, same conversation, several versions of one notification, and columns-vs-json consistency.
- **Relations between tables:** notification -> Windows toast; phone request -> notification (only where the IDs are identical); deleted row -> its live copy.
- **A caution:** `isGroup` in the json means "belongs to an app-defined group", not "is the group summary". That reading is measured on a small sample, so treat it as inferred.

The other databases already have relations between their tables (contacts, SMS threads / messages / participants / MMS / RCS, SIM, apps). Their relations inside each table are added one database at a time.

| File | What it is |
|---|---|
| `viewer/plmapview.py` | The viewer |
| `viewer/plrelate.py` | Relation engine: copies and hashes, entities, links, relation tables, SQLECmd map output |
| `viewer/plnotif.py` | notifications.db: live and carved rows, json unpacked and decoded, its relations |
| `viewer/plparse.py`, `viewer/plcarve.py` | Copy-first staging, address normalisation and time conversion; schema-guided SQLite carver |
| `viewer/pldemo.py` | A synthetic device folder with known relations, including deleted notifications on free pages |
| `viewer/maps-upstream/` | The four stock SQLECmd Phone Link maps, unchanged (authors in each file), so the viewer can show their output too |

## Tests

See [docs/TESTING.md](docs/TESTING.md). To run them:

```
pip install pyyaml yamllint
yamllint -c .yamllint maps
python tests/run_tests.py                                         # emulator, synthetic data
python tests/run_tests.py --sqlecmd C:\Tools\SQLECmd.exe           # plus the real SQLECmd
python tests/stress.py C:\Tools\SQLECmd.exe                        # large synthetic databases
python viewer/test_plrelate.py                                     # viewer: 32 relation tests, planted answers
python viewer/test_plmapview.py                                    # viewer: 7 GUI tests
```

## Related

Capability Access Manager (camera, microphone and location usage) maps are a separate repo: [sqlecmd-maps-capabilityaccessmanager](https://github.com/whitedevil1026/sqlecmd-maps-capabilityaccessmanager).

## Credits

`Windows_YourPhone_PhotosDB.smap` builds on Andrew Rathbun's original map. MIT licence.
