"""
plmapview.py - Phone Link relations viewer.

Shows the SQLECmd map output for a Phone Link device folder, every table as an
entity list, and 25 relations between the tables. Select a row to see every row
it links to; double-click a link to jump to that row in its own table.

Works on COPIES only (staged by plrelate / plparse, hash-verified, read-only).
Standard library only.

    python plmapview.py --input "<...\\LocalCache\\Indexed\\<GUID>\\System\\Database>"
    python plmapview.py --input "%LOCALAPPDATA%\\Packages\\Microsoft.YourPhone_8wekyb3d8bbwe"
    python plmapview.py --demo                  # synthetic data, nothing real
"""
import argparse
import csv
import os
import re
import sys
import tempfile

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover
    sys.exit("tkinter is not available in this Python")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plparse as P  # noqa: E402
import plconnect as C  # noqa: E402
import pldevices as D  # noqa: E402
import plrelate as R  # noqa: E402

BG, PANEL, INK, MUTED = "#f4f7fa", "#16293f", "#13233a", "#667c93"
KIND = {  # kind -> (heading, colour, plain meaning)
    "pc": ("Linked phones (this PC)", "#5d4037", "Every phone ever linked to this PC, from the package-level files"),
    "conn": ("Phone connections - USB cable and Bluetooth (this PC)", "#37474f",
             "Read live, read-only, from THIS computer's registry, setup logs and event logs - not from the evidence "
             "folder"),
    "table": ("Table", "#1e3a56", "Rows of one table"),
    "in-table": ("Inside one table", "#2e7d32", "Rows of the SAME table that belong together, or data packed inside a row"),
    "in-db": ("Between tables of one database", "#00897b", "Tables in the SAME database file joined by an ID column"),
    "check": ("Checks", "#c62828", "Rows whose partner is missing, or counts that disagree"),
    "cross": ("Across databases", "#e08a00", "Joins between DIFFERENT database files - inferences, hidden by default"),
    "map": ("SQLECmd map output", "#6a1b9a", "Exactly what the SQLECmd maps return"),
    "provenance": ("Provenance", "#546e7a", "Where the copies came from and their hashes"),
}
# ---------------------------------------------------------------- the view list (left column)
# Plain-English names for the view list. The technical name (view code, file, table) is shown in the header
# when a view is opened, so nothing is lost. Views not listed here fall back to a group chosen from their
# database and kind, under their engine title.
GROUPS = [  # id, heading, open when the window opens
    ("notifications", "Notifications", True),
    ("messages", "Text messages (SMS, MMS, chat)", False),
    ("calls", "Calls", False),
    ("contacts", "Contacts", False),
    ("photos", "Photos", False),
    ("apps", "Apps", False),
    ("browser", "Browser, handoff and settings", False),
    ("overview", "Overview and checks", False),
    ("linked", "Phones linked to this PC", False),
    ("conn", "USB and Bluetooth on this PC", False),
    ("advanced", "Advanced", False),
]
SUBGROUPS = {  # sub-heading inside a group; folded away until opened
    "together": "Rows that belong together",
    "links": "Links to other tables",
    "inferred": "Matched from other files (inferred)",
    "between": "How these connect",
    "maps": "SQLECmd map output",
    "hashes": "Copies and hashes",
}
NAV = {  # view -> (group, sub-heading or None, name in the list)
    # notifications.db
    "N_notifications": ("notifications", None, "All notifications (live and deleted)"),
    "N_messages": ("notifications", None, "Chat messages inside notifications"),
    "N_actions": ("notifications", None, "Buttons on notifications"),
    "N_toasts": ("notifications", None, "Windows pop-ups (toasts)"),
    "N_requests": ("notifications", None, "Requests sent from the phone"),
    "NR1": ("notifications", "together", "Same notification group"),
    "NR2": ("notifications", "together", "Same chat conversation"),
    "NR3": ("notifications", "together", "Earlier versions of a notification"),
    "NR5": ("notifications", "together", "Each notification and its messages"),
    "NR6": ("notifications", "together", "Each notification and its buttons"),
    "ND1": ("notifications", "links", "Notification \u2192 Windows pop-up"),
    "ND2": ("notifications", "links", "Phone request \u2192 notification"),
    "ND3": ("notifications", "links", "Deleted rows and their live copy"),
    # phone.db
    "E_threads": ("messages", None, "Conversations"),
    "E_sms": ("messages", None, "Text messages (SMS)"),
    "E_mms": ("messages", None, "Picture messages (MMS)"),
    "E_rcs": ("messages", None, "Chat conversations (RCS)"),
    "E_participants": ("messages", None, "People in conversations"),
    "E_sims": ("messages", None, "SIM cards"),
    "R03": ("messages", "links", "Conversation \u2192 its messages"),
    "R04": ("messages", "links", "Conversation \u2192 its people"),
    "R05": ("messages", "links", "Picture message \u2192 attachments"),
    "R06": ("messages", "links", "Chat \u2192 messages and files"),
    "R07": ("messages", "links", "Chat \u2192 matching SMS conversation"),
    "R08": ("messages", "links", "Pinned, muted or hidden conversations"),
    "R09": ("messages", "links", "Messages waiting to be sent"),
    "R10": ("messages", "links", "Text message \u2192 SIM card"),
    "R15": ("messages", "inferred", "Text message \u2192 contact"),
    "R16": ("messages", "inferred", "Conversation \u2192 contacts"),
    "R17": ("messages", "inferred", "Person in a conversation \u2192 contact"),
    # calling.db
    "E_calls": ("calls", None, "All calls"),
    "R13": ("calls", "inferred", "Call \u2192 contact"),
    "R14": ("calls", "inferred", "Call \u2192 SIM card"),
    # contacts.db
    "E_contacts": ("contacts", None, "All contacts"),
    "R01": ("contacts", "links", "Contact \u2192 numbers, emails and addresses"),
    "R02": ("contacts", "links", "Deleted contact details"),
    # photos.db
    "E_photos": ("photos", None, "All photos"),
    "R20": ("photos", "inferred", "Photos taken during a call"),
    # phoneapps.db
    "E_apps": ("apps", None, "Apps on the phone"),
    "E_recent": ("apps", None, "Recently used apps"),
    "E_pcapp": ("apps", None, "Matching apps on this PC"),
    "R11": ("apps", "links", "App \u2192 recent use and PC match"),
    "R19": ("apps", "inferred", "Notification, setting or web page \u2192 app"),
    # continuity.db, settings.db
    "E_browser": ("browser", None, "Web pages opened on the phone"),
    "E_handoff": ("browser", None, "Apps continued on the PC (handoff)"),
    "E_settings": ("browser", None, "Phone Link settings"),
    # across the whole phone
    "R22": ("overview", None, "Timeline \u2013 everything in time order"),
    "R18": ("overview", None, "People \u2013 calls and texts per person"),
    "R21": ("overview", None, "Things that happened close together"),
    "R24": ("overview", None, "Unknown numbers and quiet contacts"),
    "R23": ("overview", None, "Check: rows whose partner is missing"),
    "R25": ("overview", None, "Check: is every feed fully synced?"),
    "NR4": ("overview", None, "Check: notification columns vs stored data"),
    # this PC: linked phones
    "PC_devices": ("linked", None, "Phone pairings"),
    "PC_phones": ("linked", None, "Phone details (make, model, Android)"),
    "PC_trust": ("linked", None, "Trusted connections"),
    "PC_streamed": ("linked", None, "Apps streamed to this PC"),
    "PR4": ("linked", None, "Pairing history"),
    "PR1": ("linked", "between", "Phone \u2192 trusted connection"),
    "PR2": ("linked", "between", "Pairing \u2192 streamed apps"),
    "PR3": ("linked", "between", "Phone \u2192 pairing (matched by time)"),
    # this PC: cable and Bluetooth
    "C_phones": ("conn", None, "Phones connected (summary)"),
    "C_usb": ("conn", None, "USB devices"),
    "C_wpd": ("conn", None, "Phones and cameras (MTP)"),
    "C_bt": ("conn", None, "Bluetooth devices"),
    "C_btsvc": ("conn", None, "Bluetooth services"),
    "C_btle": ("conn", None, "Bluetooth Low Energy devices"),
    "C_autoplay": ("conn", None, "AutoPlay devices"),
    "C_install": ("conn", None, "First time connected (driver install)"),
    "C_events": ("conn", None, "Connection events"),
    "CR1": ("conn", None, "Same device seen in several places"),
    # advanced
    "P_copies": ("advanced", "hashes", "Evidence copies and hashes"),
    "PC_copies": ("advanced", "hashes", "Linked-phone copies and hashes"),
}
MAP_NAMES = {  # SQLECmd map BaseFileName -> name in the list
    "CallingDB_CallHistory": "Calls", "ContactsDB": "Contacts", "PhotosDB": "Photos",
    "PhotosDB_Thumbnails": "Photo thumbnails", "PhotosDB_MediaBlobs": "Photo data",
    "PhoneDB_SMSMessages": "Text messages", "PhoneDB_SubscriptionInfo": "SIM cards",
    "NotificationsDB": "Notifications", "PhoneAppsDB_InstalledApps": "Installed apps",
    "PhoneAppsDB_RecentApps": "Recent apps", "PhoneAppsDB_PCAppData": "Matching PC apps",
    "ContinuityDB_BrowserHistory": "Browser history", "ContinuityDB_ContinueAppContext": "Handoff",
    "DeviceDataDB_Wallpaper": "Wallpaper", "SettingsDB": "Settings",
}
GROUP_SHORT = {"messages": "Messages", "browser": "Browser", "overview": "Overview", "linked": "Linked phones",
               "conn": "USB / Bluetooth"}   # for the narrow "Found in" column
