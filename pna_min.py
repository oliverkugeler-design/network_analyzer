#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import sys
import traceback
from dataclasses import dataclass
from typing import Optional, List, Tuple

import pyvisa
from PyQt6 import QtCore, QtWidgets


DEBUG = ("--debug" in sys.argv)

# Existing measurement names on your NA (from your PAR:CAT?)
MEAS_S21 = "Meas_S21"
MEAS_S11 = "Meas_S11"
MEAS_S11_FALLBACK = "CH1_S11_1"  # optional fallback

CH = 1  # channel number


@dataclass
class Sess:
    rm: pyvisa.ResourceManager
    inst: pyvisa.resources.MessageBasedResource


def log_print(msg: str):
    if DEBUG:
        print(msg)


def open_session(ip: str, timeout_ms: int = 8000) -> Sess:
    res = f"TCPIP0::{ip}::5025::SOCKET"
    last = None
    for backend in ["", "@py"]:
        try:
            rm = pyvisa.ResourceManager(backend)
            inst = rm.open_resource(res, timeout=timeout_ms)
            inst.write_termination = "\n"
            inst.read_termination = "\n"
            inst.chunk_size = 1024 * 1024
            return Sess(rm=rm, inst=inst)
        except Exception as e:
            last = e
    raise RuntimeError(f"Could not open {res}. Last error: {last}\nHint: pip install pyvisa-py (or install NI/Keysight VISA).")


def close_session(s: Optional[Sess]) -> None:
    if not s:
        return
    try:
        s.inst.close()
    except Exception:
        pass
    try:
        s.rm.close()
    except Exception:
        pass


def write(s: Sess, cmd: str) -> None:
    log_print(f">> {cmd}")
    s.inst.write(cmd)


def query(s: Sess, cmd: str, timeout_ms: Optional[int] = None) -> str:
    old = s.inst.timeout
    if timeout_ms is not None:
        s.inst.timeout = timeout_ms
    try:
        log_print(f"?> {cmd}")
        return s.inst.query(cmd).strip()
    finally:
        s.inst.timeout = old


def safe_query(s: Sess, cmd: str, timeout_ms: int = 800) -> Optional[str]:
    try:
        return query(s, cmd, timeout_ms=timeout_ms)
    except Exception:
        return None


def select_param_quiet(s: Sess, param: str) -> str:
    """Select existing measurement only. No DEF/DEL/DISP."""
    p = param.upper().strip()
    if p == "S21":
        name = MEAS_S21
    elif p == "S11":
        name = MEAS_S11
    else:
        raise ValueError("param must be S11 or S21")

    # Select
    write(s, f'CALC{CH}:PAR:SEL "{name}"')

    # Optional sanity check (no crash if unsupported)
    sel = safe_query(s, f"CALC{CH}:PAR:SEL?", timeout_ms=600)
    if sel is not None and name not in sel:
        # Try S11 fallback if needed
        if p == "S11":
            write(s, f'CALC{CH}:PAR:SEL "{MEAS_S11_FALLBACK}"')
            name = MEAS_S11_FALLBACK
    return name


def configure_sweep(s: Sess, center_hz: float, span_hz: float, points: int, ifbw_hz: float) -> None:
    # Keep it minimal; no display commands.
    write(s, f"SENS{CH}:SWE:TYPE LIN")
    write(s, f"SENS{CH}:FREQ:CENT {center_hz:.0f}")
    write(s, f"SENS{CH}:FREQ:SPAN {span_hz:.0f}")
    write(s, f"SENS{CH}:SWE:POIN {int(points)}")
    write(s, f"SENS{CH}:BWID {float(ifbw_hz)}")
    write(s, f"INIT{CH}:CONT 0")
    write(s, "TRIG:SEQ:SOUR IMM")
    # Data format
    write(s, "FORM:DATA ASCII")


def run_sweep(s: Sess, timeout_s: float = 120.0) -> bool:
    write(s, "ABOR")
    write(s, f"INIT{CH}:IMM")
    # *OPC? is the cleanest “wait done”
    s.inst.timeout = int(timeout_s * 1000)
    return query(s, "*OPC?") == "1"


def fetch_data(s: Sess) -> Tuple[List[float], List[float], List[float]]:
    f_csv = query(s, f"SENS{CH}:FREQ:DATA?")
    freqs = [float(v) for v in f_csv.split(",") if v]

    s_csv = query(s, f"CALC{CH}:DATA? SDATA")
    vals = [float(v) for v in s_csv.split(",") if v]
    reals, imags = [], []
    it = iter(vals)
    for r, im in zip(it, it):
        reals.append(r)
        imags.append(im)

    n = min(len(freqs), len(reals), len(imags))
    return freqs[:n], reals[:n], imags[:n]


