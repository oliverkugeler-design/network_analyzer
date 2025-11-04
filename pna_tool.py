#!/usr/bin/env python3
"""
pna_tool.py — Minimal-yet-robust helper for Agilent/Keysight PNA (E8358A) via VISA-Socket (TCP 5025)

USAGE EXAMPLES
--------------
# IDN
python pna_tool.py 192.168.147.75 idn

# Configure S11 and fetch formatted data (dB)
python pna_tool.py 192.168.147.75 measure --param S11 --center 1.3e9 --span 1e8 --points 1001 --ifbw 1e3

# Configure S21 and dump CSV (freq, Re, Im, dB, phase)
python pna_tool.py 192.168.147.75 measure --param S21 --center 1.3e9 --span 1e8 --points 1601 --csv out.csv

# List files on the instrument
python pna_tool.py 192.168.147.75 list "C:\\UserData"

# Download a file from PNA to local
python pna_tool.py 192.168.147.75 get "C:\\UserData\\daresbury.cti" daresbury.cti

# Upload a local file to PNA
python pna_tool.py 192.168.147.75 put my_state.sta "C:\\UserData\\my_state.sta"

NOTES
-----
- Works with your proven path: VISA SOCKET, terminators '\n'.
- Enforces ASCII data (`FORM:DATA ASCII`) and waits for sweep completion via `*OPC?`.
- File I/O tries multiple legacy headers (A.06.x firmware): DATA/READ/TRAN variants.
"""

from __future__ import annotations
import argparse
import csv
import math
import os
from dataclasses import dataclass
from typing import Iterable, Tuple, Optional

import pyvisa


# -------------------------
# Session helpers
# -------------------------
@dataclass
class PNASession:
    rm: pyvisa.ResourceManager
    inst: pyvisa.resources.MessageBasedResource

def open_session(ip: str, backend: str = "", timeout_ms: int = 60000) -> PNASession:
    rm = pyvisa.ResourceManager(backend)
    res = f"TCPIP0::{ip}::5025::SOCKET"
    inst = rm.open_resource(res, timeout=timeout_ms)
    inst.write_termination = "\n"   # your unit responds with '\n'
    inst.read_termination  = "\n"
    inst.chunk_size        = 1024 * 1024
    return PNASession(rm=rm, inst=inst)

def close_session(sess: PNASession) -> None:
    try:
        sess.inst.close()
    except Exception:
        pass
    try:
        sess.rm.close()
    except Exception:
        pass

def scpi(sess: PNASession, cmd: str) -> None:
    sess.inst.write(cmd)

def query(sess: PNASession, cmd: str) -> str:
    return sess.inst.query(cmd).strip()

def idn(sess: PNASession) -> str:
    return query(sess, "*IDN?")


# -------------------------
# Measurement setup
# -------------------------
def ensure_measurement(sess: PNASession, param: str = "S21", meas_name: str = "Meas1", ch: int = 1) -> None:
    """Create/select measurement PARAM (S11/S21/…) named meas_name on channel ch and feed it to Trace1."""
    param = param.upper()
    scpi(sess, "*CLS")
    # Delete old to avoid CALC conflicts on older firmware
    scpi(sess, f"CALC{ch}:PAR:DEL:ALL")
    scpi(sess, f"CALC{ch}:PAR:DEF:EXT '{meas_name}','{param}'")
    scpi(sess, f"CALC{ch}:PAR:SEL '{meas_name}'")
    scpi(sess, "DISP:WIND1:STATE ON")
    scpi(sess, f"DISP:WIND1:TRAC1:FEED '{meas_name}'")
    # Display format as needed; we'll fetch formatted (FDATA) but keep ASCII globally
    scpi(sess, f"CALC{ch}:FORM MLOG")
    # ASCII data format (very important on A.06.x)
    scpi(sess, "FORM:DATA ASCII")
    # Make sure output on
    scpi(sess, "OUTP ON")

def configure_sweep(sess: PNASession,
                    center_hz: float,
                    span_hz: float,
                    points: int = 1001,
                    ifbw_hz: Optional[float] = None,
                    ch: int = 1) -> None:
    scpi(sess, f"SENS{ch}:SWE:TYPE LIN")
    scpi(sess, f"SENS{ch}:FREQ:CENT {center_hz}")
    scpi(sess, f"SENS{ch}:FREQ:SPAN {span_hz}")
    scpi(sess, f"SENS{ch}:SWE:POIN {points}")
    if ifbw_hz is not None:
        scpi(sess, f"SENS{ch}:BWID {ifbw_hz}")
    scpi(sess, f"INIT{ch}:CONT 0")     # single
    scpi(sess, "TRIG:SEQ:SOUR IMM")    # immediate trigger

