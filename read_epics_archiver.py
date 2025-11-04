import sys
import math
import json
from datetime import datetime, timedelta, timezone
import requests
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# --- Konfiguration ---
ARCHIVER_BASE = "https://archiver.bessy.de/HOBICAT/retrieval"
PV = "TEMP1Z2VHF:rdTemp"
HOURS =  48                        # Zeitraum: letzte x Stunden
VERIFY_SSL = True                  # bei Zertifikatsproblemen ggf. False setzen

def iso_utc(dt: datetime) -> str:
    """ISO8601 in UTC (Archiver mag 'Z' oder Offset)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def fetch_archiver_series(base_url: str, pv: str, t_from: datetime, t_to: datetime, verify=True):
    """
    Holt Daten über /data/getData.json.
    Rückgabe: (zeiten:list[datetime], werte:list[float])
    """
    url = f"{base_url}/data/getData.json"
    params = {
        "pv": pv,
        "from": iso_utc(t_from),
        "to": iso_utc(t_to),
    }
    r = requests.get(url, params=params, timeout=30, verify=verify)
    r.raise_for_status()
    payload = r.json()

    if not isinstance(payload, list) or not payload:
        raise RuntimeError("Unerwartete Antwortstruktur vom Archiver.")

    series = payload[0]  # erste Kurve (eine PV)
    data = series.get("data", [])
    times = []
    vals = []

    for pt in data:
        # typische Felder: secs, nanos (oder nsecs), val (skalar/array)
        secs = pt.get("secs") or pt.get("sec") or pt.get("seconds")
        nsecs = pt.get("nsecs") or pt.get("nanos") or pt.get("nano") or 0
        v = pt.get("val")
        # val kann Liste sein (z.B. Wellenform) – hier erwarten wir Skalar:
        if isinstance(v, list):
            # nimm erstes Element, oder überspringen – je nach Bedarf
            v = v[0] if v else None
        if secs is None or v is None:
            continue
        ts = datetime.fromtimestamp(secs + (nsecs / 1e9), tz=timezone.utc).astimezone()
        # Nur finite Zahlen plotten:
        try:
            vf = float(v)
            if math.isfinite(vf):
                times.append(ts)
                vals.append(vf)
        except Exception:
            continue

    return times, vals

def main():
    t_to = datetime.now(timezone.utc)
    t_from = t_to - timedelta(hours=HOURS)

    try:
        times, vals = fetch_archiver_series(ARCHIVER_BASE, PV, t_from, t_to, verify=VERIFY_SSL)
    except requests.exceptions.SSLError:
        print("SSL-Fehler. Versuche VERIFY_SSL=False oder installiere das Root-Zertifikat.", file=sys.stderr)
        raise
    except Exception as e:
        print(f"Fehler beim Laden: {e}", file=sys.stderr)
        raise

    if not times:
        print("Keine Daten im gewählten Zeitraum gefunden.")
        return

    # Plot
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(times, vals, marker=".", linestyle="-", linewidth=1.2)
    ax.set_title(f"{PV} (Archiver)\n{times[0].strftime('%Y-%m-%d %H:%M:%S')} – {times[-1].strftime('%Y-%m-%d %H:%M:%S')}")
    ax.set_xlabel("Zeit")
    ax.set_ylabel("Wert")
    ax.grid(True, linestyle="--", alpha=0.4)

    # hübsche Zeitachse
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax.xaxis.get_major_locator()))
    fig.autofmt_xdate()

    plt.tight_layout()
    plt.show()

if __name__ == "__main__":
    main()