def save_cti(path: str, param: str, freq_hz: List[float], re: List[float], im: List[float]) -> None:
    n = min(len(freq_hz), len(re), len(im))
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# CTI v1; param={param}; points={n}\n")
        f.write("f_Hz:" + ",".join(f"{x:.6f}" for x in freq_hz[:n]) + "\n")
        f.write("re:" + ",".join(f"{x:.16e}" for x in re[:n]) + "\n")
        f.write("im:" + ",".join(f"{x:.16e}" for x in im[:n]) + "\n")


class Worker(QtCore.QObject):
    log = QtCore.pyqtSignal(str)
    idn = QtCore.pyqtSignal(str)
    data = QtCore.pyqtSignal(list, list, list)

    def __init__(self):
        super().__init__()
        self.s: Optional[Sess] = None
        self.last_param: str = "S21"

    @QtCore.pyqtSlot(str)
    def connect_ip(self, ip: str):
        try:
            self.close()
            self.s = open_session(ip)
            who = query(self.s, "*IDN?")
            self.idn.emit(who)
            self.log.emit("Connected.")
        except Exception as e:
            self.log.emit(f"Connect failed: {e}")
            self.close()

    @QtCore.pyqtSlot()
    def close(self):
        if self.s:
            close_session(self.s)
            self.s = None

    @QtCore.pyqtSlot(str, float, float, int, float)
    def apply(self, param: str, center: float, span: float, points: int, ifbw: float):
        try:
            if not self.s:
                self.log.emit("Not connected")
                return
            self.last_param = param.strip().upper()

            name = select_param_quiet(self.s, self.last_param)
            configure_sweep(self.s, center, span, points, ifbw)
            self.log.emit(f"Applied: {self.last_param} (selected {name}), sweep configured.")
        except Exception:
            self.log.emit("Apply failed:\n" + traceback.format_exc())

    @QtCore.pyqtSlot()
    def sweep_and_fetch(self):
        try:
            if not self.s:
                self.log.emit("Not connected")
                return
            ok = run_sweep(self.s, timeout_s=300.0)
            if not ok:
                self.log.emit("Sweep did not complete (*OPC? != 1).")
                return
            f, re, im = fetch_data(self.s)
            self.data.emit(f, re, im)
            self.log.emit(f"Sweep+Fetch OK ({len(f)} pts).")
        except Exception:
            self.log.emit("Sweep/Fetch failed:\n" + traceback.format_exc())