def run_single_sweep(sess: PNASession, ch: int = 1, timeout_s: float = 90.0) -> bool:
    """Start a single sweep and block until done via repeated *OPC? to tolerate A.06 latency."""
    scpi(sess, "ABOR")
    scpi(sess, f"INIT{ch}:IMM")
    # Poll *OPC? because some old FW won’t resolve inline INIT;OPC? reliably
    sess.inst.timeout = int(timeout_s * 1000)
    done = query(sess, "*OPC?")
    return done == "1"

def get_frequency_axis(sess: PNASession, ch: int = 1) -> Iterable[float]:
    freqs_csv = query(sess, f"SENS{ch}:FREQ:DATA?")
    return [float(v) for v in freqs_csv.split(",") if v]

def fetch_fdata(sess: PNASession, ch: int = 1) -> Iterable[float]:
    """Formatted data in current CALC form (e.g., MLOG in dB). ASCII enforced."""
    data_csv = query(sess, f"CALC{ch}:DATA? FDATA")
    return [float(v) for v in data_csv.split(",") if v]

def fetch_sdata_complex(sess: PNASession, ch: int = 1) -> Tuple[list, list]:
    """Complex raw SDATA (Re, Im alternating) -> returns lists re[], im[]"""
    s_csv = query(sess, f"CALC{ch}:DATA? SDATA")
    vals = [float(v) for v in s_csv.split(",") if v]
    reals, imags = [], []
    it = iter(vals)
    for r, im in zip(it, it):
        reals.append(r)
        imags.append(im)
    return reals, imags

def fetch_trace(sess: PNASession, ch: int = 1) -> Tuple[list, list, list, str]:
    """
    Returns (freq_Hz, y1, y2, mode):
      - If FDATA worked: (freq, values_dB, None, "FDATA")
      - Else fall back to SDATA: (freq, Re, Im, "SDATA")
    """
    freq = get_frequency_axis(sess, ch)
    try:
        y = fetch_fdata(sess, ch)
        # Trim to same length just in case
        n = min(len(freq), len(y))
        return freq[:n], y[:n], None, "FDATA"
    except Exception:
        re, im = fetch_sdata_complex(sess, ch)
        n = min(len(freq), len(re), len(im))
        return freq[:n], re[:n], im[:n], "SDATA"


# -------------------------
# File I/O (legacy-friendly)
# -------------------------
def list_dir(sess: PNASession, remote_dir: str) -> str:
    return query(sess, f'MMEM:CAT? "{remote_dir}"')

def _read_definite_block(sess: PNASession) -> bytes:
    """Read IEEE 488.2 definite block: #<n><len><data> ."""
    # Read first two bytes
    head = sess.inst.read_bytes(2, break_on_termchar=False)
    if not head or head[0:1] != b"#":
        raise RuntimeError(f"Expected '#', got {head!r}")
    ndigits = head[1] - 48
    if ndigits <= 0:
        raise RuntimeError("Indefinite block not supported")
    len_bytes = sess.inst.read_bytes(ndigits, break_on_termchar=False)
    total = int(len_bytes.decode("ascii"))
    data = sess.inst.read_bytes(total, break_on_termchar=False)
    return data

def get_file(sess: PNASession, remote_path: str, local_path: str) -> None:
    scpi(sess, "*CLS")
    # try several headers, old firmware varies
    candidates = [
        f'MMEM:DATA? "{remote_path}"',
        f'MMEM:READ? "{remote_path}"',
        f'MMEM:TRAN? "{remote_path}"',
        f'MMEM:TRAN:FILE? "{remote_path}"',
    ]
    last_err = None
    for cmd in candidates:
        try:
            scpi(sess, cmd)
            data = _read_definite_block(sess)
            with open(local_path, "wb") as f:
                f.write(data)
            return
        except Exception as e:
            last_err = e
            # clear error queue
            try:
                for _ in range(4):
                    err = query(sess, "SYST:ERR?")
                    if err.startswith("+0") or "No error" in err:
                        break
            except Exception:
                pass
    raise RuntimeError(f"GET failed ({remote_path}). Last error: {last_err}")

def _build_block(payload: bytes) -> bytes:
    L = str(len(payload)).encode("ascii")
    return b"#" + str(len(L)).encode("ascii") + L + payload

def put_file(sess: PNASession, local_path: str, remote_path: str) -> None:
    scpi(sess, "*CLS")
    data = open(local_path, "rb").read()
    block = _build_block(data)
    candidates = [
        ('MMEM:DATA "{dst}",', True),
        ('MMEM:WRIT "{dst}",', True),
        ('MMEM:TRAN:FILE "{dst}",', True),
    ]
    last_err = None
    for fmt, use_raw in candidates:
        try:
            prefix = fmt.format(dst=remote_path).encode("ascii")
            if use_raw:
                sess.inst.write_raw(prefix + block)
            else:
                sess.inst.write(prefix + block)  # unlikely path
            return
        except Exception as e:
            last_err = e
            try:
                for _ in range(4):
                    err = query(sess, "SYST:ERR?")
                    if err.startswith("+0") or "No error" in err:
                        break
            except Exception:
                pass
    raise RuntimeError(f"PUT failed ({remote_path}). Last error: {last_err}")


