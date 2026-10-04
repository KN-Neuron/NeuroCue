#!/usr/bin/env python3
"""Pierwsza weryfikacja na prawdziwym czepku BrainAccess (bez Electrona).

  python probe.py                       # lista czepków w zasięgu
  python probe.py "BA MAXI 001"         # impedancja 10 s, potem EEG 15 s ze statystykami
  python probe.py "BA MAXI 001" --tap C3  # test mapowania: stukaj w elektrodę C3, narzędzie pokaże który kanał zareagował

Wklej wynik, jeśli coś wygląda dziwnie. Szczególnie: kΩ (elektrody suche mogą mieć znacznie więcej niż 20 kΩ,
wtedy podnieś progi w aplikacji), 'motion' w bezruchu (powinno być < 0.03) i 'nOk' (ile kanałów bez artefaktów).
"""
import sys
import time

import numpy as np

from dsp import Config, Engine
from sources import BrainAccessSource

args = [a for a in sys.argv[1:] if not a.startswith("--")]
tap = sys.argv[sys.argv.index("--tap") + 1] if "--tap" in sys.argv else None
if tap and tap in args:
    args.remove(tap)
if not args:
    print("Czepki w zasięgu:", BrainAccessSource.scan() or "brak (włącz czepek i Bluetooth)")
    sys.exit(0)

src = BrainAccessSource()
info = src.connect(args[0])
names = info["channels"]
print("Połączono:", info)
if info.get("unmappedCap"):
    print("UWAGA: nieznany układ elektrod dla tego modelu, kanały nazwane E1..En (wskaźniki motoryczne niedostępne).")
eng = Engine(names, Config(fs=info["fs"]), has_accel=info["accel"])
last = {}
raw_hist = []


def on_chunk(eeg, acc):
    raw_hist.append(eeg.copy())
    for f in eng.push(eeg, acc):
        last[f["ev"]] = f


def run(mode, seconds, show, every=2):
    eng.mode = mode
    eng.reset_stream()
    raw_hist.clear()
    src.start(mode, on_chunk)
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(every)
        show()
    src.stop()


def show_imp():
    f = last.get("impedance")
    if f:
        bad = {n: v for n, v in zip(names, f["kohm"])}
        print("impedancja [kΩ]:", bad if len(names) <= 8 else {k: round(v) for k, v in sorted(bad.items(), key=lambda kv: kv[1])})


def show_eeg():
    f = last.get("frame")
    if f:
        bad = [n for n, ok in zip(names, f["chOk"]) if not ok]
        print(f"motion={f['motion']:.3f} still={f['still']} nOk={f['nOk']}/{len(names)} clean={f['clean']} słabe: {bad or '—'}")


def rms_since(i0):
    x = np.concatenate(raw_hist[i0:], axis=1)
    x = x - x.mean(axis=1, keepdims=True)
    return x.std(axis=1)


try:
    if tap:
        print(f"\n--- test mapowania: elektroda {tap} ---")
        eng.mode = "eeg"; eng.reset_stream(); raw_hist.clear(); src.start("eeg", on_chunk)
        print("Nie dotykaj czepka przez 5 s…"); time.sleep(5)
        k = len(raw_hist); base = rms_since(0)
        print(f"STUKAJ w elektrodę {tap} przez 6 s (naciskaj i puszczaj)…"); time.sleep(6)
        src.stop()
        ratio = rms_since(k) / (base + 1e-9)
        order = np.argsort(-ratio)[:3]
        print("Najmocniej zareagowały kanały:", [(int(i), names[i], round(float(ratio[i]), 1)) for i in order])
        exp = names.index(tap) if tap in names else None
        if exp is None:
            print(f"{tap} nie występuje w układzie {info['model']}.")
        elif int(order[0]) == exp:
            print(f"OK: mapowanie zgodne z tabelą producenta (wejście {exp} = {tap}).")
        else:
            print(f"NIEZGODNOŚĆ: spodziewano wejścia {exp} ({tap}), a zareagowało {int(order[0])} ({names[order[0]]}). "
                  "Sprawdź kolejność kabli w złączu albo napisz do mnie, poprawię tabelę.")
    else:
        print("\n--- impedancja (10 s) ---"); run("impedance", 10, show_imp)
        print("\n--- EEG (15 s), siedź nieruchomo ---"); run("eeg", 15, show_eeg)
finally:
    src.disconnect()
    print("Rozłączono.")
