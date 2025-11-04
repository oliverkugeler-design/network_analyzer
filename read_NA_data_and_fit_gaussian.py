from pathlib import Path
from datetime import datetime
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.widgets import Button
import os
import math

# ===== Configuration =====
BASE_DIR = r"D:\CavityTesting\2025-10 Nb3Sn STFC\2025-10-23 VNA warmup 2"
EXTS = (".txt", ".dat", ".csv")
DB_KIND = "power"   # "power" (10*log10 P), "dBm", or "voltage" (20*log10 V)
# ========================

# Try SciPy for robust non-linear fit; fall back if unavailable
try:
    from scipy.optimize import curve_fit  # type: ignore
    HAVE_SCIPY = True
except Exception:
    HAVE_SCIPY = False

def latest_file(folder: str | os.PathLike, exts=EXTS) -> Path:
    folder = Path(folder)
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in exts]
    if not files:
        raise FileNotFoundError(f"No files with {exts} in {folder}")
    return max(files, key=lambda p: p.stat().st_mtime)

def load_quick(path: str | os.PathLike):
    """
    Fixed format:
      - line 2: date + time are first two tokens
      - lines 3–4 ignored
      - from line 5: '<freq> <S21>'
    Returns: freq_MHz, s21_dB, timestamp, filename
    """
    p = Path(path)
    with p.open("r", encoding="utf-8", errors="ignore") as f:
        _ = f.readline()             # line 1
        line2 = f.readline().strip() # line 2
        toks = line2.split()
        if len(toks) < 2:
            raise ValueError(f"{p.name}: line 2 missing date/time")
        date_str = toks[0].replace("-", "/")
        time_str = toks[1]
        ts = datetime.strptime(f"{date_str} {time_str}", "%Y/%m/%d %H:%M:%S")

        _ = f.readline()  # line 3
        _ = f.readline()  # line 4
        data = np.loadtxt(f, dtype=float, usecols=(0, 1))
        if data.ndim == 1 and data.size == 2:
            data = data.reshape(1, 2)

    freq = data[:, 0].astype(float)
    s21  = data[:, 1].astype(float)

    # If frequency is in Hz, convert to MHz
    if np.nanmedian(freq) > 1e6:
        freq = freq / 1e6

    return freq, s21, ts, p.name

# ----- dB conversions -----
def db_to_power(y_db: np.ndarray) -> np.ndarray:
    """
    Convert y (in dB) to **linear power** for Lorentzian fit.
    - "power":    P_rel = 10^(dB/10)
    - "dBm":      P_W   = 10^((dBm-30)/10)
    - "voltage":  V_rel = 10^(dB/20)  -> power ∝ V^2
    """
    kind = DB_KIND.lower()
    if kind == "power":
        return 10.0 ** (y_db / 10.0)
    elif kind == "dbm":
        return 10.0 ** ((y_db - 30.0) / 10.0)
    elif kind == "voltage":
        v = 10.0 ** (y_db / 20.0)
        return v * v
    else:
        raise ValueError(f"Unknown DB_KIND='{DB_KIND}'")

def power_to_db(P: np.ndarray) -> np.ndarray:
    """Map linear power back to the plotting dB scale."""
    kind = DB_KIND.lower()
    P = np.maximum(P, 1e-300)
    if kind == "power":
        return 10.0 * np.log10(P)
    elif kind == "dbm":
        return 10.0 * np.log10(P) + 30.0
    elif kind == "voltage":
        V = np.sqrt(P)
        return 20.0 * np.log10(np.maximum(V, 1e-300))
    else:
        raise ValueError(f"Unknown DB_KIND='{DB_KIND}'")

# ----- Lorentzian model in linear power -----
def lorentz(f, A, mu, gamma, C):
    """
    Power Lorentzian:
      P(f) = A / (1 + ((f - mu)/gamma)^2) + C
    FWHM (power) = 2*gamma
    """
    x = (f - mu) / gamma
    return A / (1.0 + x*x) + C