# -------------------------
# CLI
# -------------------------
def cmd_idn(sess: PNASession, _args: argparse.Namespace) -> None:
    print(idn(sess))

def cmd_measure(sess: PNASession, a: argparse.Namespace) -> None:
    print("*IDN? ->", idn(sess))
    ensure_measurement(sess, param=a.param, meas_name=a.meas, ch=a.channel)
    configure_sweep(sess, center_hz=a.center, span_hz=a.span, points=a.points, ifbw_hz=a.ifbw, ch=a.channel)
    ok = run_single_sweep(sess, ch=a.channel, timeout_s=a.timeout)
    if not ok:
        print("WARN: sweep did not signal completion; continuing.")
    freq, y1, y2, mode = fetch_trace(sess, ch=a.channel)
    print(f"Fetched {len(freq)} points via {mode}.")
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.writer(f)
            if mode == "FDATA":
                w.writerow(["freq_Hz", "value_dB"])
                for fi, vi in zip(freq, y1):
                    w.writerow([fi, vi])
            else:
                w.writerow(["freq_Hz", "Re", "Im", "Mag_dB", "Phase_deg"])
                for fi, re, im in zip(freq, y1, y2):
                    mag = math.hypot(re, im)
                    mag_db = 20.0 * math.log10(max(mag, 1e-300))
                    phase = math.degrees(math.atan2(im, re))
                    w.writerow([fi, re, im, mag_db, phase])
        print(f"Wrote CSV: {a.csv}")
    else:
        # print a small preview
        n = min(5, len(freq))
        if mode == "FDATA":
            for i in range(n):
                print(f"{i:4d}: f={freq[i]:.3f} Hz, value(dB)={y1[i]:.3f}")
        else:
            for i in range(n):
                re, im = y1[i], y2[i]
                mag = math.hypot(re, im)
                mag_db = 20.0 * math.log10(max(mag, 1e-300))
                phase = math.degrees(math.atan2(im, re))
                print(f"{i:4d}: f={freq[i]:.3f} Hz, Re={re:.3e}, Im={im:.3e}, |S|={mag_db:.3f} dB, ∠={phase:.2f}°")

def cmd_list(sess: PNASession, a: argparse.Namespace) -> None:
    print(list_dir(sess, a.remote))

def cmd_get(sess: PNASession, a: argparse.Namespace) -> None:
    get_file(sess, a.remote, a.local)
    print(f"Downloaded → {a.local}")

def cmd_put(sess: PNASession, a: argparse.Namespace) -> None:
    put_file(sess, a.local, a.remote)
    print("Uploaded.")

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="E8358A PNA helper via VISA Socket 5025")
    p.add_argument("ip", help="Instrument IP (e.g., 192.168.147.75)")
    p.add_argument("--backend", default="", help='VISA backend ("" default, "@py", or visa DLL path)')
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("idn", help="Query *IDN?")
    s.set_defaults(func=cmd_idn)

    m = sub.add_parser("measure", help="Configure S-parameter, sweep once, fetch data")
    m.add_argument("--param", default="S21", help="S-parameter (S11, S21, ...)")
    m.add_argument("--meas", default="Meas1", help="Measurement name")
    m.add_argument("--channel", type=int, default=1)
    m.add_argument("--center", type=float, required=True)
    m.add_argument("--span", type=float, required=True)
    m.add_argument("--points", type=int, default=1001)
    m.add_argument("--ifbw", type=float, default=None, help="IF bandwidth in Hz (optional)")
    m.add_argument("--timeout", type=float, default=90.0, help="sweep wait timeout (s)")
    m.add_argument("--csv", default=None, help="write CSV file with data")
    m.set_defaults(func=cmd_measure)

    l = sub.add_parser("list", help="List directory on instrument")
    l.add_argument("remote", help=r'Instrument path, e.g. "C:\UserData"')
    l.set_defaults(func=cmd_list)

    g = sub.add_parser("get", help="Download file from instrument")
    g.add_argument("remote", help=r'Instrument file path, e.g. "C:\UserData\file.cti"')
    g.add_argument("local", help="Local destination path")
    g.set_defaults(func=cmd_get)

    u = sub.add_parser("put", help="Upload file to instrument")
    u.add_argument("local", help="Local source path")
    u.add_argument("remote", help=r'Instrument file path, e.g. "C:\UserData\file.sta"')
    u.set_defaults(func=cmd_put)

    return p

def main():
    ap = build_parser()
    args = ap.parse_args()
    sess = open_session(args.ip, backend=args.backend, timeout_ms=60000)
    try:
        args.func(sess, args)
    finally:
        close_session(sess)

if __name__ == "__main__":
    main()
