#!/usr/bin/env python3
"""
PNA E8358A Control GUI — PyQt6

Purpose
-------
Small desktop GUI to control an Agilent/Keysight PNA (e.g., E8358A) via VISA socket (TCP 5025).

Features
- Connect by IP, query *IDN?
- Select S-parameter (S11/S21)
- Configure center frequency, span (Hz), IF bandwidth (device-typical list), sweep points (device-typical list)
- 1 Hz precision for frequencies via QDoubleSpinBox (step=1 Hz)
- Start a single sweep (INIT; *OPC?)
- Fetch latest trace as complex SDATA and save to CTI-like text file
  (Format: 4-line minimal CTI explained in save_cti())

Notes
- Reuses a small VISA helper similar to your pna_tool.py; no external deps besides PyQt6 and pyvisa.
- The IFBW and points lists are typical for PNAs of this generation; adjust if your unit has a different set.
- All instrument comms run in a worker thread to keep the GUI responsive.

CTI format used here
--------------------
A simple, explicit 4-line text format for robustness and readability; extension ".cti":
    Line 1: # CTI v1; param=S21; points=NNN
    Line 2: f_Hz: comma-separated floats (N values)
    Line 3: re:   comma-separated floats (N values)
    Line 4: im:   comma-separated floats (N values)
This ships along a matching `load_cti()` for your analysis pipeline; adapt as needed.
"""

from __future__ import annotations
import sys
import math
import traceback
from dataclasses import dataclass
from typing import Optional, Tuple, List

import pyvisa
from PyQt6 import QtCore, QtWidgets


# -------------------------
# VISA session helpers
# -------------------------
@dataclass
class PNASession:
    rm: pyvisa.ResourceManager
    inst: pyvisa.resources.MessageBasedResource


def open_session(ip: str, backend: str = "", timeout_ms: int = 60000) -> PNASession:
    rm = pyvisa.ResourceManager(backend)
    res = f"TCPIP0::{ip}::5025::SOCKET"
    inst = rm.open_resource(res, timeout=timeout_ms)
    inst.write_termination = "\n"
    inst.read_termination = "\n"
    inst.chunk_size = 1024 * 1024
    return PNASession(rm=rm, inst=inst)


def close_session(sess: Optional[PNASession]) -> None:
    if not sess:
        return
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
# Instrument configuration & acquisition
# -------------------------

def ensure_measurement(sess: PNASession, param: str = "S21", meas_name: str = "Meas1", ch: int = 1) -> None:
    param = param.upper()
    scpi(sess, "*CLS")
    scpi(sess, f"CALC{ch}:PAR:DEL:ALL")
    scpi(sess, f"CALC{ch}:PAR:DEF:EXT '{meas_name}','{param}'")
    scpi(sess, f"CALC{ch}:PAR:SEL '{meas_name}'")
    scpi(sess, "DISP:WIND1:STATE ON")
    scpi(sess, f"DISP:WIND1:TRAC1:FEED '{meas_name}'")
    scpi(sess, f"CALC{ch}:FORM MLOG")  # display in dB, even though we fetch SDATA
    scpi(sess, "FORM:DATA ASCII")
    scpi(sess, "OUTP ON")


def configure_sweep(
    sess: PNASession,
    center_hz: float,
    span_hz: float,
    points: int,
    ifbw_hz: Optional[float] = None,
    ch: int = 1,
) -> None:
    scpi(sess, f"SENS{ch}:SWE:TYPE LIN")
    scpi(sess, f"SENS{ch}:FREQ:CENT {center_hz:.0f}")  # 1 Hz resolution
    scpi(sess, f"SENS{ch}:FREQ:SPAN {span_hz:.0f}")
    scpi(sess, f"SENS{ch}:SWE:POIN {points}")
    if ifbw_hz is not None:
        scpi(sess, f"SENS{ch}:BWID {ifbw_hz}")
    scpi(sess, f"INIT{ch}:CONT 0")
    scpi(sess, "TRIG:SEQ:SOUR IMM")


def get_frequency_axis(sess: PNASession, ch: int = 1) -> List[float]:
    freqs_csv = query(sess, f"SENS{ch}:FREQ:DATA?")
    return [float(v) for v in freqs_csv.split(",") if v]