class Main(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PNA minimal (quiet S11/S21)")
        self.resize(740, 420)

        cw = QtWidgets.QWidget()
        self.setCentralWidget(cw)
        v = QtWidgets.QVBoxLayout(cw)

        # Connect row
        r = QtWidgets.QHBoxLayout()
        self.ip = QtWidgets.QLineEdit("192.168.147.75")
        self.btn_conn = QtWidgets.QPushButton("Connect")
        self.btn_disc = QtWidgets.QPushButton("Disconnect")
        self.lbl = QtWidgets.QLabel("Not connected")
        r.addWidget(QtWidgets.QLabel("IP:"))
        r.addWidget(self.ip, 1)
        r.addWidget(self.btn_conn)
        r.addWidget(self.btn_disc)
        r.addWidget(self.lbl, 2)
        v.addLayout(r)

        # Settings
        g = QtWidgets.QGridLayout()
        self.param = QtWidgets.QComboBox()
        self.param.addItems(["S21", "S11"])
        g.addWidget(QtWidgets.QLabel("Param"), 0, 0)
        g.addWidget(self.param, 0, 1)

        self.center = QtWidgets.QDoubleSpinBox()
        self.center.setDecimals(0)
        self.center.setRange(1, 50_000_000_000)
        self.center.setValue(1_298_100_001)
        self.center.setSuffix(" Hz")
        g.addWidget(QtWidgets.QLabel("Center"), 1, 0)
        g.addWidget(self.center, 1, 1)

        self.span = QtWidgets.QDoubleSpinBox()
        self.span.setDecimals(0)
        self.span.setRange(1, 50_000_000_000)
        self.span.setValue(100_000_000)
        self.span.setSuffix(" Hz")
        g.addWidget(QtWidgets.QLabel("Span"), 2, 0)
        g.addWidget(self.span, 2, 1)

        self.points = QtWidgets.QComboBox()
        for p in [51, 101, 201, 401, 801, 1601]:
            self.points.addItem(str(p), p)
        self.points.setCurrentIndex(5)
        g.addWidget(QtWidgets.QLabel("Points"), 3, 0)
        g.addWidget(self.points, 3, 1)

        self.ifbw = QtWidgets.QComboBox()
        for v_if in [1, 3, 10, 30, 100, 300, 1_000, 3_000, 10_000, 30_000, 100_000, 300_000, 1_000_000]:
            self.ifbw.addItem(f"{v_if} Hz", float(v_if))
        self.ifbw.setCurrentIndex(8)  # 10kHz
        g.addWidget(QtWidgets.QLabel("IFBW"), 4, 0)
        g.addWidget(self.ifbw, 4, 1)

        v.addLayout(g)

        # Buttons
        r2 = QtWidgets.QHBoxLayout()
        self.btn_apply = QtWidgets.QPushButton("Apply (select + sweep)")
        self.btn_run = QtWidgets.QPushButton("Sweep + Fetch + Save…")
        r2.addWidget(self.btn_apply)
        r2.addWidget(self.btn_run)
        v.addLayout(r2)

        # Log
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        v.addWidget(self.log, 1)

        # Worker thread
        self.t = QtCore.QThread(self)
        self.w = Worker()
        self.w.moveToThread(self.t)
        self.t.start()

        # Signals
        self.btn_conn.clicked.connect(self.on_connect)
        self.btn_disc.clicked.connect(self.on_disconnect)
        self.btn_apply.clicked.connect(self.on_apply)
        self.btn_run.clicked.connect(self.on_run)

        self.w.log.connect(self.log.appendPlainText)
        self.w.idn.connect(self.lbl.setText)
        self.w.data.connect(self.on_data)

        self._last_data: Optional[Tuple[List[float], List[float], List[float]]] = None

    def on_connect(self):
        ip = self.ip.text().strip()
        QtCore.QMetaObject.invokeMethod(self.w, "connect_ip", QtCore.Qt.ConnectionType.QueuedConnection, QtCore.Q_ARG(str, ip))

    def on_disconnect(self):
        QtCore.QMetaObject.invokeMethod(self.w, "close", QtCore.Qt.ConnectionType.QueuedConnection)
        self.lbl.setText("Disconnected")
        self.log.appendPlainText("Disconnected.")

    def on_apply(self):
        param = self.param.currentText().strip()
        center = float(self.center.value())
        span = float(self.span.value())
        points = int(self.points.currentData())
        ifbw = float(self.ifbw.currentData())
        QtCore.QMetaObject.invokeMethod(
            self.w, "apply", QtCore.Qt.ConnectionType.QueuedConnection,
            QtCore.Q_ARG(str, param),
            QtCore.Q_ARG(float, center),
            QtCore.Q_ARG(float, span),
            QtCore.Q_ARG(int, points),
            QtCore.Q_ARG(float, ifbw),
        )

    def on_run(self):
        # Make sure apply is done before sweep+fetch
        self.on_apply()
        QtCore.QMetaObject.invokeMethod(self.w, "sweep_and_fetch", QtCore.Qt.ConnectionType.QueuedConnection)

    def on_data(self, f: list, re: list, im: list):
        self._last_data = (f, re, im)
        param = self.param.currentText().strip()

        dlg = QtWidgets.QFileDialog(self, "Save CTI", filter="CTI files (*.cti);;All files (*.*)")
        dlg.setAcceptMode(QtWidgets.QFileDialog.AcceptMode.AcceptSave)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            path = dlg.selectedFiles()[0]
            try:
                save_cti(path, param, f, re, im)
                self.log.appendPlainText(f"Saved CTI → {path}")
            except Exception:
                self.log.appendPlainText("Save failed:\n" + traceback.format_exc())
        else:
            self.log.appendPlainText("Save canceled.")

    def closeEvent(self, e):
        try:
            QtCore.QMetaObject.invokeMethod(self.w, "close", QtCore.Qt.ConnectionType.QueuedConnection)
        finally:
            self.t.quit()
            self.t.wait(1500)
        super().closeEvent(e)


def main():
    if "--debug" in sys.argv:
        sys.argv.remove("--debug")
    app = QtWidgets.QApplication(sys.argv)
    m = Main()
    m.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
