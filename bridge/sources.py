"""Źródła danych EEG: symulator (testy, demo) i adapter czepka BrainAccess (SDK `brainaccess`).

Wspólny interfejs: connect() -> info, start(mode, on_chunk), stop(), disconnect().
on_chunk(eeg[n_ch, n] w µV, acc[3, n] | None) jest wołane z wątku źródła.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Callable

import numpy as np
from scipy.signal import lfilter

# Układy elektrod = numer wejścia złącza → pozycja 10–20 (instrukcja BrainAccess „Electrodes & Cables”,
# tabele 1–3). Uwaga: kolejność w MIDI/MAXI jest INNA niż w MINI. REF = Fp1, BIAS = Fp2 (nie są kanałami EEG).
MIDI_CAP = ["P8", "O2", "P4", "C4", "F8", "F4", "Oz", "Cz", "Fz", "Pz", "F3", "O1", "P7", "C3", "P3", "F7"]
MAXI_CAP = MIDI_CAP + ["T8", "FC6", "CP6", "CP2", "PO4", "FC2", "AF4", "POz", "AFz", "AF3", "FC1", "FC5", "T7", "CP1", "CP5", "PO3"]
CAPS = {
    "MINI": ["F3", "F4", "C3", "C4", "P3", "P4", "O1", "O2"],
    "MIDI": MIDI_CAP,
    "MAXI": MAXI_CAP,
    "HALO": ["Fp1", "Fp2", "O1", "O2"],   # układ z przykładu SDK
    "HALO1": ["Fp1", "Fp2", "O1", "O2"],
}

OnChunk = Callable[[np.ndarray, "np.ndarray | None"], None]

_ba_ready = False


def ba_init():
    """core.init() wolno wywołać tylko raz na proces (drugie wywołanie rzuca 'Library already initialized')."""
    global _ba_ready
    if not _ba_ready:
        from brainaccess import core
        core.init()
        _ba_ready = True


def ba_close():
    global _ba_ready
    if _ba_ready:
        from brainaccess import core
        core.close()
        _ba_ready = False


class SimSource:
    """Syntetyczne EEG: rytmy theta/alfa/mu/beta + szum, artefakty ruchu, zły kontakt, zmęczenie."""

    def __init__(self, cap: list[str] | None = None, fs: int = 250, speed: float = 1.0, seed: int = 7, model: str = "MINI"):
        self.model = model
        self.names = cap or list(CAPS.get(model, CAPS["MINI"]))
        self.fs, self.speed = fs, speed
        self.rng = np.random.default_rng(seed)
        self.params = {"motion": False, "fatigue": 0.0, "erd": 0.0, "bad": [], "flat": [], "zfinal": {}}
        self._thread = None
        self._stop = threading.Event()
        self._n = 0
        self.mode = "eeg"
        self._tau = self.rng.uniform(0.8, 2.5, len(self.names))
        self._phase = self.rng.uniform(0, 2 * math.pi, (len(self.names), 4))
        self._lp = np.zeros(len(self.names))

    def connect(self, name: str = ""):
        return {"name": "SYMULATOR " + self.model if self.model != "MINI" else "SYMULATOR", "model": self.model, "channels": self.names, "fs": self.fs, "accel": True, "battery": 87, "sim": True}

    def set_params(self, **kw):
        self.params.update({k: v for k, v in kw.items() if k in self.params})

    def start(self, mode: str, on_chunk: OnChunk, bias: list[int] | None = None):
        self.stop()
        self.bias = bias
        self.mode, self._n = mode, 0
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(on_chunk,), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None

    def disconnect(self):
        self.stop()

    def _run(self, on_chunk: OnChunk):
        step = 25  # 0.1 s
        dt = step / self.fs / max(self.speed, 1e-6)
        nxt = time.perf_counter()
        while not self._stop.is_set():
            eeg, acc = self._gen(step)
            on_chunk(eeg, acc)
            nxt += dt
            sl = nxt - time.perf_counter()
            if sl > 0:
                time.sleep(sl)

    def _gen(self, n: int):
        p, names, fs = self.params, self.names, self.fs
        t = (self._n + np.arange(n)) / fs
        self._n += n
        nc = len(names)
        if self.mode == "impedance":
            now = self._n / fs
            zfin = np.array([p["zfinal"].get(nm, 10.0) for nm in names])
            z = zfin + (150.0 - zfin) * np.exp(-now / self._tau)           # kΩ, kontakt „dochodzi”
            vpp = np.floor(z * 7.0)                                         # jak czepek: Vpp[µV] = Z[kΩ]·7 nA, stałe na kanał
            if now < 1.5:
                vpp = vpp * 0                                               # pierwsze sekundy: zera (jeszcze nie policzone)
            return np.repeat(vpp[:, None], n, axis=1), None
        eeg = np.zeros((nc, n))
        fat = float(p["fatigue"])
        for i, nm in enumerate(names):
            post = nm.startswith(("P", "O"))
            motor = nm in ("C3", "C4")
            a_alpha = 8.0 if post else (3.0 if not motor else 1.0)
            a_mu = 6.0 * (1.0 - 0.6 * float(p["erd"])) if motor else 0.0
            a_theta = 3.0 + 7.0 * fat
            a_beta = max(0.5, 2.5 - 1.5 * fat)
            x = (a_alpha * np.sin(2 * math.pi * 10.0 * t + self._phase[i, 0]) * (1 + 0.6 * fat)
                 + a_mu * np.sin(2 * math.pi * 11.0 * t + self._phase[i, 1])
                 + a_theta * np.sin(2 * math.pi * 6.0 * t + self._phase[i, 2])
                 + a_beta * np.sin(2 * math.pi * 20.0 * t + self._phase[i, 3]))
            w = self.rng.normal(0, 3.0, n)
            lp, zf = lfilter([0.1], [1.0, -0.9], w, zi=[0.9 * self._lp[i]])  # szum różowawy: dolnoprzepustowy biały szum
            self._lp[i] = lp[-1]
            w = lp * 4 + w * 0.3
            x = x + w + 0.4 * np.sin(2 * math.pi * 50.0 * t) + 150.0  # offset DC
            if nm in p["bad"]:
                x = x + 60.0 * np.sin(2 * math.pi * 50.0 * t) + self.rng.normal(0, 25.0, n)
            if nm in p["flat"]:
                x = np.full(n, 150.0)
            eeg[i] = x
        acc = np.vstack([self.rng.normal(0, 0.003, n), self.rng.normal(0, 0.003, n), 1.0 + self.rng.normal(0, 0.003, n)])
        if p["motion"]:
            burst = self.rng.normal(0, 1.0, (nc, n)) * 250.0
            eeg = eeg + burst
            acc = acc + self.rng.normal(0, 0.35, (3, n))
        return eeg, acc


class BrainAccessSource:
    """Adapter czepka BrainAccess (MINI/MIDI/MAXI/HALO) przez oficjalne SDK Pythona.

    UWAGA: napisane wg dokumentacji i przykładów SDK 3.6.x, ale NIE przetestowane na sprzęcie w tym repo.
    Do pierwszej weryfikacji użyj `python probe.py <nazwa_urządzenia>`.
    """

    def __init__(self, cap: list[str] | None = None):
        self.cap_override = cap
        self.mgr = None
        self.names: list[str] = []
        self.fs = 250
        self.has_accel = False
        self.on_disconnect: Callable[[], None] | None = None
        self.on_battery: Callable[[int], None] | None = None
        self._rows: dict | None = None
        self.name = ""
        self._mode: str | None = None

    @staticmethod
    def scan():
        from brainaccess import core
        ba_init()
        return [{"name": d.name, "mac": d.mac_address, "sim": False} for d in core.scan()]

    def connect(self, name: str):
        from brainaccess import core
        from brainaccess.core.eeg_manager import EEGManager
        ba_init()
        self.mgr = EEGManager()
        self.name = name
        status = self.mgr.connect(name)
        if status == 2:
            raise RuntimeError("Strumień jest niezgodny z firmware. Zaktualizuj oprogramowanie czepka.")
        if status != 0:
            raise RuntimeError(f"Połączenie nieudane (kod {status})")
        feats = self.mgr.get_device_features()
        info = self.mgr.get_device_info()
        n = feats.electrode_count()
        self.has_accel = bool(feats.has_accel())
        model = info.device_model.name
        cap = self.cap_override or CAPS.get(model)
        unmapped = cap is None or len(cap) != n
        self.names = list(cap) if not unmapped else [f"E{i + 1}" for i in range(n)]
        self.fs = int(self.mgr.get_sample_frequency())
        if self.on_disconnect:
            self.mgr.set_callback_disconnect(self.on_disconnect)
        if self.on_battery:
            self.mgr.set_callback_battery(lambda b: self.on_battery(int(b.level)))
        return {"name": name, "model": model, "channels": self.names, "fs": self.fs, "accel": self.has_accel,
                "battery": int(self.mgr.get_battery_info().level), "unmappedCap": unmapped, "sim": False}

    def start(self, mode: str, on_chunk: OnChunk, bias: list[int] | None = None):
        import brainaccess.core.eeg_channel as ch
        from brainaccess.core.polarity import Polarity
        from brainaccess.core.gain_mode import GainMode
        from brainaccess.core.impedance_measurement_mode import ImpedanceMeasurementMode as IM
        if self._mode is not None and self._mode != mode:
            self._reconnect()   # po zmianie trybu (impedancja ↔ EEG) czepek MAXI nie wznawia strumienia bez ponownego połączenia
        self._mode = mode
        m = self.mgr
        self._halt()
        m.set_impedance_mode(IM.HZ_31_2 if mode == "impedance" else IM.OFF)
        n = len(self.names)
        for i in range(n):  # ustawienia kanałów resetują się po stop_stream: konfigurujemy przy każdym starcie
            m.set_channel_enabled(ch.ELECTRODE_MEASUREMENT + i, True)
            m.set_channel_gain(ch.ELECTRODE_MEASUREMENT + i, GainMode.X8)
        if mode != "impedance":
            # Sprzężenie biasu liczone z wybranych kanałów: SDK zaleca tylko kanały z dobrym kontaktem.
            # Wybieramy je z pomiaru impedancji (bridge); bez pomiaru: ostatni kanał, jak w przykładzie SDK.
            for i in (bias or [n - 1]):
                m.set_channel_bias(ch.ELECTRODE_MEASUREMENT + i, Polarity.BOTH)
        if self.has_accel:
            for k in range(3):
                m.set_channel_enabled(ch.ACCELEROMETER + k, True)
        m.set_channel_enabled(ch.SAMPLE_NUMBER, True)
        m.set_channel_enabled(ch.STREAMING, True)
        m.set_sample_rate(self.fs)   # jak w acquisition.EEG z SDK (ustawienie resetuje się po stop_stream)
        m.load_config()
        # Wiersze paczki to włączone kanały w kolejności rosnących identyfikatorów (dokumentacja SDK).
        # get_channel_index działa dopiero po starcie strumienia, więc liczymy wiersze sami i weryfikujemy po starcie.
        ids = [ch.SAMPLE_NUMBER] + [ch.ELECTRODE_MEASUREMENT + i for i in range(n)] + ([ch.ACCELEROMETER + k for k in range(3)] if self.has_accel else []) + [ch.STREAMING]
        order = sorted(ids)
        rows = {"eeg": [order.index(ch.ELECTRODE_MEASUREMENT + i) for i in range(n)], "acc": [order.index(ch.ACCELEROMETER + k) for k in range(3)] if self.has_accel else None, "checked": False}

        def cb(chunk, size):
            a = np.asarray(chunk, dtype=float)
            if not rows["checked"]:
                rows["checked"] = True
                try:
                    sdk = [m.get_channel_index(ch.ELECTRODE_MEASUREMENT + i) for i in range(n)]
                    if sdk != rows["eeg"]:
                        rows["eeg"] = sdk
                        if self.has_accel:
                            rows["acc"] = [m.get_channel_index(ch.ACCELEROMETER + k) for k in range(3)]
                except Exception:
                    pass
            on_chunk(a[rows["eeg"]], a[rows["acc"]] if rows["acc"] else None)

        m.set_callback_chunk(cb)
        m.start_stream()

    def _reconnect(self):
        """Rozłącza i łączy ponownie TEN SAM menedżer (drugi EEGManager na tym samym czepku wywala bibliotekę natywną)."""
        try:
            self._halt()
            self.mgr.disconnect()
        except Exception:
            pass
        time.sleep(1.5)
        last = None
        for _ in range(3):
            try:
                last = self.mgr.connect(self.name)
                if last == 0:
                    break
            except Exception as e:
                last = e
            time.sleep(1.5)
        if last != 0:
            raise RuntimeError(f"Nie udało się ponownie połączyć z czepkiem ({last})")
        if self.on_disconnect:
            self.mgr.set_callback_disconnect(self.on_disconnect)
        if self.on_battery:
            self.mgr.set_callback_battery(lambda b: self.on_battery(int(b.level)))

    def _halt(self):
        """Zatrzymuje strumień i daje urządzeniu chwilę na zamknięcie (SDK w przykładach czeka 1 s po stop_stream)."""
        if self.mgr and self.mgr.is_streaming():
            self.mgr.stop_stream()
            time.sleep(1.0)

    def stop(self):
        self._halt()

    def battery(self) -> int | None:
        try:
            return int(self.mgr.get_battery_info().level)
        except Exception:
            return None

    def disconnect(self):
        try:
            self.stop()
            if self.mgr:
                self.mgr.disconnect()
        finally:
            self.mgr = None
