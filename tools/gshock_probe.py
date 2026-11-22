#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["bleak>=0.22"]
# ///
"""
G-Shock probe: connect to a Casio watch over Bluetooth (Linux/BlueZ), pair if needed, and record everything it
answers. Read-only: it only sends Casio *request* codes (one byte each), never settings or time.

Usage:
    uv run gshock_probe.py                 # scan for a Casio watch, then probe
    uv run gshock_probe.py --address AA:BB:CC:DD:EE:FF
    uv run gshock_probe.py --sweep         # also try every request code 0x01..0x4F (slower, still read-only)
    uv run gshock_probe.py --note "app says 80%"   # free text saved with the result (e.g. battery % shown by CASIO WATCHES)

Output: gshock_probe_<time>.json next to this script. Send that file back.
"""
import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from bleak import BleakClient, BleakScanner

# Casio vendor characteristics (from izivkov/gshock_api casio_constants.py and his DW-H5600 notes)
REQ = "26eb002c-b012-49a8-b1f8-394fb2032b0f"      # write a one-byte request code here
ALLF = "26eb002d-b012-49a8-b1f8-394fb2032b0f"     # answers to requests arrive here (notify)
DRSP = "26eb0023-b012-49a8-b1f8-394fb2032b0f"     # data request / announce (notify)
CONVOY = "26eb0024-b012-49a8-b1f8-394fb2032b0f"   # bulk data (notify)
SUBSCRIBE = [ALLF, DRSP, CONVOY]                  # NOT 26eb0030: subscribing to it made a DW-H5600 disconnect

# Known request codes (name -> code). All are reads.
KNOWN = {
    "watch_condition (battery, temperature)": 0x28,
    "watch_name": 0x23,
    "app_information": 0x22,
    "ble_features": 0x10,
    "setting_for_ble": 0x11,
    "version_information": 0x20,
    "advertise_parameters": 0x3B,
    "connection_parameters": 0x3A,
    "activity_record": 0x26,
    "dst_watch_state": 0x1D,
    "dst_setting": 0x1E,
    "setting_for_basic": 0x13,
    "alarms_1": 0x15,
    "alarms_2": 0x16,
    "timer": 0x18,
    "home_time": 0x24,
    "setting_for_exercise": 0x2D,
    "target_value": 0x43,
    "user_profile": 0x45,
    "service_discovery": 0x47,
}


def hx(b: bytes) -> str:
    return b.hex(" ")


def ascii_of(b: bytes) -> str:
    return "".join(chr(c) if 32 <= c < 127 else "." for c in b)


async def find_watch(address: str | None, seconds: float):
    if address:
        dev = await BleakScanner.find_device_by_address(address, timeout=seconds)
        return dev, None
    print(f"Scanning {seconds:.0f} s for a Casio watch... (on the watch: Control Mode > [CONNECT], or the pairing menu)")
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    for dev, adv in found.values():
        name = (adv.local_name or dev.name or "")
        if any(k in name.upper() for k in ("CASIO", "GBX", "H5600", "G-SHOCK")):
            return dev, adv
    print("No Casio watch seen. Devices found:", ", ".join(sorted({(a.local_name or d.name or d.address) for d, a in found.values()})))
    return None, None


