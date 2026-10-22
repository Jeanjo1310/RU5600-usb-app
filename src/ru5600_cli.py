#!/usr/bin/env python3
"""RU5600 Reader - headless command line client.

Examples:
    ru5600_cli.py info
    ru5600_cli.py inventory
    ru5600_cli.py watch
    ru5600_cli.py read --mem EPC --ptr 0 --count 2
    ru5600_cli.py write --epc 3034333234353536373839303132
    ru5600_cli.py params
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ru5600_core import (  # noqa: E402
    MEM_EPC,
    MEM_TID,
    MEM_USER,
    ReaderError,
    Ru5600Reader,
)


def _fmt_tags(tags):
    out = []
    for t in tags:
        ts = ""
        if t.timestamp:
            ts = "  ts=" + t.timestamp.hex().upper()
        out.append(
            f"ant={t.ant} rssi={t.rssi:3d} type=0x{t.type:02X} "
            f"epc={t.epc_hex}{ts}")
    return out


def main():
    ap = argparse.ArgumentParser(description="RU5600 UHF reader CLI")
    ap.add_argument("command",
                    choices=["info", "inventory", "watch", "read", "write",
                             "buzzer",
                             "params", "freq", "chip", "password", "heat"],
                    help="operation to run")
    ap.add_argument("--mem", default="EPC", choices=["EPC", "TID", "USER"])
    ap.add_argument("--ptr", default="0", help="start word address (hex)")
    ap.add_argument("--count", default="8", help="word count (hex), 8 = 16 bytes")
    ap.add_argument("--epc", default="", help="EPC bytes to write (hex)")
    ap.add_argument("--value", default="", help="value for the command")
    ap.add_argument("--band", default="USA", help="frequency band")
    ap.add_argument("--password", default="00000000",
                    help="Gen2 access password (8 hex digits)")
    ap.add_argument("--deep", action="store_true",
                    help="also map the reserved and user memory")
    ap.add_argument("--mode", default="balanced",
                    choices=["burst", "balanced", "reliable"],
                    help="read effort for a single detection")
    ap.add_argument("--seconds", type=float, default=30.0)
    args = ap.parse_args()

    reader = Ru5600Reader()
    try:
        reader.open()
    except ReaderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        if args.command == "info":
            info = reader.system_info()
            print(f"Device SN   : {info.device_sn}")
            print(f"Firmware     : {info.soft_ver}")
            print(f"Hardware     : {info.hard_ver}")

        elif args.command == "heat":
            st = reader.measure(args.seconds)
            print(f"elapsed        : {st['elapsed']:.1f} s")
            print(f"detections     : {st['detections']} "
                  f"({st['detections_per_s']:.2f}/s)")
            print(f"RSSI           : avg {st['rssi_avg']:.1f} "
                  f"min {st['rssi_min']} max {st['rssi_max']} "
                  f"({st['rssi_count']} samples)")
            for epc, n in st["tags"].items():
                print(f"  {epc}: {n} detections")

        elif args.command == "inventory":
            det = reader.detect(args.mode)
            tags = det.tags
            print(f"{len(tags)} tag(s) in {det.elapsed:.2f}s "
                  f"(mode {args.mode}, {det.hits}/{det.rounds} hits)")
            for line in _fmt_tags(tags):
                print(line)

        elif args.command == "watch":
            reader.set_work_mode(1)
            reader.start_read()
            seen = {}
            order = []
            end = time.time() + args.seconds
            try:
                while time.time() < end:
                    for tag in reader.poll_tags():
                        key = tag.epc_hex
                        if key not in seen:
                            # the sequential number is given the first
                            # time the tag is seen and does not change
                            order.append(key)
                        seen[key] = seen.get(key, 0) + 1
                        print(f"[{time.strftime('%H:%M:%S')}] "
                              f"#{order.index(key) + 1:<4} "
                              f"ant={tag.ant} rssi={tag.rssi:3d} "
                              f"epc={key}  (n={seen[key]})")
                    time.sleep(0.2)
            finally:
                reader.stop_read()
            print(f"unique tags: {len(seen)}")

        elif args.command == "read":
            mem = {"EPC": MEM_EPC, "TID": MEM_TID, "USER": MEM_USER}[args.mem]
            pwd = bytes.fromhex(args.password)
            data = reader.read_card(mem, int(args.ptr, 16),
                                  int(args.count, 16), pwd)
            print(data.hex().upper())

        elif args.command == "write":
            if not args.epc:
                print("error: --epc required", file=sys.stderr)
                return 2
            data = bytes.fromhex(args.epc.replace(" ", ""))
            reader.write_epc(data, bytes.fromhex(args.password))
            print(f"written: {data.hex().upper()}")

        elif args.command == "buzzer":
            # the reader beeps on every tag it reads
            on = (args.value or "off").lower() in ("on", "1", "true", "yes")
            reader.set_beep(on)
            print(f"buzzer {'on' if on else 'off'}")

        elif args.command == "params":
            print(f"RF power   : {reader.rf_power()} dBm")
            print(f"Work mode  : {reader.work_mode()} "
                  f"({'Active' if reader.work_mode() else 'Answer'})")
            print(f"Buzzer     : {reader.beep()}")
            print(f"Filter time: {reader.filter_time()} s")
            print(f"Raw freq   : {reader.read_freq().hex().upper()}")

        elif args.command == "chip":
            from ru5600_chip import analyze_chip, format_report
            print(format_report(analyze_chip(
                reader, bytes.fromhex(args.password), deep=args.deep)))

        elif args.command == "password":
            from ru5600_chip import verify_password
            pwd = bytes.fromhex(args.password)
            good, data = verify_password(reader, pwd)
            if good:
                print(f"password {pwd.hex().upper()} WORKS")
                print(f"EPC bank: {data.hex().upper()}")
            else:
                print(f"password {pwd.hex().upper()} is WRONG "
                      f"(every read was refused)")

        elif args.command == "freq":
            reader.set_freq(args.band)
            print(f"band {args.band} -> raw {reader.read_freq().hex().upper()}")

    except ReaderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
