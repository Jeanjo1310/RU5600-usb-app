#!/usr/bin/env python3
"""RU5600 Reader - GTK3 front-end (Linux reimplementation of ReaderSoft 5.1).

Layout mirrors the Windows ReaderSoftV5.1 UI: connection panel on the left,
tabbed parameter pages on the right, and a timestamped log at the bottom.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ru5600_i18n import LANGUAGES, tr, tip  # noqa: E402
from ru5600_net import NetReader  # noqa: E402
from ru5600_core import (  # noqa: E402
    FREQ_BANDS,
    READ_MODES,
    check_fingerprint,
    learn_fingerprint,
    MEM_EPC,
    MEM_TID,
    MEM_USER,
    ReaderError,
    Ru5600Reader,
    Tag,
)

APP_TITLE = "RU5600 Reader"
USB_VID = "1a86"
USB_PID = "e010"

# Web-like stylesheet (lo mas cercano a Tailwind en GTK3: CSS propio).
# El toggle claro/oscuro cambia de provider = garantizado en vivo,
# sin depender del tema del escritorio.
DARK_CSS = b"""
window, box, notebook { background-color: #2b2b2b; color: #e6e6e6; }
label, checkbutton, radiobutton { color: #e6e6e6; }
button { background-color: #3a3a3a; color: #e6e6e6;
         border: 1px solid #555555; border-radius: 6px; padding: 6px 12px; }
button:hover { background-color: #454545; }
button:disabled { color: #888888; }
entry { background-color: #333333; color: #e6e6e6;
        border: 1px solid #555555; border-radius: 6px; padding: 4px 8px; }
frame { border: 1px solid #555555; border-radius: 8px; }
notebook tab { background-color: #333333; padding: 6px 12px; }
notebook tab:checked { background-color: #404040; }
treeview { background-color: #262626; color: #e6e6e6; }
"""

LIGHT_CSS = b"""
window, box, notebook { background-color: #f5f5f5; color: #222222; }
label, checkbutton, radiobutton { color: #222222; }
button { background-color: #ffffff; color: #222222;
         border: 1px solid #cccccc; border-radius: 6px; padding: 6px 12px; }
button:hover { background-color: #e8f0fe; }
button:disabled { color: #999999; }
entry { background-color: #ffffff; color: #222222;
        border: 1px solid #cccccc; border-radius: 6px; padding: 4px 8px; }
frame { border: 1px solid #cccccc; border-radius: 8px; }
notebook tab { background-color: #e8e8e8; padding: 6px 12px; }
notebook tab:checked { background-color: #ffffff; }
treeview { background-color: #ffffff; color: #222222; }
"""


def _usb_readers() -> list:
    """Find reader nodes under /sys/bus/usb/devices (VID:PID 1a86:e010)."""
    found = []
    base = "/sys/bus/usb/devices"
    try:
        for node in sorted(os.listdir(base)):
            vid = pid = product = None
            try:
                vid = open(os.path.join(base, node, "idVendor")).read().strip()
                pid = open(os.path.join(base, node, "idProduct")).read().strip()
            except OSError:
                continue
            if vid.lower() == USB_VID and pid.lower() == USB_PID:
                try:
                    product = open(
                        os.path.join(base, node, "product"), errors="replace"
                    ).read().strip()
                except OSError:
                    product = node
                found.append((node, product or node))
    except OSError:
        pass
    return found


class Worker:
    """Runs a callable off the GTK thread and posts the result back."""

    def __init__(self, gui):
        self.gui = gui

    def run(self, fn, on_ok=None, on_err=None):
        def target():
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001
                if on_err:
                    GLib.idle_add(on_err, exc)
                else:
                    GLib.idle_add(self.gui.log_error, str(exc))
                return
            if on_ok:
                GLib.idle_add(on_ok, result)

        threading.Thread(target=target, daemon=True).start()


class ReaderApp(Gtk.Window):
    def __init__(self):
        super().__init__(title=APP_TITLE)
        self.set_default_size(1180, 800)
        self.reader = None
        self.worker = Worker(self)
        self._tag_counts = {}
        self._tag_order = []
        self._active = False

        self._build_ui()
        self.connect("destroy", self._on_destroy)

    # ------------------------------------------------------------------ UI
    def on_language(self, *_):
        """Change the language of the interface in place."""
        code = self.cb_lang.get_active_id() or "en"
        self._translate(self, code)

    def _translate(self, widget, lang):
        """Translate the visible texts and add the missing tooltips.

        The original text is kept on the widget, so the language can be
        changed back and forth as many times as wanted.
        """
        if isinstance(widget, (Gtk.Label, Gtk.Button, Gtk.CheckButton,
                              Gtk.Frame)):
            if isinstance(widget, Gtk.Label):
                text = widget.get_text()
            else:
                text = widget.get_label() or ""
            original = getattr(widget, "_orig_text", None)
            if original is None:
                original = text
                widget._orig_text = text
            new = tr(original, lang)
            if isinstance(widget, Gtk.Label):
                widget.set_text(new)
            else:
                widget.set_label(new)
            self._set_tip(widget, original, lang)
        elif isinstance(widget, Gtk.Notebook):
            for lbl in getattr(self, "_tab_labels", []):
                original = getattr(lbl, "_orig_text", None)
                if original is None:
                    original = lbl.get_text()
                    lbl._orig_text = original
                lbl.set_text(tr(original, lang))
            for i in range(widget.get_n_pages()):
                page = widget.get_nth_page(i)
                if page is not None:
                    self._translate(page, lang)
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                self._translate(child, lang)

    def _set_tip(self, widget, text, lang):
        """Put the tooltip of a control, without losing the one it had."""
        if widget.get_tooltip_text():
            return
        text_tip = tip(text, lang)
        if text_tip:
            widget.set_tooltip_text(text_tip)

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add(root)
        self._css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), self._css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._apply_theme(False)

        # ---------------- notebook (full width) --------------------------
        # The connection panel used to live on the left; since 2026-10-26
        # it is the first tab ("Connection") with breathing room and no
        # side scrollbar (Creador: sin scrolls innecesarios).
        self._tab_labels = []
        self.nb = Gtk.Notebook()
        self.nb.set_scrollable(True)
        root.pack_start(self.nb, True, True, 0)
        self._build_connection_tab()
        self._build_param_tab()
        self._build_readwrite_tab()
        self._build_active_tab()
        self._build_advance_tab()
        self._build_chip_tab()
        self._build_risky_tab()
        self._build_whitelist_tab()
        self._build_batchwrite_tab()

        # ---------------- bottom action row ------------------------------
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        actions.set_halign(Gtk.Align.CENTER)
        root.pack_start(actions, False, False, 0)
        self.btn_read_action = None
        self.btn_set_action = None
        for label, cb in (("Read", self.on_read),
                          ("Set", self.on_set),
                          ("Default", self.on_default)):
            b = Gtk.Button(label=label)
            b.set_size_request(150, -1)
            b.connect("clicked", cb)
            actions.pack_start(b, False, False, 0)
            if label == "Read":
                self.btn_read_action = b
            elif label == "Set":
                self.btn_set_action = b

        # ---------------- bottom log -------------------------------------
        logbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        root.pack_start(logbox, False, False, 0)

        self.log_store = Gtk.ListStore(str, str)
        self.log_view = Gtk.TreeView(model=self.log_store)
        self.log_view.append_column(Gtk.TreeViewColumn("Time",
                                                       Gtk.CellRendererText(),
                                                       text=0))
        self.log_view.append_column(Gtk.TreeViewColumn("Result",
                                                       Gtk.CellRendererText(),
                                                       text=1))
        self.log_view.set_headers_visible(True)
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(150)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.log_view)
        logbox.pack_start(scroll, True, True, 0)

        self.refresh_devices()
        self._install_accelerators()
        self.on_language()

    def _build_connection_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(12)

        lang = Gtk.Frame(label="Language and theme")
        lg = Gtk.Grid(column_spacing=12, row_spacing=6)
        lg.set_border_width(8)
        lang.add(lg)
        self.cb_lang = Gtk.ComboBoxText()
        for name, code in LANGUAGES:
            self.cb_lang.append(code, name)
        self.cb_lang.set_active(0)
        self.cb_lang.set_tooltip_text(
            "Language of the interface. It can be changed at any moment.")
        self.cb_lang.connect("changed", self.on_language)
        lg.attach(Gtk.Label(label="Language", xalign=0), 0, 0, 1, 1)
        lg.attach(self.cb_lang, 1, 0, 1, 1)
        self.chk_light = Gtk.CheckButton(label="Light")
        self.chk_light.set_tooltip_text("Light theme instead of dark.")
        self.chk_light.connect("toggled", self.on_light_theme)
        lg.attach(self.chk_light, 2, 0, 1, 1)
        box.pack_start(lang, False, False, 0)

        conn = Gtk.Frame(label="Connection")
        lv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        lv.set_border_width(8)
        conn.add(lv)

        self.rb_usb = Gtk.RadioButton.new_with_label_from_widget(None, "USB")
        self.rb_com = Gtk.RadioButton.new_with_label_from_widget(self.rb_usb, "COM")
        self.rb_net = Gtk.RadioButton.new_with_label_from_widget(self.rb_usb, "Net")
        for rb in (self.rb_usb, self.rb_com, self.rb_net):
            lv.pack_start(rb, False, False, 0)
        self.rb_usb.set_active(True)

        grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        grid.set_column_homogeneous(True)
        grid.set_border_width(8)
        lv.pack_start(grid, False, False, 0)

        self.cb_baud = Gtk.ComboBoxText()
        for rate in ("9600bps", "19200bps", "38400bps", "57600bps",
                     "115200bps", "230400bps"):
            self.cb_baud.append_text(rate)
        self.cb_baud.set_active(4)

        g_serial = Gtk.Grid(column_spacing=6, row_spacing=6)
        g_serial.attach(Gtk.Label(label="Baudrate:", xalign=0), 0, 0, 1, 1)
        g_serial.attach(self.cb_baud, 1, 0, 1, 1)
        self.cb_device = Gtk.ComboBoxText()
        g_serial.attach(Gtk.Label(label="Device:", xalign=0), 0, 1, 1, 1)
        g_serial.attach(self.cb_device, 1, 1, 1, 1)
        grid.attach(g_serial, 0, 0, 1, 1)

        self.cb_transport = Gtk.ComboBoxText()
        for t in ("USB", "COM", "Net"):
            self.cb_transport.append_text(t)
        self.cb_transport.set_active(0)
        g_mode = Gtk.Grid(column_spacing=6, row_spacing=6)
        g_mode.attach(Gtk.Label(label="Interface:", xalign=0), 0, 0, 1, 1)
        g_mode.attach(self.cb_transport, 1, 0, 1, 1)

        self.cb_read_mode = Gtk.ComboBoxText()
        for m in READ_MODES:
            label = {"burst": "Burst (fastest)",
                     "balanced": "Balanced",
                     "reliable": "Reliable (8/8)"}[m]
            self.cb_read_mode.append_text(label)
        self.cb_read_mode.set_active(1)
        self.cb_read_mode.set_tooltip_text(
            "How hard the reader tries on a single detection:\n"
            "Burst    - one poll, fastest, misses most of the time\n"
            "Balanced - three polls\n"
            "Reliable - keeps polling until the reader answers")
        g_mode.attach(Gtk.Label(label="Read mode:", xalign=0), 0, 1, 1, 1)
        g_mode.attach(self.cb_read_mode, 1, 1, 1, 1)
        grid.attach(g_mode, 2, 0, 1, 1)

        self.entry_ip = Gtk.Entry(text="192.168.1.250")
        self.entry_port = Gtk.Entry(text="60000")
        g_net = Gtk.Grid(column_spacing=6, row_spacing=6)
        g_net.attach(Gtk.Label(label="Net IP:", xalign=0), 0, 0, 1, 1)
        g_net.attach(self.entry_ip, 1, 0, 1, 1)
        g_net.attach(Gtk.Label(label="NetPort:", xalign=0), 0, 1, 1, 1)
        g_net.attach(self.entry_port, 1, 1, 1, 1)
        grid.attach(g_net, 1, 0, 1, 1)

        btns = Gtk.Grid(column_spacing=6, row_spacing=6)
        btns.set_column_homogeneous(True)
        lv.pack_start(btns, False, False, 0)
        self.btn_open = Gtk.Button(label="Open")
        self.btn_close = Gtk.Button(label="Close")
        self.btn_close.set_sensitive(False)
        self.btn_open.connect("clicked", lambda *_: self.on_open())
        self.btn_close.connect("clicked", lambda *_: self.on_close())
        for b in (self.btn_open, self.btn_close):
            b.set_hexpand(True)
        btns.attach(self.btn_open, 0, 0, 1, 1)
        btns.attach(self.btn_close, 1, 0, 1, 1)
        row = 1
        for label, cb in (("ScanPort", self.on_scan_port),
                          ("ScanNetwork", self.on_scan_network),
                          ("TCPServer", self.on_tcp_server),
                          ("Help", self.on_help)):
            b = Gtk.Button(label=label)
            b.connect("clicked", cb)
            b.set_hexpand(True)
            btns.attach(b, row % 2, row // 2 + 1, 1, 1)
            row += 1

        self.btn_udev = Gtk.Button(label="Install USB access rule")
        self.btn_udev.set_tooltip_text(
            "Installs the udev rule that lets non-root users open the "
            "reader (needs the administrator password)")
        self.btn_udev.connect("clicked", self.on_install_udev)
        lv.pack_start(self.btn_udev, False, False, 0)

        box.pack_start(conn, False, False, 0)

        # device identity
        info = Gtk.Frame(label="Device")
        ig = Gtk.Grid(column_spacing=8, row_spacing=6)
        ig.set_border_width(8)
        info.add(ig)
        self.entry_sn = Gtk.Entry(); self.entry_sn.set_editable(False)
        self.entry_fw = Gtk.Entry(); self.entry_fw.set_editable(False)
        self.entry_hw = Gtk.Entry(); self.entry_hw.set_editable(False)
        ig.attach(Gtk.Label(label="DeviceSN:", xalign=0), 0, 0, 1, 1)
        ig.attach(self.entry_sn, 1, 0, 1, 1)
        ig.attach(Gtk.Label(label="Firmware:", xalign=0), 0, 1, 1, 1)
        ig.attach(self.entry_fw, 1, 1, 1, 1)
        ig.attach(Gtk.Label(label="Hardware:", xalign=0), 0, 2, 1, 1)
        ig.attach(self.entry_hw, 1, 2, 1, 1)
        box.pack_start(info, False, False, 0)

        cfg = Gtk.Grid(column_spacing=6, row_spacing=6)
        cfg.set_column_homogeneous(True)
        box.pack_start(cfg, False, False, 0)
        row = 0
        for label, cb in (("ExportConfig", self.on_export_config),
                          ("ImportConfig", self.on_import_config),
                          ("CloseRelay", lambda *_: self.on_relay(False)),
                          ("ReleaseRelay", lambda *_: self.on_relay(True))):
            b = Gtk.Button(label=label)
            b.connect("clicked", cb)
            b.set_hexpand(True)
            cfg.attach(b, row % 2, row // 2, 1, 1)
            row += 1

        lbl_tab = Gtk.Label(label="Connection")
        self._tab_labels.append(lbl_tab)
        self.nb.append_page(box, lbl_tab)

    def on_light_theme(self, *_):
        self._apply_theme(self.chk_light.get_active())

    def _apply_theme(self, light):
        self._css.load_from_data(LIGHT_CSS if light else DARK_CSS)
        Gtk.Settings.get_default().set_property(
            "gtk-application-prefer-dark-theme", not light)
        self.queue_draw()

    def _install_accelerators(self):
        """Keyboard shortcuts: Ctrl+letter for actions, Alt+1..6 for tabs."""
        accel = Gtk.AccelGroup()
        self.add_accel_group(accel)
        targets = [
            ("<Control>o", self.btn_open),
            ("<Control>r", self.btn_read_action),
            ("<Control>s", self.btn_set_action),
            ("<Control>t", self.btn_start),
            ("<Control><Shift>t", self.btn_stop),
        ]
        for combo, widget in targets:
            if widget is None:
                continue
            key, mods = Gtk.accelerator_parse(combo)
            accel.connect(key, mods, Gtk.AccelFlags.VISIBLE,
                          lambda *_, w=widget: w.activate())
        for i in range(self.nb.get_n_pages()):
            key, mods = Gtk.accelerator_parse(f"<Alt>{i + 1}")
            accel.connect(key, mods, Gtk.AccelFlags.VISIBLE,
                          self._make_tab_switcher(i))

    def _make_tab_switcher(self, index):
        def handler(*_):
            self.nb.set_current_page(index)
            return True
        return handler

    # ------------------------------------------------------------- tabs
    def _build_param_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(8)

        rp = Gtk.Frame(label="Reader parameters")
        rg = Gtk.Grid(column_spacing=8, row_spacing=6)
        rg.set_border_width(8)
        rp.add(rg)
        self.cb_rfpower = Gtk.ComboBoxText()
        for v in range(0, 31):
            self.cb_rfpower.append_text(f"{v}dBm")
        self.cb_rfpower.set_active(15)
        self.cb_freqband = Gtk.ComboBoxText()
        for band in FREQ_BANDS:
            self.cb_freqband.append_text(band)
        self.cb_freqband.set_active(0)
        self.chk_singlefreq = Gtk.CheckButton(label="SingleFreq")
        self.entry_freq = Gtk.Entry(text="902.75")
        rg.attach(Gtk.Label(label="RFPower:", xalign=0), 0, 0, 1, 1)
        rg.attach(self.cb_rfpower, 1, 0, 1, 1)
        rg.attach(Gtk.Label(label="FreqBand:", xalign=0), 2, 0, 1, 1)
        rg.attach(self.cb_freqband, 3, 0, 1, 1)
        rg.attach(self.chk_singlefreq, 4, 0, 1, 1)
        rg.attach(self.entry_freq, 5, 0, 1, 1)
        box.pack_start(rp, False, False, 0)

        rs = Gtk.Frame(label="Reading settings")
        sg = Gtk.Grid(column_spacing=8, row_spacing=6)
        sg.set_border_width(8)
        rs.add(sg)
        self.cb_workmode = Gtk.ComboBoxText()
        for m in ("AnswerMode", "ActiveMode"):
            self.cb_workmode.append_text(m)
        self.cb_workmode.set_active(0)
        self.cb_inquiry = Gtk.ComboBoxText()
        for a in ("EPC", "TID", "USER"):
            self.cb_inquiry.append_text(a)
        self.cb_inquiry.set_active(0)
        self.entry_filter = Gtk.Entry(text="0")
        self.entry_start = Gtk.Entry(text="00")
        self.entry_bytelen = Gtk.Entry(text="00")
        self.entry_readtime = Gtk.Entry(text="2")
        self.chk_buzzer = Gtk.CheckButton(label="Buzzer")
        self.chk_buzzer.set_active(False)
        self.btn_silence = Gtk.Button(label="Silence it")
        self.btn_silence.set_tooltip_text(
            "Turns the reader's buzzer off right away (the reader beeps "
            "on every tag it reads).")
        self.btn_silence.connect("clicked", lambda *_: self.on_silence())
        sg.attach(Gtk.Label(label="Interface:", xalign=0), 0, 0, 1, 1)
        sg.attach(self.cb_transport2(), 1, 0, 1, 1)
        sg.attach(Gtk.Label(label="WorkMode:", xalign=0), 2, 0, 1, 1)
        sg.attach(self.cb_workmode, 3, 0, 1, 1)
        sg.attach(Gtk.Label(label="FilterTime(S):", xalign=0), 4, 0, 1, 1)
        sg.attach(self.entry_filter, 5, 0, 1, 1)
        sg.attach(Gtk.Label(label="InquiryArea:", xalign=0), 0, 1, 1, 1)
        sg.attach(self.cb_inquiry, 1, 1, 1, 1)
        sg.attach(Gtk.Label(label="StartAddress(Hex):", xalign=0), 2, 1, 1, 1)
        sg.attach(self.entry_start, 3, 1, 1, 1)
        sg.attach(Gtk.Label(label="ByteLength(Hex):", xalign=0), 4, 1, 1, 1)
        sg.attach(self.entry_bytelen, 5, 1, 1, 1)
        sg.attach(Gtk.Label(label="ReadTime(*10ms):", xalign=0), 0, 2, 1, 1)
        sg.attach(self.entry_readtime, 1, 2, 1, 1)
        sg.attach(self.chk_buzzer, 2, 2, 1, 1)
        sg.attach(self.btn_silence, 3, 2, 1, 1)
        box.pack_start(rs, False, False, 0)

        kb = Gtk.Frame(label="Keyboard")
        kg = Gtk.Grid(column_spacing=8, row_spacing=6)
        kg.set_border_width(8)
        kb.add(kg)
        self.cb_outfmt = Gtk.ComboBoxText()
        for f in ("All(Hex)", "EPC(HEX)", "EPC(ASCII)"):
            self.cb_outfmt.append_text(f)
        self.cb_outfmt.set_active(0)
        self.chk_enterkey = Gtk.CheckButton(label="OutWithEnterKey")
        self.chk_enterkey.set_active(True)
        kg.attach(Gtk.Label(label="OutPutFormat:", xalign=0), 0, 0, 1, 1)
        kg.attach(self.cb_outfmt, 1, 0, 1, 1)
        kg.attach(self.chk_enterkey, 2, 0, 1, 1)
        box.pack_start(kb, False, False, 0)

        lbl_tab = Gtk.Label(label="ParameterSet")
        self._tab_labels.append(lbl_tab)
        self.nb.append_page(box, lbl_tab)

    def cb_transport2(self):
        cb = Gtk.ComboBoxText()
        for t in ("USB", "COM", "Net"):
            cb.append_text(t)
        cb.set_active(0)
        return cb

    def _build_readwrite_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(8)

        frame = Gtk.Frame(label="Read tag memory")
        g = Gtk.Grid(column_spacing=8, row_spacing=6)
        g.set_border_width(8)
        frame.add(g)
        self.cb_mem = Gtk.ComboBoxText()
        for m in ("EPC", "TID", "USER"):
            self.cb_mem.append_text(m)
        self.cb_mem.set_active(0)
        self.entry_wordptr = Gtk.Entry(text="00")
        self.entry_wordcount = Gtk.Entry(text="08")
        self.entry_password = Gtk.Entry(text="00000000")
        self.entry_password.set_max_length(8)
        self.entry_password.set_tooltip_text(
            "Gen2 access password (8 hex digits, 00000000 = no password)")
        g.attach(Gtk.Label(label="Bank:", xalign=0), 0, 0, 1, 1)
        g.attach(self.cb_mem, 1, 0, 1, 1)
        g.attach(Gtk.Label(label="WordPtr(Hex):", xalign=0), 2, 0, 1, 1)
        g.attach(self.entry_wordptr, 3, 0, 1, 1)
        g.attach(Gtk.Label(label="WordCount(Hex):", xalign=0), 4, 0, 1, 1)
        g.attach(self.entry_wordcount, 5, 0, 1, 1)
        g.attach(Gtk.Label(label="Password:", xalign=0), 0, 1, 1, 1)
        g.attach(self.entry_password, 1, 1, 2, 1)
        b = Gtk.Button(label="Read memory")
        b.connect("clicked", lambda *_: self.on_read_memory())
        g.attach(b, 3, 1, 2, 1)
        box.pack_start(frame, False, False, 0)

        frame3 = Gtk.Frame(label="Write memory (USER, EPC...)")
        g3 = Gtk.Grid(column_spacing=8, row_spacing=6)
        g3.set_border_width(8)
        frame3.add(g3)
        self.cb_write_bank = Gtk.ComboBoxText()
        for name, val in (("USER", 3), ("EPC", 1), ("RESERVED", 0)):
            self.cb_write_bank.append(name, str(val))
        self.cb_write_bank.set_active(0)
        self.cb_write_bank.set_tooltip_text(
            "RESERVED holds the passwords: writing there changes the "
            "access password of the tag. Do it only with a reason.")
        self.entry_write_ptr = Gtk.Entry(text="00")
        self.entry_write_ptr.set_tooltip_text(
            "Start word (hex). 0 = the beginning of the bank.")
        self.entry_write_data = Gtk.Entry(text="")
        self.entry_write_data.set_placeholder_text(
            "data to write (hex, even number of bytes)")
        g3.attach(Gtk.Label(label="Bank:", xalign=0), 0, 0, 1, 1)
        g3.attach(self.cb_write_bank, 1, 0, 1, 1)
        g3.attach(Gtk.Label(label="WordPtr(Hex):", xalign=0), 2, 0, 1, 1)
        g3.attach(self.entry_write_ptr, 3, 0, 1, 1)
        g3.attach(Gtk.Label(label="Data(hex):", xalign=0), 0, 1, 1, 1)
        g3.attach(self.entry_write_data, 1, 1, 3, 1)
        b3 = Gtk.Button(label="Write and verify")
        b3.set_tooltip_text(
            "Writes the data and reads it back: what is reported as "
            "written was really read from the tag afterwards.")
        b3.connect("clicked", lambda *_: self.on_write_memory())
        g3.attach(b3, 4, 1, 1, 1)
        box.pack_start(frame3, False, False, 0)

        frame2 = Gtk.Frame(label="Write EPC")
        g2 = Gtk.Grid(column_spacing=8, row_spacing=6)
        g2.set_border_width(8)
        frame2.add(g2)
        self.entry_write_epc = Gtk.Entry(
            text="3034333234353536373839303132")
        g2.attach(Gtk.Label(label="EPC (hex):", xalign=0), 0, 0, 1, 1)
        g2.attach(self.entry_write_epc, 1, 0, 3, 1)
        b2 = Gtk.Button(label="Write EPC")
        b2.connect("clicked", lambda *_: self.on_write_epc())
        g2.attach(b2, 0, 1, 2, 1)
        box.pack_start(frame2, False, False, 0)

        self.mem_output = Gtk.Label(label="-")
        self.mem_output.set_selectable(True)
        box.pack_start(self.mem_output, False, False, 0)

        fp = Gtk.Frame(label="Huella (anti-clonado)")
        fg = Gtk.Grid(column_spacing=8, row_spacing=6)
        fg.set_border_width(8)
        fp.add(fg)
        self.chk_fp = Gtk.CheckButton(
            label="Verificar la huella antes de escribir")
        self.chk_fp.set_tooltip_text(
            "El EPC se puede copiar a cualquier tag; el TID no.  La huella "
            "guarda que EPC corresponde a que TID y el cliente se niega a "
            "escribir si no coinciden.")
        self.btn_fp_learn = Gtk.Button(label="Aprender huella del tag actual")
        self.btn_fp_learn.connect("clicked", lambda *_: self.on_fp_learn())
        fg.attach(self.chk_fp, 0, 0, 1, 1)
        fg.attach(self.btn_fp_learn, 0, 1, 1, 1)
        self.lbl_fp = Gtk.Label(label="")
        fg.attach(self.lbl_fp, 1, 0, 1, 2)
        box.pack_start(fp, False, False, 0)
        lbl_tab = Gtk.Label(label="Read/Write")
        self._tab_labels.append(lbl_tab)
        self.nb.append_page(box, lbl_tab)

    def _build_active_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(8)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.pack_start(bar, False, False, 0)
        self.btn_start = Gtk.Button(label="Start read")
        self.btn_stop = Gtk.Button(label="Stop read")
        self.btn_start.connect("clicked", lambda *_: self.on_start_read())
        self.btn_stop.connect("clicked", lambda *_: self.on_stop_read())
        bar.pack_start(self.btn_start, False, False, 0)
        bar.pack_start(self.btn_stop, False, False, 0)
        self.lbl_active = Gtk.Label(label="Status: stopped")
        bar.pack_start(self.lbl_active, False, False, 0)
        self.entry_measure = Gtk.Entry(text="10")
        self.entry_measure.set_max_length(4)
        self.entry_measure.set_width_chars(4)
        bar.pack_start(Gtk.Label(label="Measure (s):"), False, False, 0)
        bar.pack_start(self.entry_measure, False, False, 0)
        b_measure = Gtk.Button(label="Measure")
        b_measure.set_tooltip_text(
            "Reads for N seconds and reports the detection rate and the "
            "antenna signal (RSSI) - the numbers to compare when mapping "
            "the reading area of the antenna.")
        b_measure.connect("clicked", lambda *_: self.on_measure())
        bar.pack_start(b_measure, False, False, 0)

        # one row per tag, with the sequential number it was given when
        # it was read the first time (the same as the ReaderSoft demo:
        # a tag that is read again does not get a new record, it only
        # gets its count and its last seen updated).
        # CRUDO (exe) shows the raw record exactly as the Windows
        # ReaderSoftV5.1 shows it ("Type/Ant/Tag/RSSI", whole blob with
        # spaces) - never hidden, never truncated: the code is fixed on
        # any antenna.  EPC/Chip are the chip-first machine
        # interpretation below the raw (filled by _identify_tag after a
        # direct TID read + decode; pending chips show "...").
        self.tag_store = Gtk.ListStore(str, str, str, str, str, str, str,
                                       str)
        view = Gtk.TreeView(model=self.tag_store)
        for i, title in enumerate(("#", "Ant", "CRUDO (exe)", "EPC",
                                   "Chip", "RSSI", "Count", "Last seen")):
            view.append_column(Gtk.TreeViewColumn(title, Gtk.CellRendererText(),
                                                  text=i))
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.add(view)
        box.pack_start(scroll, True, True, 0)
        lbl_active_tab = Gtk.Label(label="ActiveMode")
        lbl_active_tab.set_tooltip_text(
            "Lectura continua del lector (stream de tags).\n"
            "No tiene que ver con los tags activos: los tags Gen2 son "
            "pasivos, sin bateria.\n"
            "Este es el nombre que le da el vendor a este modo del lector.")
        self._tab_labels.append(lbl_active_tab)
        self.nb.append_page(box, lbl_active_tab)

    def _build_advance_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(8)

        frame = Gtk.Frame(label="Frequency")
        g = Gtk.Grid(column_spacing=8, row_spacing=6)
        g.set_border_width(8)
        frame.add(g)
        self.cb_freq_adv = Gtk.ComboBoxText()
        for band in FREQ_BANDS:
            self.cb_freq_adv.append_text(band)
        self.cb_freq_adv.set_active(0)
        g.attach(Gtk.Label(label="Band:", xalign=0), 0, 0, 1, 1)
        g.attach(self.cb_freq_adv, 1, 0, 1, 1)
        b_read = Gtk.Button(label="Read freq")
        b_read.connect("clicked", lambda *_: self.on_read_freq())
        b_set = Gtk.Button(label="Set freq")
        b_set.connect("clicked", lambda *_: self.on_set_freq())
        g.attach(b_read, 0, 1, 1, 1)
        g.attach(b_set, 1, 1, 1, 1)
        box.pack_start(frame, False, False, 0)

        frame2 = Gtk.Frame(label="Device")
        g2 = Gtk.Grid(column_spacing=8, row_spacing=6)
        g2.set_border_width(8)
        frame2.add(g2)
        b_reboot = Gtk.Button(label="Reboot reader")
        b_reboot.connect("clicked", lambda *_: self.on_reboot())
        g2.attach(b_reboot, 0, 0, 1, 1)
        self.lbl_freq_raw = Gtk.Label(label="raw freq: -")
        g2.attach(self.lbl_freq_raw, 1, 0, 1, 1)
        box.pack_start(frame2, False, False, 0)
        lbl_tab = Gtk.Label(label="AdvanceSet")
        self._tab_labels.append(lbl_tab)
        self.nb.append_page(box, lbl_tab)

    def _build_chip_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(8)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.pack_start(bar, False, False, 0)
        b_analyze = Gtk.Button(label="Analyze chip")
        b_analyze.connect("clicked", lambda *_: self.on_analyze_chip())
        bar.pack_start(b_analyze, False, False, 0)
        bar.pack_start(Gtk.Label(label="Access password:"), False, False, 0)
        self.entry_chip_pwd = Gtk.Entry(text="00000000")
        self.entry_chip_pwd.set_max_length(8)
        self.entry_chip_pwd.set_width_chars(10)
        self.entry_chip_pwd.set_tooltip_text(
            "Gen2 access password (8 hex digits). Leave 00000000 for an "
            "unprotected tag.")
        bar.pack_start(self.entry_chip_pwd, False, False, 0)
        b_pwd = Gtk.Button(label="Test password")
        b_pwd.connect("clicked", lambda *_: self.on_test_password())
        bar.pack_start(b_pwd, False, False, 0)
        self.chk_deep = Gtk.CheckButton(label="Deep scan (memory map)")
        self.chk_deep.set_tooltip_text(
            "Also maps the reserved and user memory. Takes a few seconds.")
        bar.pack_start(self.chk_deep, False, False, 0)

        self.chip_view = Gtk.TextView()
        self.chip_view.set_editable(False)
        self.chip_view.set_monospace(True)
        self.chip_buffer = self.chip_view.get_buffer()
        self.chip_buffer.set_text(
            "Put a tag on the antenna and press 'Analyze chip'.")
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.add(self.chip_view)
        box.pack_start(scroll, True, True, 0)
        lbl_tab = Gtk.Label(label="Chip info")
        self._tab_labels.append(lbl_tab)
        self.nb.append_page(box, lbl_tab)

    def _chip_password(self) -> bytes:
        text = self.entry_chip_pwd.get_text().strip().replace(" ", "")
        try:
            pwd = bytes.fromhex(text)
        except ValueError:
            pwd = b""
        return pwd if len(pwd) == 4 else b"\x00" * 4

    def on_analyze_chip(self, *_):
        if self.reader is None or not self.reader.is_open:
            self.log("Open the reader first")
            return
        self.log("Analyzing chip...")

        def task():
            from ru5600_chip import analyze_chip, format_report
            rep = analyze_chip(self.reader, self._chip_password(),
                               deep=self.chk_deep.get_active())
            return format_report(rep)

        def ok(text):
            self.chip_buffer.set_text(text)
            self.log("Chip analysis finished")

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def on_test_password(self, *_):
        if self.reader is None or not self.reader.is_open:
            self.log("Open the reader first")
            return
        pwd = self._chip_password()

        def task():
            from ru5600_chip import verify_password
            ok, data = verify_password(self.reader, pwd)
            return ok, data

        def ok(result):
            good, data = result
            if good:
                self.log(f"Password {pwd.hex().upper()} WORKS - EPC read ok")
                self.chip_buffer.set_text(
                    self.chip_buffer.get_text(
                        self.chip_buffer.get_start_iter(),
                        self.chip_buffer.get_end_iter(), False)
                    + f"\n\nAccess password {pwd.hex().upper()} WORKS\n"
                      f"EPC bank: {data.hex().upper()}")
            else:
                self.log(f"Password {pwd.hex().upper()} is WRONG "
                         f"(every read was refused)")

        self.worker.run(task, on_ok=ok, on_err=self.log_error)


    def _build_risky_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(8)

        warn = Gtk.Label()
        warn.set_markup(
            "<b><span foreground='#cc0000'>OPERACIONES IRREVERSIBLES</span></b>")
        warn.set_xalign(0.0)
        box.pack_start(warn, False, False, 0)

        text = Gtk.Label(
            label="Un lock mal hecho deja el tag inutilizable para siempre y "
                  "un comando crudo puede dejar el lector en un estado "
                  "inconsistente. El vendor no documenta la semantica del "
                  "lock; tomalo como experimental.")
        text.set_xalign(0.0)
        text.set_line_wrap(True)
        box.pack_start(text, False, False, 0)

        self.chk_risk = Gtk.CheckButton(
            label="Entiendo el riesgo de estas operaciones")
        box.pack_start(self.chk_risk, False, False, 0)

        # --- A: bloqueo Gen2 -------------------------------------------
        lock = Gtk.Frame(label="A - Bloquear tag (Gen2 block lock, cmd 0x05)")
        lg = Gtk.Grid(column_spacing=8, row_spacing=6)
        lg.set_border_width(8)
        lock.add(lg)
        self.entry_lock_data = Gtk.Entry(text="00000000")
        self.entry_lock_data.set_max_length(8)
        self.entry_lock_data.set_width_chars(12)
        self.entry_lock_data.set_tooltip_text(
            "4 bytes en hex: el target y/o el password del lock. El vendor "
            "no lo documenta.")
        self.entry_lock_action = Gtk.Entry(text="00")
        self.entry_lock_action.set_max_length(2)
        self.entry_lock_action.set_width_chars(4)
        self.entry_lock_action.set_tooltip_text(
            "Byte de la accion de lock (0x00-0xFF). No documentado.")
        lg.attach(Gtk.Label(label="Lock data (4 bytes):", xalign=0), 0, 0, 1, 1)
        lg.attach(self.entry_lock_data, 1, 0, 1, 1)
        lg.attach(Gtk.Label(label="Accion:", xalign=0), 2, 0, 1, 1)
        lg.attach(self.entry_lock_action, 3, 0, 1, 1)
        b_lock = Gtk.Button(label="Bloquear tag")
        b_lock.connect("clicked", lambda *_: self.on_lock())
        lg.attach(b_lock, 0, 1, 2, 1)
        box.pack_start(lock, False, False, 0)

        # --- B: comando crudo -------------------------------------------
        raw = Gtk.Frame(label="B - Comando crudo (CFHid_SendCommand)")
        rg = Gtk.Grid(column_spacing=8, row_spacing=6)
        rg.set_border_width(8)
        raw.add(rg)
        self.entry_raw_cmd = Gtk.Entry(text="10")
        self.entry_raw_cmd.set_max_length(4)
        self.entry_raw_cmd.set_width_chars(6)
        self.entry_raw_payload = Gtk.Entry(text="")
        self.entry_raw_timeout = Gtk.Entry(text="400")
        self.entry_raw_timeout.set_max_length(5)
        self.entry_raw_timeout.set_width_chars(6)
        rg.attach(Gtk.Label(label="Comando:", xalign=0), 0, 0, 1, 1)
        rg.attach(self.entry_raw_cmd, 1, 0, 1, 1)
        rg.attach(Gtk.Label(label="Payload (hex):", xalign=0), 2, 0, 1, 1)
        rg.attach(self.entry_raw_payload, 3, 0, 1, 1)
        rg.attach(Gtk.Label(label="Timeout (ms):", xalign=0), 0, 1, 1, 1)
        rg.attach(self.entry_raw_timeout, 1, 1, 1, 1)
        b_raw = Gtk.Button(label="Enviar comando crudo")
        b_raw.connect("clicked", lambda *_: self.on_raw_command())
        rg.attach(b_raw, 3, 1, 1, 1)
        box.pack_start(raw, False, False, 0)

        crypt = Gtk.Frame(label="C - CryptWrite (EXPERIMENTAL, en desarrollo)")
        cg = Gtk.Grid(column_spacing=8, row_spacing=6)
        cg.set_border_width(8)
        crypt.add(cg)
        self.entry_cw_mode = Gtk.Entry(text="0")
        self.entry_cw_mode.set_max_length(4)
        self.entry_cw_mode.set_width_chars(6)
        self.entry_cw_pass = Gtk.Entry(text="")
        self.entry_cw_pass.set_placeholder_text("password del cifrado")
        cg.attach(Gtk.Label(label="Mode:", xalign=0), 0, 0, 1, 1)
        cg.attach(self.entry_cw_mode, 1, 0, 1, 1)
        cg.attach(Gtk.Label(label="Pass:", xalign=0), 2, 0, 1, 1)
        cg.attach(self.entry_cw_pass, 3, 0, 1, 1)
        b_cw = Gtk.Button(label="Aplicar CryptWrite")
        b_cw.connect("clicked", lambda *_: self.on_cryptwrite())
        cg.attach(b_cw, 0, 1, 2, 1)
        cw_note = Gtk.Label(
            label="El comando es 0x2F con el payload [mode][0x55][pass]. "
                  "Este firmware (7.1 / HW 1.1) no responde a 0x2E/0x2F, "
                  "asi que la funcion todavia no esta disponible: queda "
                  "como area en desarrollo.")
        cw_note.set_xalign(0.0)
        cw_note.set_line_wrap(True)
        cg.attach(cw_note, 0, 2, 4, 1)
        box.pack_start(crypt, False, False, 0)

        table = Gtk.Frame(label="Comandos conocidos del lector RU5600")
        tl = Gtk.Label(
            label="0x01 inventario   0x02 leer memoria   0x03 escribir "
                  "memoria   0x04 escribir EPC   0x05 lock   "
                  "0x10 informacion   0x20/0x21 parametro   "
                  "0x23/0x24 un parametro   0x3E/0x3F frecuencia   "
                  "0x30 reiniciar   0x40/0x41 start/stop read   "
                  "0x85/0x86 relay   0xE0 modulo   0xE1 antena   "
                  "0xE8-0xEC whitelist   0x2E/0x2F CryptWrite\n"
                  "BLOQUEADOS: 0x2B/0x2C/0x2D (actualizan el firmware del "
                  "lector) y 0x65 (Kill6C, mata un tag).")
        tl.set_xalign(0.0)
        tl.set_line_wrap(True)
        table.add(tl)
        box.pack_start(table, False, False, 0)

        lbl_tab = Gtk.Label(label="Risky")
        self._tab_labels.append(lbl_tab)
        self.nb.append_page(box, lbl_tab)


    def _risk_ok(self):
        if not self.chk_risk.get_active():
            self.log("Mark 'Entiendo el riesgo' to use these operations")
            return False
        if self.reader is None or not self.reader.is_open:
            self.log("Open the reader first")
            return False
        return True

    def on_lock(self, *_):
        if not self._risk_ok():
            return
        try:
            data = bytes.fromhex(self.entry_lock_data.get_text().strip())
            action = int(self.entry_lock_action.get_text().strip() or "0", 16)
        except ValueError:
            self.log("ERROR: lock data must be 4 hex bytes and the action "
                     "a hex byte")
            return
        if len(data) != 4:
            self.log("ERROR: the lock data must be exactly 4 bytes")
            return
        dlg = Gtk.MessageDialog(
            self, Gtk.DialogFlags.MODAL, Gtk.MessageType.WARNING,
            Gtk.ButtonsType.OK_CANCEL,
            f"Lock the tag permanently?\n\n"
            f"lock data : {data.hex().upper()}\n"
            f"action    : 0x{action:02X}\n\n"
            "A block lock cannot be undone and the library cannot read it "
            "back. If the tag must still be read, it will only be with a "
            "reader that knows the password.")
        if dlg.run() != Gtk.ResponseType.OK:
            dlg.destroy()
            return
        dlg.destroy()

        def task():
            return self.reader.lock_card(data, action, confirm=True)

        def ok(res):
            self.log("Lock sent: " + ("accepted" if res else "refused"))

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def on_raw_command(self, *_):
        if not self._risk_ok():
            return
        try:
            cmd = int(self.entry_raw_cmd.get_text().strip() or "0", 16)
            payload = bytes.fromhex(self.entry_raw_payload.get_text().strip()
                                    or "")
            timeout = int(self.entry_raw_timeout.get_text().strip() or 400)
        except ValueError:
            self.log("ERROR: the command and the payload are hex")
            return

        def task():
            return self.reader.raw_command(cmd, payload, timeout)

        def ok(res):
            self.log(f"Raw command 0x{cmd:02X} ({len(payload)} bytes): "
                     + ("the reader answered" if res else "no answer"))

        self.worker.run(task, on_ok=ok, on_err=self.log_error)


    def on_cryptwrite(self, *_):
        """CryptWrite (experimental).

        Sets the reader's encrypted-write mode: command 0x2F with the
        payload [mode][0x55][password].  The firmware of this reader
        (7.1) does not answer 0x2E/0x2F, so this is kept as an area in
        development instead of a working feature.
        """
        if not self._risk_ok():
            return
        try:
            mode = int(self.entry_cw_mode.get_text().strip() or "0", 16)
            pwd = self.entry_cw_pass.get_text().strip().encode()
        except ValueError:
            self.log("ERROR: the mode must be hex")
            return
        payload = bytes([mode & 0xFF, 0x55]) + pwd

        def task():
            return self.reader.raw_command(0x2F, payload, want_payload=True)

        def ok(res):
            good = res[0] if isinstance(res, tuple) else res
            if good:
                self.log("CryptWrite accepted by the reader")
            else:
                self.log("CryptWrite: the reader did not answer - this "
                         "firmware does not implement it yet")

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def _build_whitelist_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(8)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.pack_start(bar, False, False, 0)
        self.btn_wl_refresh = Gtk.Button(label="Refresh")
        self.btn_wl_refresh.connect("clicked", lambda *_: self.on_wl_refresh())
        self.btn_wl_clear = Gtk.Button(label="Delete all")
        self.btn_wl_clear.connect("clicked", lambda *_: self.on_wl_clear())
        self.btn_wl_import = Gtk.Button(label="Import")
        self.btn_wl_import.connect("clicked", lambda *_: self.on_wl_import())
        for b in (self.btn_wl_refresh, self.btn_wl_clear, self.btn_wl_import):
            bar.pack_start(b, False, False, 0)
        self.lbl_wl_count = Gtk.Label(label="entries: -")
        bar.pack_start(self.lbl_wl_count, False, False, 0)

        add = Gtk.Frame(label="Add one entry (32 bytes hex)")
        ag = Gtk.Grid(column_spacing=8, row_spacing=6)
        ag.set_border_width(8)
        add.add(ag)
        self.entry_wl_data = Gtk.Entry(text="")
        self.entry_wl_data.set_placeholder_text(
            "32 bytes in hex, eg the EPC padded with zeros")
        ag.attach(Gtk.Label(label="Entry:", xalign=0), 0, 0, 1, 1)
        ag.attach(self.entry_wl_data, 1, 0, 3, 1)
        b_add = Gtk.Button(label="Add")
        b_add.connect("clicked", lambda *_: self.on_wl_add())
        ag.attach(b_add, 0, 1, 1, 1)
        b_del = Gtk.Button(label="Delete one")
        b_del.connect("clicked", lambda *_: self.on_wl_delete())
        ag.attach(b_del, 1, 1, 1, 1)
        box.pack_start(add, False, False, 0)

        self.wl_store = Gtk.ListStore(str, str)
        view = Gtk.TreeView(model=self.wl_store)
        view.append_column(Gtk.TreeViewColumn("Index", Gtk.CellRendererText(),
                                               text=0))
        view.append_column(Gtk.TreeViewColumn("Entry (hex)",
                                               Gtk.CellRendererText(), text=1))
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.add(view)
        box.pack_start(scroll, True, True, 0)
        lbl_tab = Gtk.Label(label="WhiteList")
        self._tab_labels.append(lbl_tab)
        self.nb.append_page(box, lbl_tab)

    def _build_batchwrite_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(8)

        frame = Gtk.Frame(label="Write the same EPC to a batch of tags")
        g = Gtk.Grid(column_spacing=8, row_spacing=6)
        g.set_border_width(8)
        frame.add(g)
        self.entry_bw_epc = Gtk.Entry(text="")
        self.entry_bw_epc.set_placeholder_text("EPC in hex, eg 3034333234")
        self.entry_bw_total = Gtk.Entry(text="10")
        self.entry_bw_total.set_width_chars(5)
        self.chk_bw_check = Gtk.CheckButton(label="Verify after writing")
        self.chk_bw_lock = Gtk.CheckButton(
            label="Lock the tag after writing (irreversible)")
        self.chk_bw_lock.set_tooltip_text(
            "A block lock cannot be undone and the vendor does not "
            "document its parameters. Prefer locking one tag at a time "
            "from the Risky tab.")
        g.attach(Gtk.Label(label="FastWriteEPC:", xalign=0), 0, 0, 1, 1)
        g.attach(self.entry_bw_epc, 1, 0, 2, 1)
        g.attach(Gtk.Label(label="TotalCnt.:", xalign=0), 3, 0, 1, 1)
        g.attach(self.entry_bw_total, 4, 0, 1, 1)
        g.attach(self.chk_bw_check, 0, 1, 1, 1)
        g.attach(self.chk_bw_lock, 2, 1, 1, 1)
        self.btn_bw_start = Gtk.Button(label="Start")
        self.btn_bw_start.connect("clicked", lambda *_: self.on_bw_start())
        g.attach(self.btn_bw_start, 4, 1, 1, 1)
        self.btn_bw_stop = Gtk.Button(label="Stop")
        self.btn_bw_stop.set_sensitive(False)
        self.btn_bw_stop.connect("clicked", lambda *_: self.on_bw_stop())
        g.attach(self.btn_bw_stop, 5, 1, 1, 1)
        box.pack_start(frame, False, False, 0)

        self.bw_store = Gtk.ListStore(str, str, str, str)
        view = Gtk.TreeView(model=self.bw_store)
        for i, t in enumerate(("Tag EPC", "Result", "RSSI", "Time")):
            view.append_column(Gtk.TreeViewColumn(t, Gtk.CellRendererText(),
                                                  text=i))
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.add(view)
        box.pack_start(scroll, True, True, 0)
        lbl_tab = Gtk.Label(label="BatchWrite")
        self._tab_labels.append(lbl_tab)
        self.nb.append_page(box, lbl_tab)


    # -- white list -------------------------------------------------------
    def on_wl_refresh(self, *_):
        if self.reader is None or not self.reader.is_open:
            self.log("Open the reader first")
            return

        def task():
            count = self.reader.whitelist_count()
            entries = []
            for i in range(min(count, 64)):
                try:
                    entries.append(self.reader.whitelist_read(i))
                except Exception:
                    break
            return count, entries

        def ok(res):
            count, entries = res
            self.wl_store.clear()
            for i, e in enumerate(entries):
                self.wl_store.append([str(i), e.hex().upper()])
            self.lbl_wl_count.set_text(f"entries: {count}")
            if count > 1024:
                self.log(f"White list: {count} entries - that count is not "
                         f"credible, this reader's firmware does not "
                         f"implement the white list (the answer is stale "
                         f"data).")
            else:
                self.log(f"White list: {count} entries")

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def on_wl_add(self, *_):
        if self.reader is None or not self.reader.is_open:
            self.log("Open the reader first")
            return
        text = self.entry_wl_data.get_text().strip().replace(" ", "")
        try:
            entry = bytes.fromhex(text)
        except ValueError:
            self.log("ERROR: the entry must be hex")
            return
        if not entry:
            self.log("ERROR: the entry is empty")
            return

        def task():
            return self.reader.whitelist_write(entry)

        def ok(_):
            self.log(f"White list entry added ({len(entry)} bytes)")
            self.on_wl_refresh()

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def on_wl_delete(self, *_):
        if self.reader is None or not self.reader.is_open:
            self.log("Open the reader first")
            return
        text = self.entry_wl_data.get_text().strip().replace(" ", "")
        try:
            entry = bytes.fromhex(text)
        except ValueError:
            self.log("ERROR: the entry must be hex")
            return
        if not entry:
            self.log("ERROR: the entry is empty")
            return

        def task():
            return self.reader.whitelist_delete(entry)

        def ok(_):
            self.log("White list entry deleted")
            self.on_wl_refresh()

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def on_wl_clear(self, *_):
        if self.reader is None or not self.reader.is_open:
            self.log("Open the reader first")
            return
        dlg = Gtk.MessageDialog(
            self, Gtk.DialogFlags.MODAL, Gtk.MessageType.WARNING,
            Gtk.ButtonsType.OK_CANCEL,
            "Delete every entry of the reader's white list?")
        if dlg.run() != Gtk.ResponseType.OK:
            dlg.destroy()
            return
        dlg.destroy()

        def task():
            return self.reader.whitelist_clear()

        def ok(_):
            self.log("White list cleared")
            self.on_wl_refresh()

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def on_wl_import(self, *_):
        dlg = Gtk.FileChooserDialog(
            title="Import white list", parent=self,
            action=Gtk.FileChooserAction.OPEN)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        if dlg.run() != Gtk.ResponseType.OK:
            dlg.destroy()
            return
        path = dlg.get_filename()
        dlg.destroy()
        try:
            with open(path) as fh:
                entries = [ln.strip() for ln in fh if ln.strip()]
        except OSError as exc:
            self.log_error(str(exc))
            return
        if not self.reader or not self.reader.is_open:
            self.log("Open the reader first")
            return

        def task():
            done = 0
            for line in entries:
                try:
                    self.reader.whitelist_write(bytes.fromhex(line))
                    done += 1
                except Exception:
                    break
            return done

        def ok(done):
            self.log(f"Imported {done}/{len(entries)} white list entries")

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    # -- batch write ------------------------------------------------------
    def on_bw_start(self, *_):
        if self.reader is None or not self.reader.is_open:
            self.log("Open the reader first")
            return
        text = self.entry_bw_epc.get_text().strip().replace(" ", "")
        try:
            epc = bytes.fromhex(text)
        except ValueError:
            self.log("ERROR: the EPC must be hex")
            return
        if not epc:
            self.log("ERROR: the EPC is empty")
            return
        try:
            total = int(self.entry_bw_total.get_text() or "10")
        except ValueError:
            total = 10
        total = max(1, min(999, total))
        check = self.chk_bw_check.get_active()
        lock = self.chk_bw_lock.get_active()
        self.bw_store.clear()
        self._bw_state = {"stop": False, "done": 0, "seen": set()}
        self.btn_bw_start.set_sensitive(False)
        self.btn_bw_stop.set_sensitive(True)

        def step():
            if self._bw_state["stop"] or self._bw_state["done"] >= total:
                self.btn_bw_start.set_sensitive(True)
                self.btn_bw_stop.set_sensitive(False)
                self.log(f"Batch write finished: {self._bw_state['done']} "
                         f"tag(s)")
                return False
            try:
                tags = self.reader.scan(dwell=0.25, attempts=2)
            except Exception as exc:
                self.log_error(str(exc))
                return False
            for tag in tags:
                key = tag.epc_hex
                if key in self._bw_state["seen"]:
                    continue
                self._bw_state["seen"].add(key)
                result = "written"
                try:
                    self.reader.write_epc(epc)
                    self._bw_state["done"] += 1
                    if check:
                        back = self.reader.read_card(MEM_EPC, 2, 6)
                        if back != epc:
                            result = "verify FAILED"
                    if lock:
                        # un lock automatico destruye el tag sin permiso
                        # explicito: queda registrado, no se ejecuta solo
                        result += " (lock: hacelo en la tab Risky)"
                except Exception as exc:
                    result = f"ERROR: {exc}"
                self.bw_store.append([key, result, str(tag.rssi),
                                      time.strftime("%H:%M:%S")])
            return True

        GLib.timeout_add(500, step)

    def on_bw_stop(self, *_):
        if hasattr(self, "_bw_state"):
            self._bw_state["stop"] = True
        self.btn_bw_start.set_sensitive(True)
        self.btn_bw_stop.set_sensitive(False)
        self.log("Batch write stopped")

    def refresh_devices(self):
        self.cb_device.remove_all()
        devices = _usb_readers()
        for node, product in devices:
            self.cb_device.append_text(f"{USB_VID}:{USB_PID} ({product})")
        if not devices:
            self.cb_device.append_text("No reader found")
        self.cb_device.set_active(0)

    def on_scan_port(self, *_):
        self.refresh_devices()
        self.log(f"Scan: {len(_usb_readers())} USB reader(s) found")

    def on_scan_network(self, *_):
        self.log("ScanNetwork: not supported by this build (USB transport)")

    def on_tcp_server(self, *_):
        self.log("TCPServer: not supported by this build (USB transport)")

    def on_help(self, *_):
        dlg = Gtk.MessageDialog(
            self, Gtk.DialogFlags.MODAL, Gtk.MessageType.INFO,
            Gtk.ButtonsType.OK,
            "RU5600 Reader (unofficial Linux client)\n\n"
            "Transport : USB via libusb (vendor library libCFHidApi)\n"
            "Device    : WCH 1a86:e010\n"
            "If Open fails, install the udev rule so the reader is\n"
            "readable without root.")
        dlg.run()
        dlg.destroy()

    def on_install_udev(self, *_):
        """Install the udev rule with a polkit prompt (needs admin rights)."""
        import shutil
        import subprocess

        rule = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "99-inveton-ru5600.rules")
        if not os.path.exists(rule):
            rule = "/opt/ru5600-reader/99-inveton-ru5600.rules"
        if not os.path.exists(rule):
            self.log("udev rule not found next to the app")
            return
        target = "/etc/udev/rules.d/99-inveton-ru5600.rules"
        script = (f"install -m 644 {rule} {target} && "
                  "udevadm control --reload-rules && "
                  "udevadm trigger --subsystem-match=usb "
                  "--attr-match=idVendor=1a86")
        if shutil.which("pkexec") is None:
            self.log("pkexec not available - run the install script manually")
            return
        self.log("Requesting administrator rights to install the udev rule...")
        try:
            proc = subprocess.run(["pkexec", "sh", "-c", script],
                                  capture_output=True, text=True)
        except OSError as exc:
            self.log_error(str(exc))
            return
        if proc.returncode == 0:
            self.log("udev rule installed - unplug and replug the reader")
        else:
            self.log(f"udev rule install failed (rc={proc.returncode})")

    def on_export_config(self, *_):
        dlg = Gtk.FileChooserDialog(
            title="Export configuration", parent=self,
            action=Gtk.FileChooserAction.SAVE)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        dlg.set_current_name("ru5600-config.ini")
        if dlg.run() == Gtk.ResponseType.OK:
            path = dlg.get_filename()
            cfg = self.collect_config()
            with open(path, "w") as fh:
                for k, v in cfg.items():
                    fh.write(f"{k}={v}\n")
            self.log(f"Configuration exported to {path}")
        dlg.destroy()

    def on_import_config(self, *_):
        dlg = Gtk.FileChooserDialog(
            title="Import configuration", parent=self,
            action=Gtk.FileChooserAction.OPEN)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        if dlg.run() == Gtk.ResponseType.OK:
            path = dlg.get_filename()
            try:
                with open(path) as fh:
                    for line in fh:
                        if "=" in line:
                            k, v = line.strip().split("=", 1)
                            self.apply_config(k, v)
                self.log(f"Configuration imported from {path}")
            except OSError as exc:
                self.log_error(str(exc))
        dlg.destroy()

    def collect_config(self):
        return {
            "rfpower": self.cb_rfpower.get_active_text(),
            "freqband": self.cb_freqband.get_active_text(),
            "workmode": self.cb_workmode.get_active_text(),
            "filtertime": self.entry_filter.get_text(),
            "buzzer": "1" if self.chk_buzzer.get_active() else "0",
            "startaddr": self.entry_start.get_text(),
            "bytelength": self.entry_bytelen.get_text(),
            "readtime": self.entry_readtime.get_text(),
            "outformat": self.cb_outfmt.get_active_text(),
        }

    def apply_config(self, key, value):
        table = {
            "rfpower": self.cb_rfpower,
            "freqband": self.cb_freqband,
            "workmode": self.cb_workmode,
            "outformat": self.cb_outfmt,
        }
        if key in table:
            cb = table[key]
            for i in range(cb.get_model()[0]):
                if cb.get_model()[i][0] == value:
                    cb.set_active(i)
                    return
        entry = {
            "filtertime": self.entry_filter,
            "startaddr": self.entry_start,
            "bytelength": self.entry_bytelen,
            "readtime": self.entry_readtime,
        }.get(key)
        if entry is not None:
            entry.set_text(value)
        elif key == "buzzer":
            self.chk_buzzer.set_active(value == "1")

    def on_default(self, *_):
        self.cb_rfpower.set_active(15)
        self.cb_freqband.set_active(0)
        self.cb_workmode.set_active(0)
        self.entry_filter.set_text("0")
        self.entry_start.set_text("00")
        self.entry_bytelen.set_text("00")
        self.entry_readtime.set_text("2")
        self.chk_buzzer.set_active(True)
        self.log("Default parameters restored")

    def _make_reader(self):
        """Create the reader for the selected transport."""
        if self.rb_net.get_active():
            ip = self.entry_ip.get_text().strip() or "192.168.1.250"
            port = int(self.entry_port.get_text().strip() or 60000)
            self.log(f"Using the network transport: {ip}:{port}")
            return NetReader(ip, port, on_tags=self._on_tags_threadsafe)
        if self.rb_com.get_active():
            self.log("The serial transport is not available in the Linux "
                     "library - using USB")
        self.log("Opening USB reader...")
        return Ru5600Reader(on_tags=self._on_tags_threadsafe)

    def on_silence(self, *_):
        """Turn the reader's buzzer off (it beeps on every tag read)."""
        if self.reader is None or not self.reader.is_open:
            self.log("Open the reader first")
            return

        def task():
            self.reader.set_beep(False)
            return True

        def ok(_):
            self.chk_buzzer.set_active(False)
            self.log("Buzzer off: the reader will not beep anymore")

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def on_open(self, *_):
        if self.rb_com.get_active():
            self.log("The serial transport is not in the Linux library - "
                     "using USB")

        def task():
            if self.reader is None or not self.reader.is_open:
                self.reader = self._make_reader()
            self.reader.open()
            return self.reader.system_info()

        self.worker.run(
            task,
            on_ok=self._on_opened,
            on_err=self.log_error,
        )

    def _on_opened(self, info):
        self.entry_sn.set_text(info.device_sn)
        self.entry_fw.set_text(info.soft_ver)
        self.entry_hw.set_text(info.hard_ver)
        self.btn_open.set_sensitive(False)
        self.btn_close.set_sensitive(True)
        self.log(f"Open USB successfully - SN {info.device_sn}, "
                 f"firmware {info.soft_ver}, hardware {info.hard_ver}")
        self.load_params()
        # the reader beeps on every tag it reads, which is unbearable
        # when it is next to you: it is silenced on opening
        try:
            self.reader.set_beep(False)
            self.chk_buzzer.set_active(False)
            self.log("Buzzer off (the reader beeps on every tag read)")
        except Exception:
            pass

    def on_close(self, *_):
        try:
            self.reader.close()
        except Exception as exc:  # noqa: BLE001
            self.log_error(str(exc))
            return
        self.btn_open.set_sensitive(True)
        self.btn_close.set_sensitive(False)
        self.lbl_active.set_text("Status: stopped")
        self.log("Reader closed")

    def load_params(self):
        def task():
            return {
                "rf": self.reader.rf_power(),
                "mode": self.reader.work_mode(),
                "beep": self.reader.beep(),
                "filter": self.reader.filter_time(),
                "freq": self.reader.read_freq(),
            }

        def ok(data):
            self.cb_rfpower.set_active(min(data["rf"], 30))
            self.cb_workmode.set_active(1 if data["mode"] else 0)
            self.chk_buzzer.set_active(bool(data["beep"]))
            self.entry_filter.set_text(str(data["filter"]))
            self.lbl_freq_raw.set_text(
                "raw freq: " + data["freq"].hex().upper())
            self.log("Parameters loaded from reader")

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def on_read(self, *_):
        if self.reader is None or not self.reader.is_open:
            self.log("Open the reader first")
            return
        self.log("Read started...")

        def task():
            # detect() uses active mode - the answer mode inventory is
            # refused by the reader most of the time - and the read mode
            # picks how hard it tries.
            return self.reader.detect(self._read_mode())

        def ok(tags):
            if not tags:
                self.log("Read finished: no tags found")
            else:
                self.log(f"Read finished: {len(tags)} tag(s)")
            self.update_tag_table(tags)

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def on_set(self, *_):
        if self.reader is None or not self.reader.is_open:
            self.log("Open the reader first")
            return

        def task():
            self.reader.set_rf_power(int(self.cb_rfpower.get_active_text()[:-3]))
            mode = 1 if self.cb_workmode.get_active_text() == "ActiveMode" else 0
            self.reader.set_work_mode(mode)
            self.reader.set_beep(self.chk_buzzer.get_active())
            self.reader.set_filter_time(int(self.entry_filter.get_text() or 0))
            return True

        def ok(_):
            self.log("Parameters applied to reader")

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def _read_mode(self) -> str:
        labels = {"Burst (fastest)": "burst", "Balanced": "balanced",
                  "Reliable (8/8)": "reliable"}
        return labels.get(self.cb_read_mode.get_active_text(), "balanced")

    def _access_password(self) -> bytes:
        text = self.entry_password.get_text().strip().replace(" ", "")
        try:
            pwd = bytes.fromhex(text)
        except ValueError:
            pwd = b""
        if len(pwd) != 4:
            pwd = b"\x00" * 4
        return pwd

    def on_read_memory(self, *_):
        if self.reader is None or not self.reader.is_open:
            self.log("Open the reader first")
            return
        mem_map = {"EPC": MEM_EPC, "TID": MEM_TID, "USER": MEM_USER}
        mem = mem_map.get(self.cb_mem.get_active_text(), MEM_EPC)
        try:
            wptr = int(self.entry_wordptr.get_text() or "0", 16)
            wcount = int(self.entry_wordcount.get_text() or "0", 16)
        except ValueError:
            self.log("Invalid address / length (use hex)")
            return
        pwd = self._access_password()

        def task():
            return self.reader.read_card(mem, wptr, wcount, pwd)

        def ok(data):
            self.mem_output.set_text(data.hex().upper())
            bank = self.cb_mem.get_active_text()
            extra = ""
            if bank == "EPC" and wptr == 0 and len(data) >= 4:
                extra = ("  (word 0-1 = CRC + PC header, the EPC starts at "
                         "word 2)")
            self.log(f"Read {wcount} word(s) from {bank}: "
                     f"{data.hex().upper()}{extra}")

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def on_fp_learn(self, *_):
        if self.reader is None or not self.reader.is_open:
            self.log("Open the reader first")
            return

        def task():
            tags = self.reader.scan()
            if not tags:
                raise ReaderError("no tag detected")
            return tags[0].epc, learn_fingerprint(self.reader, tags[0].epc)

        def ok(res):
            epc, tid = res
            self.lbl_fp.set_text(f"{epc.hex().upper()} -> {tid}")
            self.log(f"Fingerprint learned: {epc.hex().upper()} -> {tid}")

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def on_write_memory(self, *_):
        """Write a chunk of memory and verify it by reading it back."""
        if self.reader is None or not self.reader.is_open:
            self.log("Open the reader first")
            return
        text = self.entry_write_data.get_text().strip().replace(" ", "")
        try:
            data = bytes.fromhex(text)
        except ValueError:
            self.log("The data must be an even-length hex string")
            return
        if not data or len(data) % 2:
            self.log("The data must be an even-length hex string")
            return
        try:
            word_ptr = int(self.entry_write_ptr.get_text().strip() or "0", 16)
        except ValueError:
            self.log("The word pointer must be in hex")
            return
        bank_id = self.cb_write_bank.get_active_id()
        mem = int(bank_id) if bank_id is not None else 3
        bank_name = self.cb_write_bank.get_active_text() or "USER"
        if mem == 0:
            if not self.chk_fp.get_active():
                self.log("Writing the RESERVED bank changes the passwords "
                         "of the tag: arm the fingerprint check first")
                return

        def task():
            self.reader.write_card_verified(data, mem, word_ptr,
                                            self._access_password())
            return True

        def ok(_):
            self.log(f"{bank_name}: {len(data)} B written at word "
                     f"{word_ptr} and read back OK")

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def on_write_epc(self, *_):
        if self.reader is None or not self.reader.is_open:
            self.log("Open the reader first")
            return
        text = self.entry_write_epc.get_text().strip().replace(" ", "")
        try:
            data = bytes.fromhex(text)
        except ValueError:
            self.log("EPC must be an even-length hex string")
            return
        if len(data) % 2:
            self.log("EPC length must be a multiple of 2 bytes")
            return

        def task():
            if self.chk_fp.get_active():
                tags = self.reader.scan()
                if not tags:
                    raise ReaderError("no tag detected")
                ok, msg = check_fingerprint(self.reader, tags[0].epc)
                if not ok:
                    raise ReaderError("fingerprint check failed: " + msg)
                self.log("Fingerprint: " + msg)
            return self.reader.write_epc(data, self._access_password())

        def ok(_):
            self.log(f"EPC written: {data.hex().upper()}")

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def on_start_read(self, *_):
        if self.reader is None or not self.reader.is_open:
            self.log("Open the reader first")
            return
        self.reader.clear_tag_buf()
        self._tag_counts.clear()
        self._tag_order.clear()
        self.tag_store.clear()

        def task():
            self.reader.set_work_mode(1)
            return self.reader.start_read()

        def ok(_):
            self._active = True
            self.lbl_active.set_text("Status: reading")
            self.log("ActiveMode: start reading")

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def on_stop_read(self, *_):
        if self.reader is None or not self.reader.is_open:
            return

        def task():
            self.reader.stop_read()
            # Back to answer mode so the one shot Read button works again.
            self.reader.set_work_mode(0)
            return True

        def ok(_):
            self._active = False
            self.lbl_active.set_text("Status: stopped")
            self.log("ActiveMode: stop reading (back to AnswerMode)")

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def on_measure(self, *_):
        if self.reader is None or not self.reader.is_open:
            self.log("Open the reader first")
            return
        try:
            seconds = float(self.entry_measure.get_text() or "10")
        except ValueError:
            seconds = 10.0
        seconds = max(1.0, min(300.0, seconds))
        self.log(f"Measuring for {seconds:.0f} s...")

        def task():
            return self.reader.measure(seconds)

        def ok(st):
            self.lbl_active.set_text(
                f"Status: {st['detections']} detections "
                f"({st['detections_per_s']:.1f}/s), "
                f"RSSI avg {st['rssi_avg']:.0f} "
                f"min {st['rssi_min']} max {st['rssi_max']}")
            self.log(
                f"Measure {st['elapsed']:.1f}s: {st['detections']} detections "
                f"({st['detections_per_s']:.2f}/s), RSSI avg {st['rssi_avg']:.1f} "
                f"min {st['rssi_min']} max {st['rssi_max']}, "
                f"tags {st['tags']}")

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def on_relay(self, on: bool, *_):
        if self.reader is None or not self.reader.is_open:
            self.log("Open the reader first")
            return

        def task():
            return self.reader.relay(on)

        def ok(_):
            self.log("Relay " + ("closed" if on else "released"))

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def on_read_freq(self, *_):
        if self.reader is None or not self.reader.is_open:
            return

        def task():
            return self.reader.read_freq()

        def ok(raw):
            self.lbl_freq_raw.set_text("raw freq: " + raw.hex().upper())
            self.log(f"Frequency read: {raw.hex().upper()}")

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def on_set_freq(self, *_):
        if self.reader is None or not self.reader.is_open:
            return
        band = self.cb_freq_adv.get_active_text()

        def task():
            return self.reader.set_freq(band)

        def ok(_):
            self.log(f"Frequency set to {band}")

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    def on_reboot(self, *_):
        if self.reader is None or not self.reader.is_open:
            return

        def task():
            return self.reader.reboot()

        def ok(_):
            self.log("Reboot command sent")

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    # -------------------------------------------------------------- tags
    def _on_tags_threadsafe(self, tags):
        GLib.idle_add(self.update_tag_table, tags)

    def update_tag_table(self, tags):
        now = time.strftime("%H:%M:%S")
        for tag in tags:
            blob = bytes(tag.raw_id) if tag.raw_id else bytes(tag.epc)
            key = blob.hex().upper()
            crudo = " ".join(f"{b:02X}" for b in blob)
            if key not in self._tag_counts:
                self._tag_counts[key] = 0
                self._tag_order.append(key)
            self._tag_counts[key] += 1
            row = self._find_row(key)
            if row is None:
                # the sequential number is given when the tag is read
                # for the first time and does not change afterwards
                row = self.tag_store.append([str(len(self._tag_order)),
                                             str(tag.ant), crudo, "...",
                                             "...", str(tag.rssi), "1",
                                             now])
                self._identify_tag(key, blob)
            else:
                row[5] = str(tag.rssi)
                row[6] = str(self._tag_counts[key])
                row[7] = now
        return False

    def _find_row(self, key):
        compact = key.replace(" ", "")
        for row in self.tag_store:
            if row[2].replace(" ", "") == compact:
                return row
        return None

    def _identify_tag(self, key, blob):
        """Chip-first machine interpretation under the raw (worker thread).

        Direct TID read (bank 2) + decode -> chip -> per-chip interpreter
        for the blob.  Unknown chips keep the raw and show pending: no
        guessing, and no write path touches this.  H9/H3/ZKTeco/Dogbone
        all enter here; only chips with evidence-backed interpreters in
        `interpret_active_blob` get an EPC, the rest wait for hardware
        evidence (Creador 2026-10-26).
        """
        if self.reader is None or not self.reader.is_open:
            return

        def task():
            from ru5600_core import interpret_active_blob
            from ru5600_chip import decode_tid
            votes: dict = {}
            for _ in range(3):
                try:
                    tid = self.reader.read_card(2, 0, 6, b"\x00" * 4,
                                                retries=1)
                except Exception:  # noqa: BLE001 - flaky reader, vote on
                    continue
                if tid and len(tid) >= 4 and tid[0] == 0xE2:
                    votes[tid] = votes.get(tid, 0) + 1
            if not votes:
                return (key, None, "TID no leido")
            tid = max(votes.items(), key=lambda kv: kv[1])[0]
            try:
                info = decode_tid(tid)
                prefix = tid[:4].hex().upper()
                chip = (f"{info.manufacturer} {info.model}"
                        if info.known else
                        f"desconocido ({prefix})")
            except Exception:  # noqa: BLE001 - never break the table
                return (key, None, "TID no decodificado")
            epc, _, pending = interpret_active_blob(blob, prefix)
            if pending is not None:
                return (key, None, f"{chip} - interpreter pending")
            return (key, epc.hex().upper(), chip)

        def ok(res):
            k, epc_hex, chip = res
            row = self._find_row(k)
            if row is None:
                return
            row[3] = epc_hex if epc_hex else "-"
            row[4] = chip

        self.worker.run(task, on_ok=ok, on_err=self.log_error)

    # --------------------------------------------------------------- log
    def log(self, message):
        self.log_store.append([time.strftime("%H:%M:%S"), message])

    def log_error(self, exc):
        self.log_store.append([time.strftime("%H:%M:%S"), f"ERROR: {exc}"])

    def _on_destroy(self, *_):
        try:
            self.reader.close()
        except Exception:
            pass
        Gtk.main_quit()


def main():
    app = ReaderApp()
    app.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