def fetch_sdata_complex(sess: PNASession, ch: int = 1) -> Tuple[List[float], List[float]]:
    s_csv = query(sess, f"CALC{ch}:DATA? SDATA")
    vals = [float(v) for v in s_csv.split(",") if v]
    reals, imags = [], []
    it = iter(vals)
    for r, im in zip(it, it):
        reals.append(r)
        imags.append(im)
    return reals, imags


def run_single_sweep(sess: PNASession, ch: int = 1, timeout_s: float = 120.0) -> bool:
    scpi(sess, "ABOR")
    scpi(sess, f"INIT{ch}:IMM")
    sess.inst.timeout = int(timeout_s * 1000)
    done = query(sess, "*OPC?")
    return done == "1"


# -------------------------
# Simple CTI I/O helpers
# -------------------------

def save_cti(path: str, param: str, freq_hz: List[float], re: List[float], im: List[float]) -> None:
    if not (len(freq_hz) == len(re) == len(im)):
        raise ValueError("Length mismatch between freq/re/im arrays")
    n = len(freq_hz)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# CTI v1; param={param}; points={n}\n")
        f.write("f_Hz:" + ",".join(f"{x:.6f}" for x in freq_hz) + "\n")
        f.write("re:" + ",".join(f"{x:.16e}" for x in re) + "\n")
        f.write("im:" + ",".join(f"{x:.16e}" for x in im) + "\n")


def load_cti(path: str) -> Tuple[str, List[float], List[float], List[float]]:
    """Small helper for your analysis scripts (optional). Returns (param, f, re, im)."""
    with open(path, "r", encoding="utf-8") as f:
        header = f.readline().strip()
        if not header.startswith("# CTI"):
            raise ValueError("Not a CTI v1 file")
        kv = {k: v for k, v in (part.split("=") for part in header.split(";") if "=" in part)}
        param = kv.get(" param", kv.get("param", "S21")).strip()
        f_line = f.readline().strip()
        r_line = f.readline().strip()
        i_line = f.readline().strip()
    freq = [float(x) for x in f_line.split(":", 1)[1].split(",") if x]
    re = [float(x) for x in r_line.split(":", 1)[1].split(",") if x]
    im = [float(x) for x in i_line.split(":", 1)[1].split(",") if x]
    if not (len(freq) == len(re) == len(im)):
        raise ValueError("CTI arrays length mismatch")
    return param, freq, re, im


# -------------------------
# Worker thread for instrument I/O
# -------------------------
class PNAWorker(QtCore.QObject):
    log = QtCore.pyqtSignal(str)
    idn_ready = QtCore.pyqtSignal(str)
    sweep_done = QtCore.pyqtSignal(bool)
    data_ready = QtCore.pyqtSignal(list, list, list)  # freq, re, im

    def __init__(self):
        super().__init__()
        self.sess: Optional[PNASession] = None

    @QtCore.pyqtSlot(str)
    def connect_ip(self, ip: str):
        try:
            self.close()
            self.sess = open_session(ip)
            who = idn(self.sess)
            self.idn_ready.emit(who)
            self.log.emit(f"Connected: {who}")
        except Exception as e:
            self.log.emit("Connect failed: " + str(e))
            self.close()

    @QtCore.pyqtSlot()
    def close(self):
        if self.sess:
            try:
                close_session(self.sess)
            finally:
                self.sess = None

    @QtCore.pyqtSlot(str, float, float, int, object)
    def setup_measurement(self, param: str, center_hz: float, span_hz: float, points: int, ifbw: Optional[float]):
        try:
            if not self.sess:
                self.log.emit("Not connected")
                return
            ensure_measurement(self.sess, param=param, meas_name="Meas1", ch=1)
            configure_sweep(self.sess, center_hz=center_hz, span_hz=span_hz, points=points, ifbw_hz=ifbw, ch=1)
            self.log.emit(
                f"Configured: {param} | center={center_hz:.0f} Hz | span={span_hz:.0f} Hz | points={points} | IFBW={ifbw} Hz"
            )
        except Exception:
            self.log.emit("Setup failed:\n" + traceback.format_exc())

    @QtCore.pyqtSlot()
    def start_sweep(self):
        try:
            if not self.sess:
                self.log.emit("Not connected")
                self.sweep_done.emit(False)
                return
            ok = run_single_sweep(self.sess, ch=1, timeout_s=300.0)
            self.sweep_done.emit(ok)
            self.log.emit("Sweep complete" if ok else "Sweep may not have completed (no *OPC)")
        except Exception:
            self.sweep_done.emit(False)
            self.log.emit("Sweep error:\n" + traceback.format_exc())

    @QtCore.pyqtSlot()
    def fetch_latest(self):
        try:
            if not self.sess:
                self.log.emit("Not connected")
                return
            freq = get_frequency_axis(self.sess, ch=1)
            re, im = fetch_sdata_complex(self.sess, ch=1)
            n = min(len(freq), len(re), len(im))
            freq, re, im = freq[:n], re[:n], im[:n]
            self.data_ready.emit(freq, re, im)
            self.log.emit(f"Fetched {n} points (complex SDATA)")
        except Exception:
            self.log.emit("Fetch error:\n" + traceback.format_exc())


