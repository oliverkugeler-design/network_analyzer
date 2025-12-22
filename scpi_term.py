#!/usr/bin/env python3
# scpi_term.py — kleines interaktives SCPI-Terminal für TCP 5025 (Keysight/Agilent PNA)
# Nutzung:
#   python scpi_term.py 192.168.147.75
#   python scpi_term.py 192.168.147.75 --raw-socket
#
# Befehle in der REPL:
#   ?<SCPI>       -> query, z.B. "?*IDN?"
#   !<SCPI>       -> write ohne Antwort, z.B. "!SYST:LOC"
#   <SCPI>?       -> auto-query, wenn die Eingabe auf '?' endet
#   <SCPI>        -> write (ohne '?') wird gesendet
#   :opc          -> führt "*OPC?" aus
#   :err          -> liest SYST:ERR? (mehrfach)
#   :cls          -> *CLS
#   :help         -> Hilfe anzeigen
#   :quit / :exit -> Beenden

import argparse
import socket
import sys
import time

TERMINATOR = "\n"
BUFFER = 1024 * 1024

# ---------------- Raw-Socket Backend ----------------
class RawSocketSess:
    def __init__(self, ip: str, port: int = 5025, timeout_s: float = 10.0):
        self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s.settimeout(timeout_s)
        self.s.connect((ip, port))

    def write(self, cmd: str) -> None:
        data = (cmd + TERMINATOR).encode("ascii", errors="ignore")
        self.s.sendall(data)

    def query(self, cmd: str) -> str:
        self.write(cmd)
        return self.read()

    def read(self) -> str:
        chunks = []
        self.s.settimeout(self.s.gettimeout())
        while True:
            try:
                b = self.s.recv(BUFFER)
                if not b:
                    break
                chunks.append(b)
                # einfache Termination via \n
                if b.endswith(b"\n"):
                    break
            except socket.timeout:
                break
        data = b"".join(chunks)
        try:
            return data.decode("utf-8", errors="ignore").strip()
        except Exception:
            return data.decode("ascii", errors="ignore").strip()

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass

# ---------------- PyVISA-Socket Backend ----------------
class VisaSess:
    def __init__(self, ip: str, backend: str = "", timeout_s: float = 10.0):
        import pyvisa
        rm = pyvisa.ResourceManager(backend)
        res = f"TCPIP0::{ip}::5025::SOCKET"
        inst = rm.open_resource(res, timeout=int(timeout_s * 1000))
        inst.write_termination = TERMINATOR
        inst.read_termination = TERMINATOR
        inst.chunk_size = BUFFER
        self.rm = rm
        self.inst = inst

    def write(self, cmd: str) -> None:
        self.inst.write(cmd)

    def query(self, cmd: str) -> str:
        return self.inst.query(cmd).strip()

    def read(self) -> str:
        return self.inst.read().strip()

    def close(self):
        try:
            self.inst.close()
        except Exception:
            pass
        try:
            self.rm.close()
        except Exception:
            pass

# ---------------- REPL ----------------
HELP = """\
Shortcuts:
  ?<SCPI>        query (z.B. ?*IDN?)
  !<SCPI>        write (z.B. !SYST:LOC)
  <SCPI>?        auto-query (wenn auf '?' endet)
  <SCPI>         write (normal)
  :opc           *OPC?
  :err           SYST:ERR? (liest bis +0,"No error")
  :cls           *CLS
  :help          diese Hilfe
  :quit / :exit  beenden
"""

def drain_errors(sess):
    # Lies Fehler bis “No error”
    for _ in range(12):
        try:
            e = sess.query("SYST:ERR?")
        except Exception as ex:
            print(f"[ERR] SYST:ERR? -> {ex}")
            break
        print(f"[ERRQ] {e}")
        if e.startswith("+0") or "No error" in e:
            break

def repl(sess):
    # Banner
    try:
        idn = sess.query("*IDN?")
    except Exception as e:
        idn = f"[IDN failed: {e}]"
    print(f"[Connected] {idn}")
    print(HELP)

    while True:
        try:
            line = input("SCPI> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue

        if line.lower() in (":quit", ":exit"):
            break
        if line.lower() == ":help":
            print(HELP)
            continue
        if line.lower() == ":cls":
            try:
                sess.write("*CLS")
                print("[OK] *CLS")
            except Exception as e:
                print(f"[ERR] {e}")
            continue
        if line.lower() == ":opc":
            try:
                r = sess.query("*OPC?")
                print(r)
            except Exception as e:
                print(f"[ERR] {e}")
            continue
        if line.lower() == ":err":
            drain_errors(sess)
            continue

        # Prefix-Shortcuts
        if line.startswith("?"):
            cmd = line[1:].strip()
            try:
                out = sess.query(cmd)
                print(out)
            except Exception as e:
                print(f"[ERR] {e}")
            continue
        if line.startswith("!"):
            cmd = line[1:].strip()
            try:
                sess.write(cmd)
                print("[OK]")
            except Exception as e:
                print(f"[ERR] {e}")
            continue

        # Auto: Query wenn auf '?' endet, sonst write
        try:
            if line.endswith("?"):
                out = sess.query(line)
                print(out)
            else:
                sess.write(line)
                print("[OK]")
        except Exception as e:
            print(f"[ERR] {e}")

def main():
    ap = argparse.ArgumentParser(description="Einfaches SCPI-Terminal für TCP 5025 (PNA etc.)")
    ap.add_argument("ip", help="Instrument-IP, z.B. 192.168.147.75")
    ap.add_argument("--timeout", type=float, default=10.0, help="Timeout in Sekunden (Default 10)")
    ap.add_argument("--backend", default="", help='PyVISA Backend ("" oder "@py" oder Pfad zur DLL)')
    ap.add_argument("--raw-socket", action="store_true", help="Ohne PyVISA, direkt per socket verbinden")
    args = ap.parse_args()

    sess = None
    try:
        if args.raw_socket:
            sess = RawSocketSess(args.ip, 5025, args.timeout)
        else:
            sess = VisaSess(args.ip, backend=args.backend, timeout_s=args.timeout)
        repl(sess)
    finally:
        if sess:
            sess.close()

if __name__ == "__main__":
    main()
