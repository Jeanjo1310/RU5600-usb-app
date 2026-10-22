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
from gi.repository import GLib, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ru5600_net import NetReader  # noqa: E402
from ru5600_core import (  # noqa: E402
    FREQ_BANDS,
    READ_MODES,
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
    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add(root)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        root.pack_start(paned, True, True, 0)

        # ---------------- left: connection -------------------------------
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left.set_size_request(290, -1)
        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        left_scroll.set_min_content_width(290)
        left_scroll.add(left)
        paned.pack1(left_scroll, False, False)

        conn = Gtk.Frame(label="Connection")
        lv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        conn.add(lv)

        self.rb_usb = Gtk.RadioButton.new_with_label_from_widget(None, "USB")
        self.rb_com = Gtk.RadioButton.new_with_label_from_widget(self.rb_usb, "COM")
        self.rb_net = Gtk.RadioButton.new_with_label_from_widget(self.rb_usb, "Net")
        for rb in (self.rb_usb, self.rb_com, self.rb_net):
            lv.pack_start(rb, False, False, 0)
        self.rb_usb.set_active(True)

        grid = Gtk.Grid(column_spacing=6, row_spacing=4)
        grid.set_column_homogeneous(False)
        lv.pack_start(grid, False, False, 0)

        self.cb_baud = Gtk.ComboBoxText()
        for rate in ("9600bps", "19200bps", "38400bps", "57600bps",
                     "115200bps", "230400bps"):
            self.cb_baud.append_text(rate)
        self.cb_baud.set_active(4)
        grid.attach(Gtk.Label(label="Baudrate:", xalign=0), 0, 0, 1, 1)
        grid.attach(self.cb_baud, 1, 0, 1, 1)

        self.cb_device = Gtk.ComboBoxText()
        grid.attach(Gtk.Label(label="Device:", xalign=0), 0, 1, 1, 1)
        grid.attach(self.cb_device, 1, 1, 1, 1)

        self.cb_transport = Gtk.ComboBoxText()
        for t in ("USB", "COM", "Net"):
            self.cb_transport.append_text(t)
        self.cb_transport.set_active(0)
        grid.attach(Gtk.Label(label="Interface:", xalign=0), 0, 2, 1, 1)
        grid.attach(self.cb_transport, 1, 2, 1, 1)

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
        grid.attach(Gtk.Label(label="Read mode:", xalign=0), 0, 5, 1, 1)
        grid.attach(self.cb_read_mode, 1, 5, 1, 1)

        self.entry_ip = Gtk.Entry(text="192.168.1.250")
        grid.attach(Gtk.Label(label="Net IP:", xalign=0), 0, 3, 1, 1)
        grid.attach(self.entry_ip, 1, 3, 1, 1)

        self.entry_port = Gtk.Entry(text="60000")
        grid.attach(Gtk.Label(label="NetPort:", xalign=0), 0, 4, 1, 1)
        grid.attach(self.entry_port, 1, 4, 1, 1)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        lv.pack_start(btns, False, False, 0)
        self.btn_open = Gtk.Button(label="Open")
        self.btn_close = Gtk.Button(label="Close")
        self.btn_close.set_sensitive(False)
        self.btn_open.connect("clicked", lambda *_: self.on_open())
        self.btn_close.connect("clicked", lambda *_: self.on_close())
        btns.pack_start(self.btn_open, True, True, 0)
        btns.pack_start(self.btn_close, True, True, 0)

        btns2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        lv.pack_start(btns2, False, False, 0)
        for label, cb in (("ScanPort", self.on_scan_port),
                          ("ScanNetwork", self.on_scan_network)):
            b = Gtk.Button(label=label)
            b.connect("clicked", cb)
            btns2.pack_start(b, True, True, 0)

        btns3 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        lv.pack_start(btns3, False, False, 0)
        for label, cb in (("TCPServer", self.on_tcp_server),
                          ("Help", self.on_help)):
            b = Gtk.Button(label=label)
            b.connect("clicked", cb)
            btns3.pack_start(b, True, True, 0)

        self.btn_udev = Gtk.Button(label="Install USB access rule")
        self.btn_udev.set_tooltip_text(
            "Installs the udev rule that lets non-root users open the "
            "reader (needs the administrator password)")
        self.btn_udev.connect("clicked", self.on_install_udev)
        lv.pack_start(self.btn_udev, False, False, 0)

        left.pack_start(conn, False, False, 0)

        # device identity
        info = Gtk.Frame(label="Device")
        ig = Gtk.Grid(column_spacing=6, row_spacing=4)
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
        left.pack_start(info, False, False, 0)

        cfg = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        left.pack_start(cfg, False, False, 0)
        for label, cb in (("ExportConfig", self.on_export_config),
                          ("ImportConfig", self.on_import_config)):
            b = Gtk.Button(label=label)
            b.connect("clicked", cb)
            cfg.pack_start(b, True, True, 0)

        relay = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        left.pack_start(relay, False, False, 0)
        for label, cb in (("CloseRelay", lambda *_: self.on_relay(False)),
                          ("ReleaseRelay", lambda *_: self.on_relay(True))):
            b = Gtk.Button(label=label)
            b.connect("clicked", cb)
            relay.pack_start(b, True, True, 0)

        opts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        left.pack_start(opts, False, False, 0)
        self.chk_skin = Gtk.CheckButton(label="Skin")
        self.chk_skin.set_active(True)
        opts.pack_start(self.chk_skin, False, False, 0)
        self.cb_lang = Gtk.ComboBoxText()
        for lang in ("English", "Spanish", "Portuguese"):
            self.cb_lang.append_text(lang)
        self.cb_lang.set_active(0)
        opts.pack_start(self.cb_lang, True, True, 0)

        # ---------------- right: notebook --------------------------------
        self.nb = Gtk.Notebook()
        self.nb.set_scrollable(True)
        paned.pack2(self.nb, True, False)
        self._build_param_tab()
        self._build_readwrite_tab()
        self._build_active_tab()
        self._build_advance_tab()
        self._build_chip_tab()
        self._build_risky_tab()
        self._build_batch_tab()

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
        self.chk_buzzer.set_active(True)
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
        box.pack_start(rs, False, False, 0)

        kb = Gtk.Frame(label="Keyboard")
        kg = Gtk.Grid(column_spacing=8, row_spacing=6)
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

        self.nb.append_page(box, Gtk.Label(label="ParameterSet"))

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

        frame2 = Gtk.Frame(label="Write EPC")
        g2 = Gtk.Grid(column_spacing=8, row_spacing=6)
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
        self.nb.append_page(box, Gtk.Label(label="Read/Write"))

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

        self.tag_store = Gtk.ListStore(str, str, str, str, str)
        view = Gtk.TreeView(model=self.tag_store)
        for i, title in enumerate(("Ant", "EPC / Tag ID", "RSSI", "Count",
                                   "Last seen")):
            view.append_column(Gtk.TreeViewColumn(title, Gtk.CellRendererText(),
                                                  text=i))
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.add(view)
        box.pack_start(scroll, True, True, 0)
        self.nb.append_page(box, Gtk.Label(label="ActiveMode"))

    def _build_advance_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(8)

        frame = Gtk.Frame(label="Frequency")
        g = Gtk.Grid(column_spacing=8, row_spacing=6)
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
        frame2.add(g2)
        b_reboot = Gtk.Button(label="Reboot reader")
        b_reboot.connect("clicked", lambda *_: self.on_reboot())
        g2.attach(b_reboot, 0, 0, 1, 1)
        self.lbl_freq_raw = Gtk.Label(label="raw freq: -")
        g2.attach(self.lbl_freq_raw, 1, 0, 1, 1)
        box.pack_start(frame2, False, False, 0)
        self.nb.append_page(box, Gtk.Label(label="AdvanceSet"))

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
        self.nb.append_page(box, Gtk.Label(label="Chip info"))

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

        table = Gtk.Frame(label="Comandos conocidos del lector RU5600")
        tl = Gtk.Label(
            label="0x01 inventario   0x02 leer memoria   0x03 escribir "
                  "memoria   0x04 escribir EPC   0x05 lock   "
                  "0x10 informacion   0x20/0x21 parametro   "
                  "0x23/0x24 un parametro   0x3E/0x3F frecuencia   "
                  "0x30 reiniciar   0x40/0x41 start/stop read   "
                  "0x85/0x86 relay   0xE8-0xEC whitelist")
        tl.set_xalign(0.0)
        tl.set_line_wrap(True)
        table.add(tl)
        box.pack_start(table, False, False, 0)

        self.nb.append_page(box, Gtk.Label(label="Risky"))


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

    def _build_batch_tab(self):
        for title in ("BatchWrite", "WhiteList"):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            box.set_border_width(8)
            lbl = Gtk.Label(
                label=f"{title} is not exposed by the vendor Linux API "
                      "(libCFHidApi) and is not implemented here.",
                wrap=True)
            box.pack_start(lbl, False, False, 0)
            self.nb.append_page(box, Gtk.Label(label=title))

    # ------------------------------------------------------------ actions
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
            key = tag.epc_hex
            if key not in self._tag_counts:
                self._tag_counts[key] = 0
                self._tag_order.append(key)
            self._tag_counts[key] += 1
            row = self._find_row(key)
            if row is None:
                row = self.tag_store.append([str(tag.ant), key, str(tag.rssi),
                                             "1", now])
            else:
                row[2] = str(tag.rssi)
                row[3] = str(self._tag_counts[key])
                row[4] = now
        return False

    def _find_row(self, key):
        for row in self.tag_store:
            if row[1] == key:
                return row
        return None

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
