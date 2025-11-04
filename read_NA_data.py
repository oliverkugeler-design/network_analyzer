from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.widgets import CheckButtons
import matplotlib.gridspec as gridspec
import matplotlib.dates as mdates
from datetime import datetime
from typing import Tuple, List
import os

# ====== Configuration ======
BASE_DIR = r"D:\CavityTesting\2025-10 Nb3Sn STFC\2025-10-23 VNA warmup 2"
EXTS = (".txt", ".dat", ".csv")
DROP_DB = 3.0   # –3 dB bandwidth
MAX_PLOTS = 800  # limit how many datasets we render (was 265)
# ===========================

def list_data_files(folder: str | os.PathLike, exts=EXTS) -> List[Path]:
    p = Path(folder)
    return sorted([f for f in p.iterdir() if f.is_file() and f.suffix.lower() in exts])

def load_one(path: os.PathLike) -> Tuple[np.ndarray, np.ndarray, datetime, str]:
    """
    Fixed format:
      - line 2: date + time are the first two tokens (YYYY/MM/DD HH:MM:SS)
      - lines 3–4 ignored
      - from line 5: '<freq> <S21>' (freq may be in Hz or MHz depending on file)
    Returns: freq_MHz (float64), s21_dB (float64), timestamp, label
    """
    path = Path(path)
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        _ = f.readline()
        line2 = f.readline().strip()
        toks = line2.split()
        if len(toks) < 2:
            raise ValueError(f"{path.name}: Line 2 missing date/time")
        date_str = toks[0].replace("-", "/")
        time_str = toks[1]
        ts = datetime.strptime(f"{date_str} {time_str}", "%Y/%m/%d %H:%M:%S")
        _ = f.readline()
        _ = f.readline()
        data = np.loadtxt(f, dtype=float, usecols=(0, 1))
        if data.ndim == 1 and data.size == 2:
            data = data.reshape(1, 2)

    freq = np.asarray(data[:, 0], dtype=np.float64)
    s21  = np.asarray(data[:, 1], dtype=np.float64)

    # Unit check: only convert if clearly in Hz (avoid borderline medians)
    med = np.nanmedian(freq)
    if med > 1e7:  # >10 MHz -> almost certainly Hz
        freq = freq / 1e6  # Hz -> MHz

    label = ts.strftime("%Y-%m-%d %H:%M:%S")
    return freq, s21, ts, label

def bandwidth_details(freq_mhz: np.ndarray, s21_db: np.ndarray, drop_db: float = 3.0):
    """
    –drop_db bandwidth via linear interpolation on the dataset’s OWN frequency axis.
    Returns: (bw_mhz, f_left, f_right, y_target, f_peak_sample, y_peak_sample)
    """
    if len(freq_mhz) < 3:
        return (np.nan, np.nan, np.nan, np.nan, np.nan, np.nan)

    order = np.argsort(freq_mhz)
    f = freq_mhz[order].astype(np.float64)
    y = s21_db[order].astype(np.float64)

    i_peak = int(np.nanargmax(y))
    y_peak = y[i_peak]
    f_peak = f[i_peak]
    y_target = y_peak - drop_db

    def find_crossing(i_start, i_end, step):
        for i in range(i_start, i_end, step):
            y0, y1 = y[i], y[i + 1]
            if (y0 - y_target) * (y1 - y_target) <= 0:
                x0, x1 = f[i], f[i + 1]
                if y1 == y0:
                    return float(0.5 * (x0 + x1))
                # exact linear interpolation on absolute frequencies
                return float(x0 + (y_target - y0) * (x1 - x0) / (y1 - y0))
        return np.nan

    f_left  = find_crossing(i_peak - 1, -1, -1)
    f_right = find_crossing(i_peak, len(f) - 1, +1)

    if np.isnan(f_left) or np.isnan(f_right) or f_right <= f_left:
        return (np.nan, f_left, f_right, y_target, f_peak, y_peak)

    return (float(f_right - f_left), f_left, f_right, float(y_target), float(f_peak), float(y_peak))

def parabolic_peak_fit(freq_mhz: np.ndarray, s21_db: np.ndarray):
    """
    Sub-bin peak using a 3-point quadratic (parabola) fit around the maximum sample,
    performed directly in (f, y_dB) using the dataset’s own absolute frequencies.
    Works with non-uniform spacing.
    Returns (f_fit_MHz, y_fit_dB). Falls back to the sample max if needed.
    """
    if len(freq_mhz) < 3:
        i = int(np.nanargmax(s21_db))
        return float(freq_mhz[i]), float(s21_db[i])

    order = np.argsort(freq_mhz)
    f = freq_mhz[order].astype(np.float64)
    y = s21_db[order].astype(np.float64)
    i = int(np.nanargmax(y))
    if i == 0 or i == len(f) - 1:
        return float(f[i]), float(y[i])

    F = f[i-1:i+2]
    Y = y[i-1:i+2]
    try:
        a, b, c = np.polyfit(F, Y, 2)
        if a >= 0:  # not concave
            return float(f[i]), float(y[i])
        f_v = -b / (2*a)
        f_v = float(np.clip(f_v, F.min(), F.max()))  # no extrapolation
        y_v = float(a*f_v*f_v + b*f_v + c)
        return f_v, y_v
    except Exception:
        return float(f[i]), float(y[i])

