import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from epics import caget
import math

PVNAME = "TEMP1Z2VHF:rdTemp"
xs, ys = [], []

fig, ax = plt.subplots()
(line,) = ax.plot([], [], lw=1.5)
ax.set_xlabel("sample #")
ax.set_ylabel("temperature TEMP1Z2VHF")
ax.grid(True)

def update(frame):
    # Wert holen (etwas längeres Timeout für die erste Verbindung)
    v = caget(PVNAME, timeout=1.0 if frame == 0 else 0.3)
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        # keinen ungültigen Punkt anhängen
        return (line,)

    xs.append(len(xs))
    ys.append(float(v))

    line.set_data(xs, ys)
    ax.relim()
    ax.autoscale_view()   # skaliert x/y automatisch neu
    return (line,)

# WICHTIG: Referenz behalten!
anim = FuncAnimation(fig, update, interval=500, blit=False)

plt.show()