DB_GROUP = {"notifications.db": "notifications", "phone.db": "messages", "calling.db": "calls",
            "contacts.db": "contacts", "photos.db": "photos", "phoneapps.db": "apps",
            "continuity.db": "browser", "settings.db": "browser", "deviceData.db": "browser"}
KIND_SUB = {"in-table": "together", "in-db": "links", "cross": "inferred"}
BADGE = {"table": "DATA", "in-table": "ROWS THAT BELONG TOGETHER", "in-db": "LINKED TABLES",
         "cross": "INFERRED MATCH", "check": "CHECK", "map": "SQLECMD MAP", "provenance": "HASHES",
         "pc": "THIS PC", "conn": "THIS PC"}


def nav_place(v, window_min=None):
    """(group, sub-heading, name) for a view - its plain-English place in the view list."""
    if v.vid in NAV:
        group, sub, name = NAV[v.vid]
        if v.vid == "R21" and window_min:
            name = f"Things within {window_min} min of each other"
        return group, sub, name
    if v.kind == "map":
        base = v.vid[2:]
        return "advanced", "maps", MAP_NAMES.get(base, base.replace("_", " "))
    name = re.sub(r"^[A-Z]{1,3}\d+\s+", "", v.title)
    if v.kind == "provenance":
        return "advanced", "hashes", name
    if v.kind in ("pc", "conn"):
        return {"pc": "linked", "conn": "conn"}[v.kind], None, name
    if v.kind == "check":
        return "overview", None, "Check: " + name
    group = DB_GROUP.get(v.db or "", "overview")
    return group, KIND_SUB.get(v.kind) if group != "overview" else None, name