# -------------------------
# GUI
# -------------------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PNA E8358A Control GUI")
        self.resize(800, 520)

        # Central widget
        cw = QtWidgets.QWidget()
        self.setCentralWidget(cw)
        layout = QtWidgets.QVBoxLayout(cw)

        # Connection row
        conn_row = QtWidgets.QHBoxLayout()
        self.ip_edit = QtWidgets.QLineEdit("192.168.147.75")
        self.btn_connect = QtWidgets.QPushButton("Connect")
        self.btn_disconnect = QtWidgets.QPushButton("Disconnect")
        self.lbl_idn = QtWidgets.QLabel("Not connected")
        conn_row.addWidget(QtWidgets.QLabel("PNA IP:"))
        conn_row.addWidget(self.ip_edit, 1)
        conn_row.addWidget(self.btn_connect)
        conn_row.addWidget(self.btn_disconnect)
        conn_row.addWidget(self.lbl_idn, 2)
        layout.addLayout(conn_row)

        # Config grid
        grid = QtWidgets.QGridLayout()
        row = 0
        self.combo_param = QtWidgets.QComboBox(); self.combo_param.addItems(["S21", "S11"])  # extend if needed
        grid.addWidget(QtWidgets.QLabel("Parameter"), row, 0); grid.addWidget(self.combo_param, row, 1)

        row += 1
        self.center_spin = QtWidgets.QDoubleSpinBox()
        self.center_spin.setDecimals(0)
        self.center_spin.setRange(1, 50_000_000_000)  # 1 Hz .. 50 GHz
        self.center_spin.setSingleStep(1)
        self.center_spin.setValue(1_298_100_001)  # default ~1 Hz precision
        self.center_spin.setSuffix(" Hz")
        grid.addWidget(QtWidgets.QLabel("Center Frequency"), row, 0); grid.addWidget(self.center_spin, row, 1)

        row += 1
        self.span_spin = QtWidgets.QDoubleSpinBox()
        self.span_spin.setDecimals(0)
        self.span_spin.setRange(1, 50_000_000_000)
        self.span_spin.setSingleStep(1)
        self.span_spin.setValue(100_000_000)  # 100 MHz default span
        self.span_spin.setSuffix(" Hz")
        grid.addWidget(QtWidgets.QLabel("Span"), row, 0); grid.addWidget(self.span_spin, row, 1)

        row += 1
        # Typical IF bandwidths (adjust to your unit if needed)
        self.combo_ifbw = QtWidgets.QComboBox()
        ifbw_values = [
            1, 3, 10, 30, 100, 300,
            1_000, 3_000, 10_000, 30_000, 100_000, 300_000,
            1_000_000
        ]
        for v in ifbw_values:
            self.combo_ifbw.addItem(f"{v} Hz", v)
        self.combo_ifbw.setCurrentIndex(8)  # 10 kHz default
        grid.addWidget(QtWidgets.QLabel("IF Bandwidth"), row, 0); grid.addWidget(self.combo_ifbw, row, 1)

        row += 1
        self.combo_points = QtWidgets.QComboBox()
        for p in [51, 101, 201, 401, 801, 1601]:
            self.combo_points.addItem(str(p), p)
        self.combo_points.setCurrentIndex(5)  # 1601
        grid.addWidget(QtWidgets.QLabel("Sweep Points"), row, 0); grid.addWidget(self.combo_points, row, 1)

        layout.addLayout(grid)

        # Action buttons
        btn_row = QtWidgets.QHBoxLayout()
        self.btn_apply = QtWidgets.QPushButton("Apply Settings")
        self.btn_sweep = QtWidgets.QPushButton("Start Sweep")
        self.btn_fetch_save = QtWidgets.QPushButton("Fetch && Save CTI…")
        btn_row.addWidget(self.btn_apply)
        btn_row.addWidget(self.btn_sweep)
        btn_row.addWidget(self.btn_fetch_save)
        layout.addLayout(btn_row)

        # Log view
        self.log_edit = QtWidgets.QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        layout.addWidget(self.log_edit, 1)

        # Threaded worker
        self.thread = QtCore.QThread(self)
        self.worker = PNAWorker()
        self.worker.moveToThread(self.thread)
        self.thread.start()

        # Wire signals
        self.btn_connect.clicked.connect(self.on_connect)
        self.btn_disconnect.clicked.connect(self.on_disconnect)
        self.btn_apply.clicked.connect(self.on_apply)
        self.btn_sweep.clicked.connect(self.on_sweep)
        self.btn_fetch_save.clicked.connect(self.on_fetch_save)

        self.worker.log.connect(self.append_log)
        self.worker.idn_ready.connect(self.lbl_idn.setText)
        self.worker.sweep_done.connect(self.on_sweep_done)
        self.worker.data_ready.connect(self.on_data_ready)

        # Cache last fetched data
        self._last_data: Optional[Tuple[List[float], List[float], List[float]]] = None

    # --------------- UI slots ---------------
    def append_log(self, msg: str):
        self.log_edit.appendPlainText(msg)

    def on_connect(self):
        ip = self.ip_edit.text().strip()
        QtCore.QMetaObject.invokeMethod(self.worker, "connect_ip", QtCore.Qt.ConnectionType.QueuedConnection, QtCore.Q_ARG(str, ip))

    def on_disconnect(self):
        QtCore.QMetaObject.invokeMethod(self.worker, "close", QtCore.Qt.ConnectionType.QueuedConnection)
        self.lbl_idn.setText("Disconnected")
        self.append_log("Disconnected")

    def read_settings(self) -> Tuple[str, float, float, int, Optional[float]]:
        param = self.combo_param.currentText()
        center = float(self.center_spin.value())
        span = float(self.span_spin.value())
        points = int(self.combo_points.currentData())
        ifbw = int(self.combo_ifbw.currentData())
        return param, center, span, points, float(ifbw)

    def on_apply(self):
        param, center, span, points, ifbw = self.read_settings()
        QtCore.QMetaObject.invokeMethod(
            self.worker,
            "setup_measurement",
            QtCore.Qt.ConnectionType.QueuedConnection,
            QtCore.Q_ARG(str, param),
            QtCore.Q_ARG(float, center),
            QtCore.Q_ARG(float, span),
            QtCore.Q_ARG(int, points),
            QtCore.Q_ARG(object, ifbw),
        )

    def on_sweep(self):
        self.on_apply()
        QtCore.QMetaObject.invokeMethod(self.worker, "start_sweep", QtCore.Qt.ConnectionType.QueuedConnection)

    def on_sweep_done(self, ok: bool):
        self.append_log("Sweep OK" if ok else "Sweep failed or timed out")

    def on_fetch_save(self):
        # Fetch
        QtCore.QMetaObject.invokeMethod(self.worker, "fetch_latest", QtCore.Qt.ConnectionType.QueuedConnection)

    def on_data_ready(self, freq: list, re: list, im: list):
        self._last_data = (freq, re, im)
        n = len(freq)
        # Save dialog
        dlg = QtWidgets.QFileDialog(self, "Save CTI", filter="CTI files (*.cti);;All files (*.*)")
        dlg.setAcceptMode(QtWidgets.QFileDialog.AcceptMode.AcceptSave)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            path = dlg.selectedFiles()[0]
            param = self.combo_param.currentText()
            try:
                save_cti(path, param, freq, re, im)
                self.append_log(f"Saved CTI ({n} pts) → {path}")
            except Exception:
                self.append_log("Save error:\n" + traceback.format_exc())
        else:
            self.append_log(f"Fetched {n} pts (not saved)")


def main():
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