# ----- quick –3 dB bandwidth (for initial guess only) -----
def bandwidth_3db_hint(freq_mhz: np.ndarray, s21_db: np.ndarray, drop_db: float = 3.0):
    if len(freq_mhz) < 3:
        return np.nan, np.nan
    order = np.argsort(freq_mhz)
    f = freq_mhz[order]
    y = s21_db[order]
    i_peak = int(np.nanargmax(y))
    ypk, fpk = y[i_peak], f[i_peak]
    yt = ypk - drop_db

    def cross_left():
        for i in range(i_peak - 1, -1, -1):
            y0, y1 = y[i], y[i + 1]
            if (y0 - yt) * (y1 - yt) <= 0:
                x0, x1 = f[i], f[i + 1]
                if y1 == y0:
                    return 0.5 * (x0 + x1)
                return x0 + (yt - y0) * (x1 - x0) / (y1 - y0)
        return np.nan

    def cross_right():
        for i in range(i_peak, len(f) - 1):
            y0, y1 = y[i], y[i + 1]
            if (y0 - yt) * (y1 - yt) <= 0:
                x0, x1 = f[i], f[i + 1]
                if y1 == y0:
                    return 0.5 * (x0 + x1)
                return x0 + (yt - y0) * (x1 - x0) / (y1 - y0)
        return np.nan

    fL, fR = cross_left(), cross_right()
    if np.isnan(fL) or np.isnan(fR) or fR <= fL:
        return np.nan, fpk
    return float(fR - fL), fpk

# ----- robust Lorentzian fit in linear, normalized power -----
def robust_lorentz_fit(freq_mhz: np.ndarray, s21_db: np.ndarray):
    """
    Fit P(f) = A / (1 + ((f - mu)/gamma)^2) + C  in **linear power**.
    Uses baseline subtraction + normalization for conditioning.
    Returns (A, mu, gamma, C, FWHM) or None.
    """
    f = np.asarray(freq_mhz, dtype=float)
    P = db_to_power(np.asarray(s21_db, dtype=float))

    mask = np.isfinite(f) & np.isfinite(P)
    f, P = f[mask], P[mask]
    if f.size < 6:
        return None

    # Baseline & normalization
    C0 = float(np.nanpercentile(P, 10))
    P_sub = np.clip(P - C0, 1e-18, None)
    scale = float(np.nanmax(P_sub))
    if not np.isfinite(scale) or scale <= 0:
        return None
    Pn = P_sub / scale

    # Initial guesses
    fwhm_hint, mu0 = bandwidth_3db_hint(f, s21_db, 3.0)
    gamma0 = (fwhm_hint / 2.0) if (np.isfinite(fwhm_hint) and fwhm_hint > 0) \
             else max((f.max() - f.min()) / 10.0, 1e-6)
    A0, Cn0 = 1.0, 0.0

    if HAVE_SCIPY:
        try:
            def lnorm(ff, A, mu, gamma, Cn):
                return A / (1.0 + ((ff - mu)/gamma)**2) + Cn

            bounds = ([0.0,  f.min(),  1e-9,   -0.2],
                      [2.0,  f.max(),  np.inf,  0.5])
            popt, _ = curve_fit(lnorm, f, Pn, p0=[A0, mu0, gamma0, Cn0],
                                bounds=bounds, maxfev=20000)
            Ahat, muhat, ghat, Cnhat = map(float, popt)
            # Map back to original linear units:
            A_lin = Ahat * scale
            C_lin = Cnhat * scale + C0
            FWHM = 2.0 * ghat
            return (A_lin, muhat, ghat, C_lin, FWHM)
        except Exception:
            pass

    # ---- Fallback (no SciPy): fix mu,gamma from hints; LS for A,C ----
    if not (np.isfinite(mu0) and np.isfinite(gamma0) and gamma0 > 0):
        return None
    L = 1.0 / (1.0 + ((f - mu0) / gamma0)**2)
    M = np.vstack([L, np.ones_like(L)]).T   # [L, 1] * [A, C]^T ≈ P
    try:
        (A_lin, C_lin), *_ = np.linalg.lstsq(M, P, rcond=None)
        A_lin = float(max(A_lin, 0.0))
        C_lin = float(max(C_lin, 0.0))
        FWHM = 2.0 * float(gamma0)
        return (A_lin, float(mu0), float(gamma0), C_lin, FWHM)
    except Exception:
        return None