async def probe(args) -> dict:
    out: dict = {"started": datetime.now().isoformat(), "note": args.note, "events": []}
    t0 = time.monotonic()

    def log(kind, **kw):
        kw.update(kind=kind, t=round(time.monotonic() - t0, 3))
        out["events"].append(kw)

    dev, adv = await find_watch(args.address, args.scan)
    if not dev:
        out["error"] = "no watch found"
        return out
    out["device"] = {"address": dev.address, "name": dev.name}
    if adv:
        out["advertisement"] = {
            "local_name": adv.local_name, "rssi": adv.rssi,
            "manufacturer_data": {str(k): hx(v) for k, v in adv.manufacturer_data.items()},
            "service_uuids": adv.service_uuids,
            "service_data": {k: hx(v) for k, v in adv.service_data.items()},
        }
    print(f"Connecting to {dev.name} {dev.address} ...")

    disconnected = asyncio.Event()
    client = BleakClient(dev, timeout=30, disconnected_callback=lambda c: (log("disconnected"), disconnected.set()))
    await client.connect()
    log("connected")
    try:
        await client.pair()
        log("paired")
    except Exception as e:  # already bonded, or pairing not needed
        log("pair_skipped", error=repr(e))

    # GATT table, and every readable characteristic
    gatt = []
    for svc in client.services:
        s = {"uuid": svc.uuid, "description": svc.description, "characteristics": []}
        for ch in svc.characteristics:
            c = {"uuid": ch.uuid, "handle": ch.handle, "properties": ch.properties, "description": ch.description}
            if "read" in ch.properties and not args.no_read:
                try:
                    v = bytes(await client.read_gatt_char(ch))
                    c["value_hex"], c["value_ascii"] = hx(v), ascii_of(v)
                except Exception as e:
                    c["read_error"] = repr(e)
            s["characteristics"].append(c)
        gatt.append(s)
    out["gatt"] = gatt
    print(f"GATT: {sum(len(s['characteristics']) for s in gatt)} characteristics recorded")

    answers: list = []

    def on_notify(uuid):
        def cb(_sender, data: bytearray):
            b = bytes(data)
            answers.append((uuid, b))
            log("notify", uuid=uuid, hex=hx(b), ascii=ascii_of(b))
        return cb

    present = {ch.uuid for s in client.services for ch in s.characteristics}
    for u in SUBSCRIBE:
        if u in present:
            try:
                await client.start_notify(u, on_notify(u))
                log("subscribed", uuid=u)
            except Exception as e:
                log("subscribe_failed", uuid=u, error=repr(e))
    if REQ not in present:
        out["error"] = "request characteristic 26eb002c not found: not a Casio watch, or a different protocol"
        return out
    req_props = next(ch.properties for s in client.services for ch in s.characteristics if ch.uuid == REQ)
    with_response = "write" in req_props

    async def ask(name, code):
        if disconnected.is_set():
            return
        before = len(answers)
        try:
            await client.write_gatt_char(REQ, bytes([code]), response=with_response)
            log("request", name=name, code=f"0x{code:02x}")
        except Exception as e:
            log("request_failed", name=name, code=f"0x{code:02x}", error=repr(e))
            return
        await asyncio.sleep(args.wait)
        got = [hx(b) for _, b in answers[before:]]
        out.setdefault("answers", {})[f"0x{code:02x} {name}"] = got
        print(f"  0x{code:02x} {name:42s} -> {got[0] if got else '(no answer)'}")

    print("Requests (the watch may drop after ~45 s; the important ones go first):")
    for i in range(3):  # battery/temperature three times, to see if it is stable
        await ask(f"watch_condition #{i + 1}", 0x28)
    for name, code in KNOWN.items():
        if code != 0x28:
            await ask(name, code)
    if args.sweep:
        for code in range(0x01, 0x50):
            if code not in KNOWN.values():
                await ask("sweep", code)
    await asyncio.sleep(2)  # anything the watch sends on its own
    if client.is_connected:
        await client.disconnect()
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address")
    ap.add_argument("--scan", type=float, default=20)
    ap.add_argument("--wait", type=float, default=1.2, help="seconds to wait for each answer")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--no-read", action="store_true", help="skip reading GATT characteristics")
    ap.add_argument("--note", default="")
    args = ap.parse_args()
    try:
        result = asyncio.run(probe(args))
    except Exception as e:
        result = {"error": repr(e)}
        print("Error:", repr(e), file=sys.stderr)
    path = Path(__file__).with_name(f"gshock_probe_{datetime.now():%Y%m%d_%H%M%S}.json")
    path.write_text(json.dumps(result, indent=1))
    print(f"\nSaved {path}  <- send this file back")


if __name__ == "__main__":
    main()
