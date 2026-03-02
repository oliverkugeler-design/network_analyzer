#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import sys
import traceback
from dataclasses import dataclass
from typing import Optional, Tuple, List

import pyvisa
from PyQt6 import QtCore, QtWidgets


DEBUG_SCPI = ("--debug" in sys.argv)


@dataclass
class PNASession:
    rm: pyvisa.ResourceManager
    inst: pyvisa.resources.MessageBasedResource


def open_session(ip: str, backend: str = "", timeout_ms: int = 60000) -> PNASession:
    res = f"TCPIP0::{ip}::5025::SOCKET"
    last_err = None
    for b in ([backend] if backend else []) + ["", "@py"]:
        try:
            rm = pyvisa.ResourceManager(b)
            inst = rm.open_resource(res, timeout=timeout_ms)
            inst.write_termination = "\n"
            inst.read_termination = "\n"
            inst.chunk_size = 1024 * 1024
            return PNASession(rm=rm, inst=inst)
        except Exception as e:
            last_err = e
    raise RuntimeError(
        f"Could not open VISA resource {res}. Last error: {last_err}\n"
        "Hint: install system VISA (NI/Keysight) or: pip install pyvisa-py"
    )


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


def query(sess: PNASession, cmd: str) -> str:
    return sess.inst.query(cmd).strip()


def scpi(sess: PNASession, cmd: str) -> None:
    # In "normal mode" we do NOT query SYST:ERR? after each command,
    # to keep the instrument quiet and to avoid queue-shifting confusion.
    if DEBUG_SCPI:
        print(">>", cmd)
    sess.inst.write(cmd)


def clear_errors(sess: PNASession) -> None:
    # *CLS clears ESR + error queue on most instruments
    try:
        scpi(sess, "*CLS")
    except Exception:
        pass


def idn(sess: PNASession) -> str:
    return query(sess, "*IDN?")


def par_cat(sess: PNASession, ch: int = 1) -> str:
    # Measurement catalog (name,param,name,param,...)
    return query(sess, f"CALC{ch}:PAR:CAT?")


def meas_exists(sess: PNASession, meas_name: str, ch: int = 1) -> bool:
    try:
        cat = par_cat(sess, ch=ch)
    except Exception:
        return False
    parts = [p.strip().strip('"') for p in cat.split(",") if p.strip()]
    names = set(parts[0::2]) if len(parts) >= 2 else set(parts)
    return meas_name in names


def ensure_measurement(sess: PNASession, param: str, meas_name: str, ch: int = 1) -> None:
    """
    Best-practice for your case:
    - Do NOT delete anything (avoids "not found" + selection->none popups)
    - Do NOT use DEF:EXT (your instrument doesn't support it)
    - Define only if missing, then select
    """
    param_u = param.upper().strip()

    # If missing, define it. Parameter is unquoted on many E-series NAs.
    if not meas_exists(sess, meas_name, ch=ch):
        scpi(sess, f'CALC{ch}:PAR:DEF "{meas_name}",{param_u}')

    # Select it (this controls what CALC:DATA? SDATA returns)
    scpi(sess, f'CALC{ch}:PAR:SEL "{meas_name}"')

    # Keep display format simple; doesn't matter for SDATA, but harmless
    try:
        scpi(sess, f"CALC{ch}:FORM MLOG")
    except Exception:
        pass

    try:
        scpi(sess, "FORM:DATA ASCII")
    except Exception:
        pass


def configure_sweep(sess: PNASession, center_hz: float, span_hz: float, points: int, ifbw_hz: Optional[float], ch: int = 1) -> None:
    scpi(sess, f"SENS{ch}:SWE:TYPE LIN")
    scpi(sess, f"SENS{ch}:FREQ:CENT {center_hz:.0f}")
    scpi(sess, f"SENS{ch}:FREQ:SPAN {span_hz:.0f}")
    scpi(sess, f"SENS{ch}:SWE:POIN {points}")
    if ifbw_hz is not None:
        scpi(sess, f"SENS{ch}:BWID {float(ifbw_hz)}")
    scpi(sess, f"INIT{ch}:CONT 0")
    scpi(sess, "TRIG:SEQ:SOUR IMM")