def main():
    # Load last file
    last = latest_file(BASE_DIR, EXTS)
    f_mhz, y_db, ts, fname = load_quick(last)

    # Fit in **linear power** with Lorentzian
    fit = robust_lorentz_fit(f_mhz, y_db)

    # Prepare data arrays for both units
    P_lin = db_to_power(y_db)
    y_db_plot = y_db.copy()

    # Build a smooth fit curve
    f_fit = np.linspace(np.nanmin(f_mhz), np.nanmax(f_mhz), 1200)
    if fit is not None:
        A, mu, gamma, C, FWHM = fit
        P_fit = lorentz(f_fit, A, mu, gamma, C)
        y_fit_db = power_to_db(P_fit)
    else:
        P_fit = np.full_like(f_fit, np.nan)
        y_fit_db = np.full_like(f_fit, np.nan)

    # --- Figure & artists
    fig = plt.figure(figsize=(9.5, 6))
    ax = fig.add_axes([0.10, 0.15, 0.80, 0.78])
    ax_btn = fig.add_axes([0.10, 0.05, 0.25, 0.06])

    # Data as symbols
    sc = ax.scatter(f_mhz, y_db_plot, s=16, label="data", zorder=3)
    # Fit as line
    (ln,) = ax.plot(f_fit, y_fit_db, linewidth=2.0, label="Lorentzian fit", zorder=2)

    # Title/labels
    ax.set_xlabel("Frequency (MHz)")
    ylabel_db = f"S21 ({'dBm' if DB_KIND.lower()=='dbm' else 'dB'})"
    ax.set_ylabel(ylabel_db)
    ttl = f"{fname} — {ts.strftime('%Y-%m-%d %H:%M:%S')}"
    if fit is not None:
        ttl += f"\nμ = {mu:.6f} MHz, FWHM = {FWHM*1e3:.1f} kHz"
    ax.set_title(ttl)
    ax.grid(True, linestyle="--", alpha=0.4)
    ax.legend(loc="best")

    # --- Unit toggle button (dB <-> W)
    state_is_db = True  # start in dB
    btn = Button(ax_btn, "Show in Watts", hovercolor="0.92")

    def to_offsets(x, y):
        return np.c_[x, y]

    def on_click(event):
        nonlocal state_is_db
        state_is_db = not state_is_db

        if state_is_db:
            # Switch to dB
            sc.set_offsets(to_offsets(f_mhz, y_db_plot))
            ln.set_data(f_fit, y_fit_db)
            ax.set_ylabel(ylabel_db)
            btn.label.set_text("Show in Watts")
        else:
            # Switch to Watts
            sc.set_offsets(to_offsets(f_mhz, P_lin))
            ln.set_data(f_fit, P_fit)
            ax.set_ylabel("Power (W)")
            btn.label.set_text("Show in dB")

        # Rescale y-limits to visible data
        y_all = []
        if state_is_db:
            y_all.extend(y_db_plot[np.isfinite(y_db_plot)].tolist())
            y_all.extend(y_fit_db[np.isfinite(y_fit_db)].tolist())
        else:
            y_all.extend(P_lin[np.isfinite(P_lin)].tolist())
            y_all.extend(P_fit[np.isfinite(P_fit)].tolist())
        if y_all:
            ymin, ymax = np.min(y_all), np.max(y_all)
            if ymin == ymax:
                ymin *= 0.9; ymax = ymax*1.1 if ymax != 0 else 1.0
            pad = 0.05 * (ymax - ymin if ymax != ymin else 1.0)
            ax.set_ylim(ymin - pad, ymax + pad)

        fig.canvas.draw_idle()

    btn.on_clicked(on_click)

    # Print fit summary to console
    if fit is None:
        print(f"Fit failed for {fname}")
    else:
        print(f"File: {fname}")
        print(f"Timestamp: {ts.strftime('%Y-%m-%d %H:%M:%S')}")
        print("Lorentzian fit (linear power):")
        print(f"  mu     = {mu:.6f} MHz")
        print(f"  gamma  = {gamma:.6f} MHz  ->  FWHM = {2*gamma*1e3:.2f} kHz")
        print(f"  A      = {A:.3e} (linear power units)")
        print(f"  C      = {C:.3e} (baseline power)")

    plt.show()

if __name__ == "__main__":
    main()