def load_all(folder=BASE_DIR):
    datasets = []
    for f in list_data_files(folder):
        try:
            freq, s21, ts, label = load_one(f)
            bw_mhz, fL, fR, yT, fP, yP = bandwidth_details(freq, s21, drop_db=DROP_DB)
            f_fit, y_fit = parabolic_peak_fit(freq, s21)
            datasets.append({
                "ts": ts,
                "label": label,
                "freq": freq,            # absolute MHz for this file
                "s21": s21,
                "bw_mhz": bw_mhz,
                "f_left": fL,
                "f_right": fR,
                "y_target": yT,
                "f_peak": fP,            # sample max (MHz)
                "y_peak": yP,
                "f_peak_fit": f_fit,     # fitted peak (MHz)
                "y_peak_fit": y_fit,     # fitted peak value (dB)
                "fname": f.name
            })
        except Exception as e:
            print(f"Skipping {f.name}: {e}")
    datasets.sort(key=lambda d: d["ts"])
    return datasets[:MAX_PLOTS]

def make_figure(datasets):
    if not datasets:
        raise RuntimeError("No valid datasets loaded.")

    fig = plt.figure(figsize=(13.5, 10))
    gs = gridspec.GridSpec(3, 2, width_ratios=[4, 1], height_ratios=[3, 1, 1], figure=fig)
    ax_s21  = fig.add_subplot(gs[0, 0])
    ax_bw   = fig.add_subplot(gs[1, 0])
    ax_fpk  = fig.add_subplot(gs[2, 0])
    ax_chk  = fig.add_subplot(gs[:, 1])

    # --- S21 traces (each keeps its own absolute frequency vector) ---
    lines, spans, mleft, mright, labels, peakstars = [], [], [], [], [], []
    for d in datasets:
        (line,) = ax_s21.plot(d["freq"], d["s21"], linewidth=1.2, label=d["label"])
        color = line.get_color()

        # –3 dB visual on absolute frequency axis (MHz)
        if np.isfinite(d["bw_mhz"]):
            (span,) = ax_s21.plot([d["f_left"], d["f_right"]],
                                  [d["y_target"], d["y_target"]],
                                  linestyle="--", linewidth=1.1, alpha=0.85, color=color)
            (ml,) = ax_s21.plot(d["f_left"], d["y_target"], marker="o", linestyle="None",
                                markersize=4, alpha=0.9, color=color)
            (mr,) = ax_s21.plot(d["f_right"], d["y_target"], marker="o", linestyle="None",
                                markersize=4, alpha=0.9, color=color)
        else:
            (span,) = ax_s21.plot([], [], linestyle="--", linewidth=1.1, alpha=0.85, color=color)
            (ml,)   = ax_s21.plot([], [], marker="o", linestyle="None", markersize=4, alpha=0.9, color=color)
            (mr,)   = ax_s21.plot([], [], marker="o", linestyle="None", markersize=4, alpha=0.9, color=color)

        # Fitted peak marker (uses fitted absolute frequency, not a delta)
        (star,) = ax_s21.plot(d["f_peak_fit"], d["y_peak_fit"], marker="*", markersize=9,
                              linestyle="None", color=color, alpha=0.95)

        lines.append(line); spans.append(span); mleft.append(ml); mright.append(mr)
        peakstars.append(star); labels.append(d["label"])

    ax_s21.set_xlabel("Frequency (MHz)")
    ax_s21.set_ylabel("S21 (dB)")
    ax_s21.set_title(f"S21 Sweeps — BW @ -{int(DROP_DB)} dB; star = fitted peak (quadratic)")
    ax_s21.grid(True, which="both", linestyle="--", alpha=0.4)

    # --- Bandwidth vs Time (absolute values; units chosen once) ---
    bw_points, time_vals, y_vals_bw = [], [], []
    finite_bws_mhz = np.array([d["bw_mhz"] for d in datasets if np.isfinite(d["bw_mhz"])])
    use_khz = (finite_bws_mhz.size > 0) and (np.nanmax(finite_bws_mhz) < 5.0)
    for d, line in zip(datasets, lines):
        color = line.get_color()
        if np.isfinite(d["bw_mhz"]):
            t = d["ts"]; y = d["bw_mhz"] * 1e3 if use_khz else d["bw_mhz"]
            pt = ax_bw.scatter([mdates.date2num(t)], [y], s=36, picker=True, color=color)
            bw_points.append(pt); time_vals.append(t); y_vals_bw.append(y)
        else:
            pt = ax_bw.scatter([], [], s=36, picker=True, color=color); pt.set_visible(False)
            bw_points.append(pt); time_vals.append(None); y_vals_bw.append(np.nan)

    ax_bw.set_xlabel("Time (from header)")
    ax_bw.set_ylabel("Bandwidth (kHz)" if use_khz else "Bandwidth (MHz)")
    ax_bw.set_title(f"Bandwidth vs Time (BW @ -{int(DROP_DB)} dB)")
    ax_bw.grid(True, linestyle="--", alpha=0.4)
    ax_bw.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax_bw.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax_bw.xaxis.get_major_locator()))

    # --- Peak frequency vs Time (uses fitted absolute frequency in MHz) ---
    peak_points, y_vals_fpk = [], []
    for d, line in zip(datasets, lines):
        color = line.get_color()
        if np.isfinite(d["f_peak_fit"]):
            t = d["ts"]; y = d["f_peak_fit"]
            pt = ax_fpk.scatter([mdates.date2num(t)], [y], s=36, picker=True, color=color, marker="^")
            peak_points.append(pt); y_vals_fpk.append(y)
        else:
            pt = ax_fpk.scatter([], [], s=36, picker=True, color=color, marker="^")
            pt.set_visible(False)
            peak_points.append(pt); y_vals_fpk.append(np.nan)

    ax_fpk.set_xlabel("Time (from header)")
    ax_fpk.set_ylabel("Peak freq (MHz)")
    ax_fpk.set_title("Fitted peak frequency vs Time")
    ax_fpk.grid(True, linestyle="--", alpha=0.4)
    ax_fpk.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax_fpk.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax_fpk.xaxis.get_major_locator()))

    # --- Rescalers (bottom plots) ---
    def rescale_bw_axes():
        xs, ys = [], []
        for pt, t, y in zip(bw_points, time_vals, y_vals_bw):
            if pt.get_visible() and t is not None and np.isfinite(y):
                xs.append(mdates.date2num(t)); ys.append(y)
        if xs and ys:
            xpad = (max(xs) - min(xs)) * 0.05 or 0.5
            ypad = (max(ys) - min(ys)) * 0.10 or (0.1 if use_khz else 0.001)
            ax_bw.set_xlim(min(xs) - xpad, max(xs) + xpad)
            ax_bw.set_ylim(min(ys) - ypad, max(ys) + ypad)
        fig.canvas.draw_idle()

    def rescale_fpk_axes():
        xs, ys = [], []
        for pt, t, y in zip(peak_points, time_vals, y_vals_fpk):
            if pt.get_visible() and t is not None and np.isfinite(y):
                xs.append(mdates.date2num(t)); ys.append(y)
        if xs and ys:
            xpad = (max(xs) - min(xs)) * 0.05 or 0.5
            ypad = (max(ys) - min(ys)) * 0.10 or 0.001
            ax_fpk.set_xlim(min(xs) - xpad, max(xs) + xpad)
            ax_fpk.set_ylim(min(ys) - ypad, max(ys) + ypad)
        fig.canvas.draw_idle()

    # --- Checkboxes controlling ALL visuals ---
    actives = [True] * len(lines)
    label_to_index = {lab: i for i, lab in enumerate(labels)}
    font_size = 9 if len(labels) <= 15 else 8
    check = CheckButtons(ax=ax_chk, labels=labels, actives=actives)
    for text in check.labels:
        text.set_fontsize(font_size)

    def set_visibility(i: int, visible: bool):
        lines[i].set_visible(visible)
        spans[i].set_visible(visible)
        mleft[i].set_visible(visible)
        mright[i].set_visible(visible)
        # star marks fitted peak on the *absolute* x-scale
        peakstars[i].set_visible(visible)
        bw_points[i].set_visible(visible)
        peak_points[i].set_visible(visible)

    def on_checkbox(label_clicked):
        i = label_to_index[label_clicked]
        vis = not lines[i].get_visible()
        set_visibility(i, vis)
        ax_s21.relim(); ax_s21.autoscale_view()
        rescale_bw_axes()
        rescale_fpk_axes()

    check.on_clicked(on_checkbox)

    # Clicking a bottom-plot point toggles the dataset
    def on_pick(event):
        artist = event.artist
        if artist in bw_points:
            i = bw_points.index(artist)
            check.set_active(i)
        elif artist in peak_points:
            i = peak_points.index(artist)
            check.set_active(i)

    fig.canvas.mpl_connect('pick_event', on_pick)

    # Initial autoscale
    ax_s21.relim(); ax_s21.autoscale_view()
    rescale_bw_axes()
    rescale_fpk_axes()
    plt.tight_layout()
    plt.show()

if __name__ == "__main__":
    data = load_all(BASE_DIR)
    make_figure(data)