def run_single_sweep(sess: PNASession, ch: int = 1, timeout_s: float = 120.0) -> bool:
    scpi(sess, "ABOR")
    scpi(sess, f"INIT{ch}:IMM")
    sess.inst.timeout = int(timeout_s * 1000)
    return query(sess, "*OPC?") == "1"


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


def save_cti(path: str, param: str, freq_hz: List[float], re: List[float], im: List[float]) -> None:
    if not (len(freq_hz) == len(re) == len(im)):
        raise ValueError("Length mismatch between freq/re/im arrays")
    n = len(freq_hz)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# CTI v1; param={param}; points={n}\n")
        f.write("f_Hz:" + ",".join(f"{x:.6f}" for x in freq_hz) + "\n")
        f.write("re:" + ",".join(f"{x:.16e}" for x in re) + "\n")
        f.write("im:" + ",".join(f"{x:.16e}" for x in im) + "\n")


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
            clear_errors(self.sess)
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

            param_u = param.strip().upper()
            meas_name = f"Meas_{param_u}"  # Meas_S11 / Meas_S21

            # quiet + robust switching
            clear_errors(self.sess)
            ensure_measurement(self.sess, param=param_u, meas_name=meas_name, ch=1)
            configure_sweep(self.sess, center_hz=center_hz, span_hz=span_hz, points=points, ifbw_hz=ifbw, ch=1)
            clear_errors(self.sess)

            self.log.emit(f"Configured: {param_u} | center={center_hz:.0f} Hz | span={span_hz:.0f} Hz | points={points} | IFBW={ifbw} Hz")
        except Exception:
            self.log.emit("Setup failed:\n" + traceback.format_exc())

    @QtCore.pyqtSlot()
    def start_sweep(self):
        try:
            if not self.sess:
                self.log.emit("Not connected")
                self.sweep_done.emit(False)
                return
            clear_errors(self.sess)
            ok = run_single_sweep(self.sess, ch=1, timeout_s=300.0)
            clear_errors(self.sess)
            self.sweep_done.emit(ok)
            self.log.emit("Sweep complete" if ok else "Sweep may not have completed (*OPC?)")
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
            self.data_ready.emit(freq[:n], re[:n], im[:n])
            self.log.emit(f"Fetched {n} points (complex SDATA)")
        except Exception:
            self.log.emit("Fetch error:\n" + traceback.format_exc())


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PNA E8358A Control GUI (quiet S11/S21 switch)")
        self.resize(800, 520)

        cw = QtWidgets.QWidget()
        self.setCentralWidget(cw)
        layout = QtWidgets.QVBoxLayout(cw)

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

        grid = QtWidgets.QGridLayout()
        self.combo_param = QtWidgets.QComboBox()
        self.combo_param.addItems(["S21", "S11"])
        grid.addWidget(QtWidgets.QLabel("Parameter"), 0, 0)
        grid.addWidget(self.combo_param, 0, 1)

        self.center_spin = QtWidgets.QDoubleSpinBox()
        self.center_spin.setDecimals(0)
        self.center_spin.setRange(1, 50_000_000_000)
        self.center_spin.setValue(1_298_100_001)
        self.center_spin.setSuffix(" Hz")
        grid.addWidget(QtWidgets.QLabel("Center Frequency"), 1, 0)
        grid.addWidget(self.center_spin, 1, 1)

        self.span_spin = QtWidgets.QDoubleSpinBox()
        self.span_spin.setDecimals(0)
        self.span_spin.setRange(1, 50_000_000_000)
        self.span_spin.setValue(100_000_000)
        self.span_spin.setSuffix(" Hz")
        grid.addWidget(QtWidgets.QLabel("Span"), 2, 0)
        grid.addWidget(self.span_spin, 2, 1)

        self.combo_ifbw = QtWidgets.QComboBox()
        for v in [1, 3, 10, 30, 100, 300, 1_000, 3_000, 10_000, 30_000, 100_000, 300_000, 1_000_000]:
            self.combo_ifbw.addItem(f"{v} Hz", v)
        self.combo_ifbw.setCurrentIndex(8)  # 10 kHz
        grid.addWidget(QtWidgets.QLabel("IF Bandwidth"), 3, 0)
        grid.addWidget(self.combo_ifbw, 3, 1)

        self.combo_points = QtWidgets.QComboBox()
        for p in [51, 101, 201, 401, 801, 1601]:
            self.combo_points.addItem(str(p), p)
        self.combo_points.setCurrentIndex(5)
        grid.addWidget(QtWidgets.QLabel("Sweep Points"), 4, 0)
        grid.addWidget(self.combo_points, 4, 1)

        layout.addLayout(grid)

        btn_row = QtWidgets.QHBoxLayout()
        self.btn_apply = QtWidgets.QPushButton("Apply Settings")
        self.btn_sweep = QtWidgets.QPushButton("Start Sweep")
        self.btn_fetch_save = QtWidgets.QPushButton("Fetch && Save CTI…")
        btn_row.addWidget(self.btn_apply)
        btn_row.addWidget(self.btn_sweep)
        btn_row.addWidget(self.btn_fetch_save)
        layout.addLayout(btn_row)

        self.log_edit = QtWidgets.QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        layout.addWidget(self.log_edit, 1)

        self.thread = QtCore.QThread(self)
        self.worker = PNAWorker()
        self.worker.moveToThread(self.thread)
        self.thread.start()

        self.btn_connect.clicked.connect(self.on_connect)
        self.btn_disconnect.clicked.connect(self.on_disconnect)
        self.btn_apply.clicked.connect(self.on_apply)
        self.btn_sweep.clicked.connect(self.on_sweep)
        self.btn_fetch_save.clicked.connect(self.on_fetch_save)

        self.worker.log.connect(self.log_edit.appendPlainText)
        self.worker.idn_ready.connect(self.lbl_idn.setText)
        self.worker.sweep_done.connect(lambda ok: self.log_edit.appendPlainText("Sweep OK" if ok else "Sweep failed"))
        self.worker.data_ready.connect(self.on_data_ready)

        self._last_data: Optional[Tuple[List[float], List[float], List[float]]] = None

    def on_connect(self):
        ip = self.ip_edit.text().strip()
        QtCore.QMetaObject.invokeMethod(
            self.worker, "connect_ip",
            QtCore.Qt.ConnectionType.QueuedConnection,
            QtCore.Q_ARG(str, ip)
        )

    def on_disconnect(self):
        QtCore.QMetaObject.invokeMethod(self.worker, "close", QtCore.Qt.ConnectionType.QueuedConnection)
        self.lbl_idn.setText("Disconnected")
        self.log_edit.appendPlainText("Disconnected")

    def read_settings(self):
        param = self.combo_param.currentText()
        center = float(self.center_spin.value())
        span = float(self.span_spin.value())
        points = int(self.combo_points.currentData())
        ifbw = float(self.combo_ifbw.currentData())
        return param, center, span, points, ifbw

    def on_apply(self):
        param, center, span, points, ifbw = self.read_settings()
        QtCore.QMetaObject.invokeMethod(
            self.worker, "setup_measurement",
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

    def on_fetch_save(self):
        QtCore.QMetaObject.invokeMethod(self.worker, "fetch_latest", QtCore.Qt.ConnectionType.QueuedConnection)

    def on_data_ready(self, freq: list, re: list, im: list):
        self._last_data = (freq, re, im)
        n = len(freq)

        dlg = QtWidgets.QFileDialog(self, "Save CTI", filter="CTI files (*.cti);;All files (*.*)")
        dlg.setAcceptMode(QtWidgets.QFileDialog.AcceptMode.AcceptSave)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            path = dlg.selectedFiles()[0]
            param = self.combo_param.currentText()
            try:
                save_cti(path, param, freq, re, im)
                self.log_edit.appendPlainText(f"Saved CTI ({n} pts) → {path}")
            except Exception:
                self.log_edit.appendPlainText("Save error:\n" + traceback.format_exc())
        else:
            self.log_edit.appendPlainText(f"Fetched {n} pts (not saved)")


def main():
    if "--debug" in sys.argv:
        sys.argv.remove("--debug")

    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