SCOPE_WORDS = {"in-table": "same table", "in-db": "other table, same database", "check": "check",
               "cross": "other database (inferred)"}
WIDE = {"Title", "Text", "BigText", "BigTitle", "SubText", "TickerText", "Message", "Body", "Row", "Notification",
        "Deleted", "LiveCopy", "Payload", "Key", "GroupKey", "What", "Flags", "First", "Second", "Value", "Check"}
# relation-table column -> entity kind, so rows without their own identity can still link
COL_ENTITY = {"CallID": "call", "ContactID": "contact", "ThreadID": "thread", "MediaID": "photo",
              "ParticipantID": "participant", "RCSThreadID": "rcsthread", "MMSID": "mms", "Package": "app"}
# MessageID means an SMS only in these relations (in R06 it is an RCS chat id)
VIEW_COL_ENTITY = {vid: {"MessageID": "sms"} for vid in ("R03", "R09", "R10", "R15")}


def _dpi_aware():
    """Crisp text on scaled Windows displays, and window coordinates that match the screen."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass


class Viewer(tk.Tk):
    def __init__(self, devices, window_min=R.DEFAULT_WINDOW_MIN, initial=None, localcache=None, connections=None,
                 redact=False):
        _dpi_aware()
        super().__init__()
        self.devices = devices
        self.localcache = localcache
        self.redact = redact  # demo / screenshots: never show this computer's real paths
        self.connections = C.CachedSource(connections) if connections is not None else None
        self.pc = None
        self.leftovers = []
        self.window_min = tk.IntVar(value=window_min)
        self.ev = self.g = None
        self.show_cross = tk.BooleanVar(value=False)
        self.cur_vid = None
        self.cur_rows = []
        self.sort_state = {}
        self.title("Phone Link Relations Viewer")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(1600, int(sw * 0.92)), min(960, int(sh * 0.88))
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 3)}")
        self.basis_wrap = w - 400
        self.configure(bg=BG)
        self._style()
        self._layout()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.load_device(0, initial)

    # ------------------------------------------------------------ layout
    def _style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Treeview", rowheight=22, font=("Segoe UI", 9))
        st.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        st.configure("Nav.Treeview", rowheight=22, font=("Segoe UI", 9))

    def _layout(self):
        bar = tk.Frame(self, bg=PANEL, height=52)
        bar.pack(fill="x")
        tk.Label(bar, text="Phone Link Relations Viewer", bg=PANEL, fg="white",
                 font=("Cambria", 15, "bold")).pack(side="left", padx=14, pady=10)
        self.src_lbl = tk.Label(bar, text="", bg=PANEL, fg="#9fb3c8", font=("Segoe UI", 9))
        self.src_lbl.pack(side="left", padx=8)
        tk.Button(bar, text="Export view (CSV)", command=self.export, relief="flat", bg="#2ec4b6", fg="white",
                  padx=10).pack(side="right", padx=8)
        tk.Button(bar, text="Open folder...", command=self.open_folder, relief="flat", bg="#1e3a56", fg="white",
                  padx=10).pack(side="right", padx=4)
        tk.Checkbutton(bar, text="Show inferred matches", variable=self.show_cross, command=self.fill_nav,
                       bg=PANEL, fg="#9fb3c8", selectcolor=PANEL, activebackground=PANEL).pack(side="right", padx=12)
        tk.Label(bar, text="Nearby (min):", bg=PANEL, fg="#9fb3c8").pack(side="right", padx=(12, 2))
        sp = tk.Spinbox(bar, from_=1, to=1440, width=5, textvariable=self.window_min, command=self.rebuild)
        sp.bind("<Return>", lambda e: self.rebuild())
        sp.pack(side="right")
        self.dev_combo = ttk.Combobox(bar, state="readonly", width=38,
                                      values=[f"{lbl}" for lbl, _ in self.devices])
        self.dev_combo.bind("<<ComboboxSelected>>", lambda e: self.load_device(self.dev_combo.current()))
        if len(self.devices) > 1:
            tk.Label(bar, text="Device:", bg=PANEL, fg="#9fb3c8").pack(side="right", padx=(12, 2))
            self.dev_combo.pack(side="right")

        body = tk.PanedWindow(self, orient="horizontal", sashwidth=4, bg=BG)
        body.pack(fill="both", expand=True)
        left = tk.Frame(body, bg="white")
        body.add(left, minsize=260, width=430)
        tk.Label(left, text="CONTENTS", bg="white", fg=MUTED, font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=12,
                                                                                               pady=(10, 2))
        navbox = tk.Frame(left, bg="white")
        navbox.pack(fill="both", expand=True, padx=6, pady=6)
        self.nav = ttk.Treeview(navbox, columns=("rows",), show="tree", style="Nav.Treeview", selectmode="browse")
        self.nav.column("#0", width=300, stretch=True)
        self.nav.column("rows", width=64, anchor="e", stretch=False)   # row count, right-aligned
        nav_scroll = ttk.Scrollbar(navbox, orient="vertical", command=self.nav.yview)
        self.nav.configure(yscrollcommand=nav_scroll.set)
        nav_scroll.pack(side="right", fill="y")
        self.nav.pack(side="left", fill="both", expand=True)
        self.nav.bind("<<TreeviewSelect>>", self.on_nav)
        self.nav.tag_configure("group", foreground=INK, font=("Segoe UI", 10, "bold"))
        self.nav.tag_configure("sub", foreground=MUTED, font=("Segoe UI", 9, "italic"))
        self.nav.tag_configure("item", foreground=INK)
        self.nav.tag_configure("inferred", foreground=KIND["cross"][1])
        self.nav.tag_configure("empty", foreground="#a0aebd")   # a view with no rows

        right = tk.PanedWindow(body, orient="vertical", sashwidth=4, bg=BG)
        body.add(right)
        top = tk.Frame(right, bg=BG)
        right.add(top, minsize=300)
        head = tk.Frame(top, bg=BG)
        head.pack(fill="x", padx=12, pady=(10, 0))
        self.title_lbl = tk.Label(head, text="", bg=BG, fg=INK, font=("Cambria", 14, "bold"), anchor="w")
        self.title_lbl.pack(side="left")
        self.kind_lbl = tk.Label(head, text="", fg="white", font=("Segoe UI", 8, "bold"), padx=8)
        self.kind_lbl.pack(side="left", padx=10)
        tk.Label(head, text="Filter:", bg=BG, fg=MUTED).pack(side="left", padx=(20, 4))
        self.filter_var = tk.StringVar()
        self._quiet = False      # True while show() clears the filter itself, so the table is filled once
        self.filter_var.trace_add("write", lambda *a: None if self._quiet else self.populate())
        tk.Entry(head, textvariable=self.filter_var, width=30).pack(side="left")
        self.count_lbl = tk.Label(head, text="", bg=BG, fg=MUTED)
        self.count_lbl.pack(side="left", padx=10)
        self.basis_lbl = tk.Label(top, text="", bg=BG, fg=INK, anchor="w", justify="left", wraplength=self.basis_wrap,
                                  font=("Segoe UI", 9, "italic"))
        self.basis_lbl.pack(fill="x", padx=12, pady=(4, 6))
        wrap = tk.Frame(top, bg=BG)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self.table = ttk.Treeview(wrap, show="headings", selectmode="browse")
        ys = ttk.Scrollbar(wrap, orient="vertical", command=self.table.yview)
        xs = ttk.Scrollbar(wrap, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.table.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.table.bind("<<TreeviewSelect>>", self.on_row)

        bottom = tk.PanedWindow(right, orient="horizontal", sashwidth=4, bg=BG)
        right.add(bottom, minsize=180)
        dframe = tk.Frame(bottom, bg=BG)
        bottom.add(dframe, minsize=300)
        tk.Label(dframe, text="SELECTED ROW", bg=BG, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=12)
        self.detail = tk.Text(dframe, height=9, wrap="word", bg="white", fg=INK, relief="flat",
                              font=("Segoe UI", 9), padx=10, pady=6)
        self.detail.pack(fill="both", expand=True, padx=12, pady=(2, 8))
        lframe = tk.Frame(bottom, bg=BG)
        bottom.add(lframe, minsize=420)
        tk.Label(lframe, text="LINKED ROWS  (double-click to jump)", bg=BG, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=6)
        self.links = ttk.Treeview(lframe, columns=("how", "target", "where", "scope"), show="headings", height=8)
        for c, w, t in (("target", 280, "Linked row"), ("where", 300, "Found in"), ("how", 380, "How it is linked"),
                        ("scope", 170, "Scope")):
            self.links.heading(c, text=t)
            self.links.column(c, width=w, minwidth=80, anchor="w", stretch=False)
        self.links["displaycolumns"] = ("target", "where", "how", "scope")   # what it is and where, first
        for k, (_, colour, _) in KIND.items():
            self.links.tag_configure(k, foreground=colour)
        links_x = ttk.Scrollbar(lframe, orient="horizontal", command=self.links.xview)
        self.links.configure(xscrollcommand=links_x.set)
        links_x.pack(side="bottom", fill="x", padx=6)
        self.links.pack(fill="both", expand=True, padx=6, pady=(2, 4))
        self.links.bind("<Double-1>", self.on_jump)
        self.link_targets = {}

        self.status = tk.Label(self, text="", bg=PANEL, fg="#9fb3c8", anchor="w", font=("Segoe UI", 8), padx=12)
        self.status.pack(fill="x", side="bottom")

    # ------------------------------------------------------------ data
    def load_device(self, i, initial=None):
        if self.ev:
            self._remember(self.ev.close())
        label, path = self.devices[i]
        self.dev_combo.current(i)
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            self.ev = R.Evidence(path)
            self.g = R.build(self.ev, self.window_min.get())
            self._add_linked_phones()
        except Exception as e:  # report and keep the window usable
            self.config(cursor="")
            messagebox.showerror("Could not load", f"{path}\n\n{type(e).__name__}: {e}")
            return
        self.config(cursor="")
        self.src_lbl.config(text=f"{label}  -  {self._shown(path)}")
        ok = sum(v["copy_matches"] == "yes" for v in self.ev.verify)
        self.status.config(text=f"Read-only COPIES in {self._shown(self.ev.work)}  |  {ok}/{len(self.ev.verify)} copies match "
                                f"their source SHA-256  |  the source folder is never opened by SQLite  |  "
                                f"{len(self.g.relations)} relations, "
                                f"{sum(len(x) for x in self.g.edges.values()) // 2:,} links")
        old = [] if self.redact else stale_copies(exclude=[self.ev.work] + ([self.pc.work] if self.pc else []))
        if old:
            self.status.config(text=self.status.cget("text") + f"  |  {len(old)} leftover copy folder(s) from earlier "
                                                                 f"runs in {self._shown(os.path.dirname(old[0]))} - "
                                                                 f"remove when done")
        self.fill_nav()
        self.show(initial or self.cur_vid or "N_notifications")

    def rebuild(self):
        if not self.ev:
            return
        try:
            self.g = R.build(self.ev, int(self.window_min.get()))
        except (ValueError, tk.TclError):
            return
        self._add_linked_phones()
        self.fill_nav()
        self.show(self.cur_vid)

    def _add_linked_phones(self):
        """Package-level views: every phone ever linked to this PC (copied and verified separately)."""
        if self.pc:
            self._remember(self.pc.close())
            self.pc = None
        try:
            _, self.pc = D.build(self.localcache, self.g, R.View, R.ts_seconds, R.iso)
        except (OSError, ValueError) as e:
            self.g.add_view(R.View("PC_devices", "Phone pairings (device folders)", "pc", f"could not read: {e}",
                                   ["Error"], [{"Error": str(e)}]))
        if self.connections is not None:
            try:
                C.build(self.connections, self.g, R.View)
            except OSError as e:
                self.g.add_view(R.View("C_phones", "Phone connections", "conn", f"could not read: {e}", ["Error"],
                                       [{"Error": str(e)}]))

    def _shown(self, path):
        return "(synthetic demo folder)" if self.redact else path

    def _remember(self, leftover):
        if leftover:
            self.leftovers.append(leftover)

    def fill_nav(self):
        """The view list: topic groups in plain English, row counts on the right, sub-headings folded away."""
        opened = {i for i in self._all_nav_ids() if self.nav.item(i, "open")} if self.nav.get_children() else None
        self.nav.delete(*self.nav.get_children())
        shown = [v for v in self.g.views.values() if v.kind != "cross" or self.show_cross.get()]
        places = {v.vid: nav_place(v, self.window_min.get()) for v in shown}
        rank = {vid: i for i, vid in enumerate(NAV)}
        built = {vid: i for i, vid in enumerate(self.g.views)}
        for gid, heading, open_default in GROUPS:
            views = [v for v in shown if places[v.vid][0] == gid]
            if not views:
                continue
            views.sort(key=lambda v: (rank.get(v.vid, len(rank)), built[v.vid]))   # NAV order, then build order
            node = f"grp:{gid}"
            self.nav.insert("", "end", iid=node, text=heading, values=("",), tags=("group",),
                            open=open_default if opened is None else node in opened)
            data, subs = [], {}
            for v in views:
                sub = places[v.vid][1]
                (subs.setdefault(sub, []) if sub else data).append(v)
            for v in data:     # the data first, then the folded sub-headings
                self._nav_item(node, v, places)
            for sub, members in sorted(subs.items(), key=lambda kv: list(SUBGROUPS).index(kv[0])
                                       if kv[0] in SUBGROUPS else len(SUBGROUPS)):
                sid = f"sub:{gid}:{sub}"
                self.nav.insert(node, "end", iid=sid, text=SUBGROUPS.get(sub, sub), values=("",), tags=("sub",),
                                open=False if opened is None else sid in opened)
                for v in members:
                    self._nav_item(sid, v, places)
        if self.cur_vid in self.g.views and self.nav.exists(self.cur_vid):
            self.nav.see(self.cur_vid)
            self.nav.selection_set(self.cur_vid)

    def _nav_item(self, parent, v, places):
        tags = ("empty",) if not v.rows else ("inferred",) if v.kind == "cross" else ("item",)
        self.nav.insert(parent, "end", iid=v.vid, text=places[v.vid][2], values=(f"{len(v.rows):,}",), tags=tags)

    def _all_nav_ids(self, parent=""):
        out = []
        for i in self.nav.get_children(parent):
            out += [i] + self._all_nav_ids(i)
        return out

    def friendly(self, vid):
        """'Group > name' for a view, as the view list shows it."""
        gid, _, name = nav_place(self.g.views[vid], self.window_min.get())
        return f"{GROUP_SHORT.get(gid) or dict((g, h) for g, h, _ in GROUPS).get(gid, gid)} \u203a {name}"

    # ------------------------------------------------------------ views
    def on_nav(self, _=None):
        sel = self.nav.selection()
        if sel and not sel[0].startswith(("grp:", "sub:")) and sel[0] != self.cur_vid:
            self.show(sel[0], from_nav=True)

    def show(self, vid, from_nav=False, select_index=None):
        if vid not in self.g.views:
            vid = next(iter(self.g.views))
        v = self.g.views[vid]
        self.cur_vid = vid
        if not from_nav and self.nav.exists(vid):
            self.nav.selection_set(vid)
            self.nav.see(vid)
        name, colour, meaning = KIND[v.kind]
        self.title_lbl.config(text=nav_place(v, self.window_min.get())[2])
        self.kind_lbl.config(text=BADGE.get(v.kind, v.kind.upper()), bg=colour)
        where = f"{v.db}  -  " if v.db else ""
        self.basis_lbl.config(text=f"{where}{meaning}.   Source: {v.basis}.   Technical name: {v.vid} \u2013 "
                                   f"{v.title}", fg=colour)
        self.table["columns"] = v.cols
        for c in v.cols:
            self.table.heading(c, text=c, command=lambda c=c: self.sort_by(c))
            self.table.column(c, width=260 if c in WIDE else max(80, min(220, 9 * len(c) + 40)), anchor="w",
                              stretch=False)
        if select_index is not None and self.filter_var.get():
            self._quiet = True
            try:
                self.filter_var.set("")
            finally:
                self._quiet = False
        self.populate(select_index)

    def populate(self, select_index=None):
        if not self.g or self.cur_vid not in self.g.views:
            return
        v = self.g.views[self.cur_vid]
        f = self.filter_var.get().strip().lower()
        self.table.delete(*self.table.get_children())
        self.cur_rows = []
        for i, r in enumerate(v.rows):
            vals = ["" if r.get(c) is None else str(r.get(c)) for c in v.cols]
            if f and not any(f in x.lower() for x in vals):
                continue
            self.cur_rows.append(i)
            self.table.insert("", "end", iid=str(i), values=vals)
        self.count_lbl.config(text=f"{len(self.cur_rows):,} of {len(v.rows):,} rows")
        self.detail.delete("1.0", "end")
        self.links.delete(*self.links.get_children())
        if select_index is not None and str(select_index) in self.table.get_children():
            self.table.selection_set(str(select_index))
            self.table.see(str(select_index))
            self.table.focus(str(select_index))

    def sort_by(self, col):
        # sort the row list and refill: moving 30,000 Tk items one by one took about 6 s, and keeping
        # cur_rows in display order makes Export write what the screen shows
        rev = self.sort_state.get(col, False)
        v = self.g.views[self.cur_vid]

        def key(i):
            x = v.rows[i].get(col)
            x = "" if x is None else str(x)
            try:
                return (0, float(x))
            except ValueError:
                return (1, x.lower())
        self.cur_rows.sort(key=key, reverse=rev)
        sel = self.table.selection()
        self.table.delete(*self.table.get_children())
        for i in self.cur_rows:
            r = v.rows[i]
            self.table.insert("", "end", iid=str(i), values=["" if r.get(c) is None else str(r.get(c)) for c in v.cols])
        if sel:
            self.table.selection_set(sel)
            self.table.see(sel[0])
        self.sort_state[col] = not rev

    # ------------------------------------------------------------ links
    def row_entities(self, row):
        ents = []
        if row.get("_e"):
            ents.append(row["_e"])
        here = self.g.views[self.cur_vid].db
        for col, kind in list(COL_ENTITY.items()) + list(VIEW_COL_ENTITY.get(self.cur_vid, {}).items()):
            if row.get(col) not in (None, ""):
                e = (kind, row[col])
                if e in self.g.home and e not in ents:
                    same_db = self.g.views[self.g.home[e][0]].db == here
                    if same_db or self.show_cross.get():
                        ents.append(e)
        return ents

    def on_row(self, _=None):
        sel = self.table.selection()
        if not sel:
            return
        v = self.g.views[self.cur_vid]
        row = v.rows[int(sel[0])]
        self.detail.delete("1.0", "end")
        self.detail.tag_configure("k", font=("Segoe UI", 9, "bold"), foreground=MUTED)
        for c in v.cols:
            val = row.get(c)
            if val in (None, ""):
                continue
            self.detail.insert("end", f"{c}: ", "k")
            self.detail.insert("end", f"{val}\n")
        if row.get("_json"):
            import json
            self.detail.insert("end", "\nRaw JSON (as stored):\n", "k")
            self.detail.insert("end", json.dumps(row["_json"], indent=2, ensure_ascii=False))
        self.links.delete(*self.links.get_children())
        self.link_targets = {}
        n = 0
        own = row.get("_e")
        mine = self.row_entities(row)
        for ent in mine:
            for rid, target, note in sorted(self.g.links(ent), key=lambda x: (x[0], str(x[1]))):
                rv = self.g.views.get(rid)
                scope = rv.kind if rv else "in-table"
                if scope == "cross" and not self.show_cross.get():
                    continue
                if target == own or target in mine:   # the row's own entities are not "linked rows"
                    continue
                home = self.g.home.get(target)
                where = ""
                if home:
                    hv = self.g.views[home[0]]
                    where = self.friendly(home[0])
                iid = f"l{n}"
                self.links.insert("", "end", iid=iid, tags=(scope,),
                                  values=(note or (nav_place(rv)[2] if rv else rid), self.g.label.get(target, str(target)),
                                          where, SCOPE_WORDS.get(scope, scope)))
                self.link_targets[iid] = target
                n += 1
        if n == 0:
            self.links.insert("", "end", values=("No other row is linked to this one" +
                                                 ("" if self.show_cross.get() else " inside its database"), "", "", ""))

    def on_jump(self, _=None):
        sel = self.links.selection()
        if not sel or sel[0] not in self.link_targets:
            return
        target = self.link_targets[sel[0]]
        if target not in self.g.home:
            messagebox.showinfo("No table row", f"{target} has no row of its own to open.")
            return
        vid, idx = self.g.home[target]
        self.show(vid, select_index=idx)
        self.on_row()

    # ------------------------------------------------------------ actions
    def open_folder(self):
        d = filedialog.askdirectory(title="Phone Link device Database folder, or the package folder")
        if not d:
            return
        devs = P.find_device_dirs(d)
        if not devs:
            messagebox.showerror("No Phone Link databases", f"No Phone Link databases found under\n{d}")
            return
        self.devices = devs
        self.dev_combo["values"] = [lbl for lbl, _ in devs]
        self.cur_vid = None
        self.load_device(0)

    def export(self):
        if not self.cur_vid:
            return
        v = self.g.views[self.cur_vid]
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=f"{self.cur_vid}.csv",
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(v.cols)
            for i in self.cur_rows:
                w.writerow([v.rows[i].get(c) for c in v.cols])
        self.status.config(text=f"Exported {len(self.cur_rows):,} rows to {path} (contains case data - keep local)")

    def on_close(self, warn=True):
        if self.ev:
            self._remember(self.ev.close())
        if self.pc:
            self._remember(self.pc.close())
        if self.leftovers and warn:
            messagebox.showwarning("Copies not removed", "These temporary copies of the evidence could not be "
                                   "removed (a file is still in use). Remove them yourself:\n\n" +
                                   "\n".join(self.leftovers))
        self.destroy()

    def screenshot(self, path):
        """Save the window to a PNG (needs Pillow). Captures the window itself, not the screen behind it."""
        self.deiconify()
        self.update()
        self.after(500)
        self.update()
        img = None
        if os.name == "nt":
            try:
                img = self._print_window()
            except (OSError, ValueError, AttributeError):
                img = None
        if img is None:
            from PIL import ImageGrab
            self.lift()
            self.update()
            x, y = self.winfo_rootx(), self.winfo_rooty()
            img = ImageGrab.grab(bbox=(x, y, x + self.winfo_width(), y + self.winfo_height()))
        img.save(path)

    def _print_window(self):
        """Win32 PrintWindow(PW_RENDERFULLCONTENT): renders the window even when it is covered or off-screen."""
        import ctypes
        from ctypes import wintypes
        from PIL import Image
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindowDC.restype = wintypes.HDC
        user32.GetWindowDC.argtypes = [wintypes.HWND]
        user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteDC.argtypes = [wintypes.HDC]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                    ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT]
        hwnd = user32.GetAncestor(self.winfo_id(), 2)  # GA_ROOT
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        hdc = user32.GetWindowDC(hwnd)
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(mdc, bmp)
        try:
            if not user32.PrintWindow(hwnd, mdc, 2):
                return None
            bmi = BITMAPINFOHEADER(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            buf = ctypes.create_string_buffer(w * h * 4)
            if not gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), 0):
                return None
            return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
        finally:
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mdc)
            user32.ReleaseDC(hwnd, hdc)


def stale_copies(exclude=()):
    """Copy folders left in the temp folder by earlier runs that still hold databases."""
    tmp = tempfile.gettempdir()
    out = []
    for name in os.listdir(tmp):
        p = os.path.join(tmp, name)
        if name.startswith(("plrelate-", "pldevices-")) and os.path.isdir(p) and p not in exclude:
            for _, _, files in os.walk(p):
                if any(f.endswith(".db") for f in files):
                    out.append(p)
                    break
    return sorted(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", help="device Database folder, Indexed folder or Phone Link package folder")
    src.add_argument("--demo", action="store_true", help="synthetic data only")
    ap.add_argument("--window", type=int, default=R.DEFAULT_WINDOW_MIN, help="nearby-events window in minutes")
    ap.add_argument("--view", help="view to open first, for example R13 or E_calls")
    ap.add_argument("--select", type=int, help="row index to select in that view")
    ap.add_argument("--screenshot", help="save a PNG of the window and exit (needs Pillow)")
    ap.add_argument("--this-pc", action="store_true",
                    help="also show phones connected to THIS computer by USB cable or Bluetooth (live registry, logs "
                         "and event logs, read-only). On by default when --input is this PC's own Phone Link folder")
    a = ap.parse_args()
    if a.demo:
        import pldemo
        root = pldemo.build_package(tempfile.mkdtemp(prefix="pldemo-"))
        devices = [(f"demo (synthetic) {lbl}", d) for lbl, d in P.find_device_dirs(root)]
        devices.sort(key=lambda d: -len([f for f in os.listdir(d[1]) if f.endswith(".db")]))  # fullest first
        localcache = D.find_localcache(root)
        connections = C.demo_source()
    else:
        devices = P.find_device_dirs(os.path.expandvars(a.input))
        if not devices:
            sys.exit(f"No Phone Link databases found under {a.input}")
        devices.sort(key=lambda d: -len([f for f in os.listdir(d[1]) if f.endswith(".db")]))  # fullest first
        localcache = D.find_localcache(a.input)
        own = os.path.normcase(os.path.abspath(os.path.expandvars("%LOCALAPPDATA%")))
        is_this_pc = os.path.normcase(os.path.abspath(os.path.expandvars(a.input))).startswith(own)
        connections = C.LiveSource() if (a.this_pc or is_this_pc) and os.name == "nt" else None
    app = Viewer(devices, a.window, a.view, localcache, connections, redact=a.demo)
    if a.select is not None:
        app.show(app.cur_vid, select_index=a.select)
        app.on_row()
    if a.screenshot:
        app.after(1500, lambda: (app.screenshot(a.screenshot), app.on_close(warn=False)))
    app.mainloop()


if __name__ == "__main__":
    main()
