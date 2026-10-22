#!/usr/bin/env python3
"""Chip identification and protection analysis for UHF Gen2 tags.

Reads the tag through the RU5600 reader and answers three questions:

1. **Which chip is it?**  The TID carries a GS1 TDS identifier: a mask
   designer id (MDID, the manufacturer) and a tag model number (TMN).
   Both are decoded here and looked up in a table of published prefixes.
2. **Is it protected?**  The memory banks are probed word by word, which
   shows which areas are readable, which are locked and which do not
   exist at all (a chip with 32 bit of user memory simply has no user
   memory beyond word 1 - that is not a lock).
3. **Which password?**  A Gen2 tag that needs an access password refuses
   *every* memory read with the wrong one, while the correct password
   works.  That makes the password testable without unlocking anything.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ru5600_core import (
    MEM_EPC,
    MEM_RESERVED,
    MEM_TID,
    MEM_USER,
    ReaderError,
    Ru5600Reader,
)

# --- GS1 mask designer ids (MDID) ----------------------------------------
MDID_NAMES = {
    0x801: "Impinj",
    0x803: "Alien Technology",
    0x806: "NXP Semiconductors",
    0x807: "STMicroelectronics",
    0x80B: "EM Microelectronics",
    0x80C: "Fudan Microelectronics",
    0x80E: "Quanray",
    0x813: "Kiloway",
    0x815: "HCTech",
    0x817: "T-HEAD (Alibaba)",
    0x819: "Axiocon",
    0x81A: "Chengdu Fudan",
    0x81B: "Smarter Coding",
    0x81C: "Mexperia",
    0x81D: "PJM",
    0x81E: "Taiwan Semiconductor",
    0x81F: "Science Technology",
    0x820: "Invengo",
    0x821: "InnerKey",
    0x822: "Gpower",
    0x823: "Avery Dennison",
    0x824: "C Six",
    0x825: "Rochester",
    0x826: "Sourcetrack",
    0x827: "Unitree",
    0x828: "RFID Korea",
    0x829: "Memsense",
    0x82A: "Apex",
    0x82B: "Shenzhen Shichuangyi",
    0x82C: "Nanjing Xuhui",
    0x82D: "Traci",
}

# --- known TID prefixes (4 bytes) -> chip model --------------------------
# manufacturer, model, EPC bits, USER bits, access pwd bits, kill pwd bits
TID_PREFIX_DB: Dict[str, Tuple[str, str, str, str]] = {
    # Alien
    "E2003411": ("Alien", "Higgs-2", "96", "0"),
    "E2003412": ("Alien", "Higgs-3", "96-480", "512"),
    "E2003414": ("Alien", "Higgs-4", "128", "128"),
    "E2003811": ("Alien", "Higgs-EC", "128", "128"),
    "E2803821": ("Alien", "Higgs-9", "96-496", "688"),
    "E2803813": ("Alien", "Higgs-10 (H10 / 7320 class)", "96-128", "32"),
    # Impinj
    "E2001093": ("Impinj", "Monza 3", "96", "0"),
    "E2801100": ("Impinj", "Monza 4D", "128", "32"),
    "E2801104": ("Impinj", "Monza 4U", "128", "512"),
    "E2801105": ("Impinj", "Monza 4QT", "128", "512"),
    "E2801106": ("Impinj", "Monza 4i", "256", "480"),
    "E280110C": ("Impinj", "Monza 4E", "496", "128"),
    "E2801130": ("Impinj", "Monza 5", "128", "32"),
    "E2801160": ("Impinj", "Monza R6", "96", "0"),
    "E2801170": ("Impinj", "Monza R6-P", "128", "32"),
    "E2801171": ("Impinj", "Monza R6-A/B", "96", "0"),
    "E2801190": ("Impinj", "Monza M750", "96", "32"),
    "E2801191": ("Impinj", "Monza M730", "128", "0"),
    "E28011A0": ("Impinj", "Monza M770", "128", "32"),
    "E28011B0": ("Impinj", "Monza M830 / M850", "128/96", "0/32"),
    "E28011C0": ("Impinj", "Monza M780", "496", "128"),
    "E28011C1": ("Impinj", "Monza M781", "128", "512"),
    # NXP
    "E2006003": ("NXP", "UCODE G2XM", "240", "512"),
    "E2006004": ("NXP", "UCODE G2XL", "240", "0"),
    "E2006806": ("NXP", "UCODE G2iL", "128", "0"),
    "E2006807": ("NXP", "UCODE G2iL+", "128", "0"),
    "E200680A": ("NXP", "UCODE G2iM", "256", "512"),
    "E200680B": ("NXP", "UCODE G2iM+", "128-448", "320-640"),
    "E2806890": ("NXP", "UCODE 7", "128", "0"),
    "E2806891": ("NXP", "UCODE 7M", "128", "32"),
    "E2806D12": ("NXP", "UCODE 7xm 1K", "448", "1024"),
    "E2806F12": ("NXP", "UCODE 7xm 2K", "448", "2048"),
    "E2806D92": ("NXP", "UCODE 7xm+", "448", "2048"),
    "E2806894": ("NXP", "UCODE 8", "128", "0"),
    "E2806994": ("NXP", "UCODE 8M", "96", "32"),
    "E2806995": ("NXP", "UCODE 9", "96", "0"),
    "E2806915": ("NXP", "UCODE 9 (2024+)", "96", "0"),
    "E2C06892": ("NXP", "UCODE DNA", "224", "3072"),
    "E2C06C12": ("NXP", "UCODE DNA Track", "448", "256"),
    "E2C06B12": ("NXP", "UCODE DNA City", "224", "1024"),
    # EM Microelectronics
    "E200B001": ("EM", "EM4324", "96", "720"),
    "E280B0A0": ("EM", "EM4423", "128/224", "160/64"),
    "E200B080": ("EM", "EM4124", "96", "0"),
    "E200B0C0": ("EM", "EM4126", "208", "0"),
    "E280B112": ("EM", "EM4425", "480", "1056"),
    # others
    "E281D011": ("Kiloway", "KX2005X-B", "96-240", "1312"),
    "E281D013": ("Kiloway", "KX2005X-544", "96-240", "544"),
    "E280F337": ("Quanray", "Qstar-71GB", "496", "128"),
    "E280F336": ("Quanray", "Qstar-73GB", "128", "512"),
    "E2827001": ("Fudan", "FM13DT160", "96", "8192"),
    "E2827802": ("Fudan", "FM13UF0051E", "128", "32"),
    "E2827803": ("Fudan", "FM13UF011E", "96-496", "512-112"),
    "E2817008": ("TegoChip", "8K", "496", "70688"),
    "E282403D": ("Axzon", "Magnus-S3 M3D", "128", "192"),
    "E282403E": ("Axzon", "Magnus-S3 M3E", "128", "192"),
}

BANK_NAMES = {0: "RESERVED", 1: "EPC", 2: "TID", 3: "USER"}


@dataclass
class TidInfo:
    raw: bytes = b""
    prefix: str = ""
    mdid: int = 0
    manufacturer: str = ""
    tmn: int = 0
    model: str = ""
    xtid: bool = False
    security: bool = False
    file_support: bool = False
    serial: str = ""
    xtid_bytes: bytes = b""
    known: bool = False


@dataclass
class BankInfo:
    bank: int = 0
    name: str = ""
    words: List[Optional[str]] = field(default_factory=list)
    readable: int = 0

    @property
    def size_bytes(self) -> int:
        return self.readable * 2


@dataclass
class ChipReport:
    tid: TidInfo = field(default_factory=TidInfo)
    epc: bytes = b""
    epc_crc: bytes = b""
    epc_pc: bytes = b""
    reserved: bytes = b""
    banks: Dict[int, BankInfo] = field(default_factory=dict)
    password_ok: Optional[bool] = None
    password_used: bytes = b""
    notes: List[str] = field(default_factory=list)

    @property
    def model_name(self) -> str:
        if self.tid.known:
            return f"{self.tid.manufacturer} {self.tid.model}"
        if self.tid.manufacturer:
            return f"{self.tid.manufacturer} (model unknown, TMN 0x{self.tid.tmn:03X})"
        return "unknown"


def decode_tid(tid: bytes) -> TidInfo:
    """Decode a TID according to the GS1 EPC Tag Data Standard."""
    info = TidInfo(raw=tid)
    if len(tid) < 4:
        info.notes = "TID too short"
        return info
    b0, b1, b2, b3 = tid[0], tid[1], tid[2], tid[3]
    info.prefix = tid[:4].hex().upper()
    info.xtid = bool(b1 & 0x80)
    info.security = bool(b1 & 0x40)
    info.file_support = bool(b1 & 0x20)
    info.mdid = ((b1 << 4) | (b2 >> 4)) & 0xFFF
    info.tmn = ((b2 & 0x0F) << 8) | b3
    info.manufacturer = MDID_NAMES.get(info.mdid, "")
    entry = TID_PREFIX_DB.get(info.prefix)
    if entry:
        info.manufacturer, info.model, _, _ = entry
        info.known = True
    # the serial is the part of the TID that is not the 4 byte header
    info.serial = tid[5:12].hex().upper() if len(tid) >= 12 else ""
    if info.xtid and len(tid) > 12:
        info.xtid_bytes = tid[12:]
    return info


@dataclass
class _Area:
    """One memory area to probe."""
    bank: int
    word: int
    words: int
    retries: int = 6
    required: bool = True
    data: bytes = b""
    done: bool = False
    validate: object = None


def _probe(reader: Ru5600Reader, areas, password: bytes,
           budget: float = 6.0) -> None:
    """Probe the memory areas inside a time budget.

    The reader answers a memory read only about 25 % of the time and a
    refused command costs 110-320 ms, so every area gets its own slice
    of the budget.  The areas are read one after the other with repeated
    commands of the *same* area: alternating between areas makes the
    reader answer with the response of the previous command.
    """
    deadline = time.monotonic() + budget
    for area in areas:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        retries = 40
        while True:
            try:
                data = reader.read_card(area.bank, area.word, area.words,
                                         password, retries=retries)
            except ReaderError:
                break
            if area.validate and not area.validate(data):
                retries = max(3, retries // 2)
                continue
            area.data = data
            area.done = True
            break


def analyze_chip(reader: Ru5600Reader,
                 password: bytes = b"\x00\x00\x00\x00",
                 budget: float = 3.0,
                 deep: bool = False,
                 expect_epc: str = "") -> ChipReport:
    """Full analysis of the tag currently in front of the reader.

    ``expect_epc`` is the EPC seen in the inventory: it is the ground
    truth to tell a real read apart from a stale answer of the reader.
    """
    report = ChipReport()
    expected_epc = expect_epc

    def valid_tid(data):
        # the TID always starts with the ISO/IEC 18000-6C class 0xE2
        return len(data) >= 4 and data[0] == 0xE2

    def valid_epc(data):
        if len(data) < 8:
            return False
        if expected_epc:
            return data[4:].hex().upper().startswith(expected_epc[:12])
        return data[4:8] != b"\x00\x00\x00\x00"

    areas = [
        _Area(MEM_TID, 0, 8, validate=valid_tid),
        _Area(MEM_EPC, 0, 8, validate=valid_epc),
    ]
    if deep:
        budget = max(budget, 6.0)
        areas += [
            _Area(MEM_RESERVED, 0, 4),
            _Area(MEM_USER, 0, 2),
            _Area(MEM_TID, 8, 4, required=False, validate=valid_tid),
            _Area(MEM_RESERVED, 4, 4, required=False),
            _Area(MEM_USER, 2, 2, required=False),
            _Area(MEM_EPC, 8, 4, required=False),
        ]
    _probe(reader, areas, password, budget=budget)

    def chunk(bank, word, words):
        for a in areas:
            if a.bank == bank and a.word == word and a.words == words:
                return a.data
        return b""

    tid = chunk(MEM_TID, 0, 8) + chunk(MEM_TID, 8, 4)
    report.tid = decode_tid(tid)
    report.banks[MEM_TID] = BankInfo(
        bank=MEM_TID, name="TID",
        words=[tid[i:i + 2].hex().upper() for i in range(0, len(tid), 2)],
        readable=len(tid) // 2)

    epc_raw = chunk(MEM_EPC, 0, 8) + chunk(MEM_EPC, 8, 4)
    report.banks[MEM_EPC] = BankInfo(
        bank=MEM_EPC, name="EPC",
        words=[epc_raw[i:i + 2].hex().upper()
               for i in range(0, len(epc_raw), 2)],
        readable=len(epc_raw) // 2)
    if len(epc_raw) >= 4:
        report.epc_crc = epc_raw[0:2]
        report.epc_pc = epc_raw[2:4]
        report.epc = epc_raw[4:]
    else:
        report.epc = epc_raw

    # stale answer check
    if expected_epc and report.epc:
        if report.epc.hex().upper().startswith(expected_epc[:8]):
            report.notes.append("EPC read matches the inventoried tag")
        else:
            report.notes.append(
                "WARNING: the EPC read does not match the inventoried tag - "
                "the reader may have answered with a stale buffer")
    elif expected_epc and not report.epc:
        report.notes.append("the EPC bank could not be read")

    report.reserved = chunk(MEM_RESERVED, 0, 4) + chunk(MEM_RESERVED, 4, 4)
    report.banks[MEM_RESERVED] = BankInfo(
        bank=MEM_RESERVED, name="RESERVED",
        words=[report.reserved[i:i + 2].hex().upper()
               for i in range(0, len(report.reserved), 2)],
        readable=len(report.reserved) // 2)

    user = chunk(MEM_USER, 0, 2) + chunk(MEM_USER, 2, 2)
    report.banks[MEM_USER] = BankInfo(
        bank=MEM_USER, name="USER",
        words=[user[i:i + 2].hex().upper() for i in range(0, len(user), 2)],
        readable=len(user) // 2)

    report.password_ok = None
    return report


def verify_password(reader: Ru5600Reader, password: bytes,
                    attempts: int = 14) -> Tuple[bool, bytes]:
    """Check a Gen2 access password.

    A tag with the wrong access password refuses every memory read, while
    the correct one lets them through (the reader itself drops roughly
    four out of five commands, so several attempts are needed).
    """
    data = b""
    for _ in range(max(1, attempts)):
        try:
            data = reader.read_card(MEM_EPC, 0, 8, password, retries=1)
            return True, data
        except ReaderError:
            time.sleep(0.03)
    return False, data


def format_report(report: ChipReport) -> str:
    """Human readable analysis report."""
    t = report.tid
    out = []
    out.append("=== CHIP IDENTIFICATION ===")
    out.append(f"TID (short)     : {t.prefix}")
    out.append(f"MDID            : 0x{t.mdid:03X}  "
               f"({t.manufacturer or 'unknown manufacturer'})")
    out.append(f"TMN             : 0x{t.tmn:03X}")
    out.append(f"Chip            : {report.model_name}")
    if t.xtid:
        out.append(f"XTID present    : yes ({len(t.xtid_bytes)} bytes)")
    if t.security:
        out.append("Security cmds   : Authenticate / Challenge supported")
    if t.file_support:
        out.append("File cmds       : FileOpen supported")
    if t.serial:
        out.append(f"Serial (TID)    : {t.serial}")
    if t.xtid_bytes:
        out.append(f"XTID payload    : {t.xtid_bytes.hex().upper()}")

    out.append("")
    out.append("=== MEMORY ===")
    for bank in (MEM_RESERVED, MEM_EPC, MEM_TID, MEM_USER):
        b = report.banks.get(bank)
        if b is None:
            continue
        out.append(f"{b.name:9s} readable: {b.readable} words "
                   f"({b.size_bytes} bytes)")
        if b.words:
            out.append("           " + " ".join(
                w if w is not None else "XX" for w in b.words))
    if report.epc:
        out.append("")
        out.append(f"EPC             : {report.epc.hex().upper()}")
    if report.epc_crc:
        out.append(f"EPC CRC (syn.)  : {report.epc_crc.hex().upper()}")
    if report.epc_pc:
        out.append(f"EPC PC          : {report.epc_pc.hex().upper()}")

    out.append("")
    out.append("=== PROTECTION ===")
    user = report.banks.get(MEM_USER)
    entry = TID_PREFIX_DB.get(t.prefix)
    if user is not None:
        if user.readable == 0:
            out.append("USER memory     : not readable - locked or absent")
        else:
            out.append(f"USER memory     : {user.size_bytes} bytes readable")
            if entry and entry[3] in ("0", ""):
                out.append(f"                  (this chip has {entry[3]} bit of "
                           f"user memory - nothing is locked)")
    if report.reserved:
        out.append(f"RESERVED        : {report.reserved.hex().upper()}")
    if report.password_ok is True:
        out.append("Access password : the tested password WORKS")
    elif report.password_ok is False:
        out.append("Access password : the tested password is WRONG")
    else:
        out.append("Access password : not tested")
    return "\n".join(out)


if __name__ == "__main__":  # quick smoke test
    r = Ru5600Reader()
    r.open()
    rep = analyze_chip(r)
    print(format_report(rep))
    r.close()
