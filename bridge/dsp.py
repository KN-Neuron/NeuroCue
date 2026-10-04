"""Przetwarzanie sygnału EEG dla NeuroCue: filtrowanie, moce pasm, impedancja, artefakty, baseline, wskaźniki.

Wszystko tu jest czystą matematyką na tablicach numpy, więc da się to testować bez sprzętu.
Jednostki: sygnał EEG w µV, moc w µV², impedancja w kΩ.

UWAGA KLINICZNA: wskaźniki (skupienie, zmęczenie, obciążenie, relaksacja) to proste, jawnie opisane
heurystyki względem baseline'u pacjenta, NIE zwalidowany model kliniczny. W UI są oznaczone jako eksperymentalne.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np
from scipy import signal

BANDS = {"theta": (4.0, 8.0), "alpha": (8.0, 13.0), "beta": (13.0, 30.0)}

# Obszary zainteresowania (nazwy elektrod 10-20). Brakujące elektrody są pomijane.
ROI = {
    "motor": ("C3", "C4"),
    "frontal": ("Fz", "F3", "F4", "AFz", "Fp1", "Fp2"),                 # theta czołowa linii środkowej (Fz) gdy dostępna
    "posterior": ("P3", "P4", "Pz", "O1", "O2", "Oz", "POz"),
    "central": ("C3", "C4", "Cz", "P3", "P4", "Pz"),
}
# Kanały, bez których okno nie jest wiarygodne (ERD ruchowe liczymy z nich). Dotyczy tylko układów, które je mają.
ESSENTIAL = ("C3", "C4")


@dataclass
class Config:
    fs: int = 250
    mains_hz: float = 50.0            # Polska
    window_s: float = 2.0
    hop_s: float = 0.25
    artifact_ptp_uv: float = 150.0    # szczyt-szczyt po filtrze pasmowym
    flat_std_uv: float = 0.2
    mains_ratio_bad: float = 0.5      # udział mocy 50 Hz w paśmie 1–40 Hz
    motion_thr: float = 0.03          # std(|acc|)/mediana(|acc|), bezwymiarowo
    min_good_frac: float = 0.75       # czyste okno: min. tyle kanałów bez artefaktów (np. 6 z 8)
    imp_freq_hz: float = 31.25        # tryb HZ_31_2
    imp_current_na: float = 7.0       # Impedancja = Vpp / 7 nA (dokumentacja BrainAccess Board)
    imp_good_kohm: float = 20.0       # cel z prototypu: < 20 kΩ
    imp_bad_kohm: float = 50.0
    baseline_s: float = 90.0          # wymagane 90 s czystego sygnału


def pick_bias(names: list[str], kohm: list[float] | None, n_max: int = 2) -> list[int]:
    """Kanały do sprzężenia biasu: te z najlepszym kontaktem (SDK: „tylko kanały z dobrym kontaktem”).
    Bez pomiaru impedancji: kanał ciemieniowy/środkowy jeśli jest, inaczej ostatni."""
    if kohm and len(kohm) == len(names):
        order = sorted(range(len(names)), key=lambda i: kohm[i])
        return order[:n_max]
    for pref in ("Pz", "P4", "P3", "Cz"):
        if pref in names:
            return [names.index(pref)]
    return [len(names) - 1]


def make_filters(cfg: Config):
    nyq = cfg.fs / 2
    bp = signal.butter(4, [1.0 / nyq, 40.0 / nyq], btype="bandpass", output="sos")
    b, a = signal.iirnotch(cfg.mains_hz, 30.0, fs=cfg.fs)
    return bp, signal.tf2sos(b, a)


class StreamFilter:
    """Filtr pasmowy 1–40 Hz + notch sieci, z zachowaniem stanu między paczkami danych."""

    def __init__(self, n_ch: int, cfg: Config):
        self.bp, self.notch = make_filters(cfg)
        self.zi_bp = np.zeros((self.bp.shape[0], n_ch, 2))
        self.zi_n = np.zeros((self.notch.shape[0], n_ch, 2))
        self.primed = False

    def __call__(self, x: np.ndarray) -> np.ndarray:  # x: (n_ch, n)
        if not self.primed:  # unika przebiegu przejściowego od offsetu DC; notch dostaje sygnał już bez DC, więc stan zerowy
            self.zi_bp = signal.sosfilt_zi(self.bp)[:, None, :] * x[:, :1][None]
            self.primed = True
        y, self.zi_bp = signal.sosfilt(self.bp, x, axis=-1, zi=self.zi_bp)
        y, self.zi_n = signal.sosfilt(self.notch, y, axis=-1, zi=self.zi_n)
        return y


def band_powers(win: np.ndarray, fs: int) -> dict[str, np.ndarray]:
    """Moc w pasmach (µV²) dla każdego kanału; Welch, rozdzielczość 1 Hz."""
    f, p = signal.welch(win, fs=fs, nperseg=min(fs, win.shape[-1]), noverlap=min(fs, win.shape[-1]) // 2, axis=-1)
    df = f[1] - f[0]
    out = {}
    for name, (lo, hi) in BANDS.items():
        m = (f >= lo) & (f < hi)
        out[name] = p[:, m].sum(axis=-1) * df
    return out


def impedance_kohm(raw: np.ndarray, cfg: Config) -> list[float | None]:
    """Impedancja z wartości zwracanych przez urządzenie w trybie impedancji.

    Czepek BrainAccess liczy napięcie szczyt–szczyt sygnału testowego (stałe na kanał, odświeżane ok. 1/s;
    zera = jeszcze nie policzone). Z dokumentacji BrainAccess Board: Z = Vpp / 7 nA, więc przy Vpp w µV: Z[kΩ] = Vpp / I[nA].
    ZAŁOŻENIE do potwierdzenia na głowie pacjenta: wartość surowa to Vpp w µV (patrz probe.py).
    """
    return [None if v <= 0 else float(v) / cfg.imp_current_na for v in raw]


def channel_flags(raw: np.ndarray, filt: np.ndarray, cfg: Config) -> np.ndarray:
    """True = kanał bez artefaktów i z dobrym kontaktem (heurystyki z samego sygnału)."""
    ptp = filt.max(axis=-1) - filt.min(axis=-1)
    flat = filt.std(axis=-1) < cfg.flat_std_uv
    f, p = signal.welch(raw - raw.mean(axis=-1, keepdims=True), fs=cfg.fs, nperseg=min(cfg.fs, raw.shape[-1]), axis=-1)
    broad = p[:, (f >= 1) & (f <= 40)].sum(axis=-1) + 1e-12
    mains = p[:, (f >= cfg.mains_hz - 2) & (f <= cfg.mains_hz + 2)].sum(axis=-1)
    return (ptp < cfg.artifact_ptp_uv) & ~flat & ((mains / broad) < cfg.mains_ratio_bad)


def motion_level(acc: np.ndarray) -> float:
    """Poziom ruchu z akcelerometru (3, n); bezwymiarowo, więc niezależnie od jednostek czujnika."""
    mag = np.linalg.norm(acc, axis=0)
    return float(mag.std() / (abs(np.median(mag)) + 1e-9))


def _roi_idx(names: list[str], roi: str) -> list[int]:
    idx = [i for i, n in enumerate(names) if n in ROI[roi]]
    return idx or list(range(len(names)))


def _mean(a: np.ndarray, idx: list[int]) -> float:
    return float(np.mean(a[idx]))


def raw_features(bp: dict[str, np.ndarray], names: list[str], good: np.ndarray) -> dict[str, float]:
    """Cechy okna, liczone tylko z dobrych kanałów."""
    def sel(roi: str):
        idx = [i for i in _roi_idx(names, roi) if good[i]] or [i for i in range(len(names)) if good[i]]
        return idx
    th, al, be = bp["theta"], bp["alpha"], bp["beta"]
    c, fr, po = sel("central"), sel("frontal"), sel("posterior")
    out = {
        "engage": _mean(be, c) / (_mean(al, c) + _mean(th, c) + 1e-12),   # β/(α+θ)
        "fatigue": (_mean(th, c) + _mean(al, c)) / (_mean(be, c) + 1e-12),  # (θ+α)/β
        "load": _mean(th, fr),
        "relax": _mean(al, po),
    }
    for name in ("C3", "C4"):
        if name in names and good[names.index(name)]:
            out["mu_" + name] = float(al[names.index(name)])
    return out


@dataclass
class Baseline:
    values: dict[str, float]
    clean_s: float
    good_channels: list[str]


def tile(rel: float) -> float:
    """Wartość względem baseline'u → skala 0–100 (50 = poziom baseline'u)."""
    return float(min(100.0, max(0.0, 50.0 * rel)))


def indices(feat: dict[str, float], base: Baseline) -> dict:
    b = base.values
    r = lambda k: feat[k] / (b[k] + 1e-12)
    erd = {n[3:]: float(100.0 * (1.0 - feat[n] / (b[n] + 1e-12))) for n in feat if n.startswith("mu_") and n in b}
    return {"focus": tile(r("engage")), "fatigue": tile(r("fatigue")), "load": tile(r("load")), "relax": tile(r("relax")), "erd": erd}


@dataclass
class Engine:
    """Strumieniowy silnik cech: przyjmuje paczki EEG/akcelerometru, zwraca ramki co hop_s."""
    names: list[str]
    cfg: Config = field(default_factory=Config)
    has_accel: bool = False

    def __post_init__(self):
        n = len(self.names)
        self.n = n
        self.win = int(self.cfg.window_s * self.cfg.fs)
        self.hop = int(self.cfg.hop_s * self.cfg.fs)
        self.filt = StreamFilter(n, self.cfg)
        self.raw_buf = np.zeros((n, 0))
        self.flt_buf = np.zeros((n, 0))
        self.acc_buf = np.zeros((3, 0))
        self.since = 0
        self.t = 0.0
        self.mode = "eeg"          # 'eeg' | 'impedance'
        self.collecting = False    # zbieranie baseline'u
        self.essential = [i for i, nm in enumerate(self.names) if nm in ESSENTIAL]
        self.last_imp: list[float] | None = None
        self.base_frames: list[tuple[dict, np.ndarray]] = []
        self.clean_s = 0.0
        self.baseline: Baseline | None = None
        self.recent: deque = deque(maxlen=16)   # ostatnie 4 s czystych cech: mediana stabilizuje wskaźniki

    # ---- sterowanie ----
    def reset_stream(self):
        self.filt = StreamFilter(self.n, self.cfg)
        self.raw_buf = np.zeros((self.n, 0)); self.flt_buf = np.zeros((self.n, 0)); self.acc_buf = np.zeros((3, 0))
        self.since = 0
        self.recent.clear()

    def set_thresholds(self, good: float | None = None, bad: float | None = None):
        if good is not None:
            self.cfg.imp_good_kohm = float(good)
        if bad is not None:
            self.cfg.imp_bad_kohm = float(bad)

    def min_good(self) -> int:
        return math.ceil(self.cfg.min_good_frac * self.n)

    def window_ok(self, good: np.ndarray) -> bool:
        """Dość dobrych kanałów (min. 75%) ORAZ kanały kluczowe (C3/C4, jeśli są w układzie) bez artefaktów."""
        return int(good.sum()) >= self.min_good() and all(good[i] for i in self.essential)

    def start_baseline(self):
        self.collecting, self.base_frames, self.clean_s = True, [], 0.0

    def stop_baseline(self):
        self.collecting = False

    # ---- wejście danych ----
    def push(self, eeg: np.ndarray, acc: np.ndarray | None = None) -> list[dict]:
        out = []
        self.raw_buf = np.concatenate([self.raw_buf, eeg], axis=1)[:, -self.win:]
        if self.mode == "eeg":
            self.flt_buf = np.concatenate([self.flt_buf, self.filt(eeg)], axis=1)[:, -self.win:]
        if acc is not None and self.has_accel:
            self.acc_buf = np.concatenate([self.acc_buf, acc], axis=1)[:, -self.cfg.fs:]  # ostatnia 1 s
        self.since += eeg.shape[1]
        self.t += eeg.shape[1] / self.cfg.fs
        while self.since >= self.hop:
            self.since -= self.hop
            fr = self._impedance_frame() if self.mode == "impedance" else self._eeg_frame()
            if fr:
                out.append(fr)
        return out

    def _impedance_frame(self):
        if self.raw_buf.shape[1] < 1:
            return None
        raw = self.raw_buf[:, -1]
        z = impedance_kohm(raw, self.cfg)
        self.last_imp = [v if v is not None else float("inf") for v in z]
        lvl = lambda v: -1 if v is None else 0 if v < self.cfg.imp_good_kohm else 1 if v < self.cfg.imp_bad_kohm else 2
        return {"ev": "impedance", "t": round(self.t, 2), "raw": [round(float(v), 1) for v in raw],
                "kohm": [None if v is None else round(v, 1) for v in z], "level": [lvl(v) for v in z]}

    def _eeg_frame(self):
        if self.flt_buf.shape[1] < self.win:
            return None
        good = channel_flags(self.raw_buf, self.flt_buf, self.cfg)
        mot = motion_level(self.acc_buf) if self.has_accel and self.acc_buf.shape[1] >= self.cfg.fs // 2 else 0.0
        still = mot < self.cfg.motion_thr
        n_ok = int(good.sum())
        clean = still and self.window_ok(good)
        bp = band_powers(self.flt_buf, self.cfg.fs)
        feat = raw_features(bp, self.names, good) if n_ok else None
        fr = {"ev": "frame", "t": round(self.t, 2), "motion": round(mot, 4), "still": bool(still),
              "chOk": [bool(g) for g in good], "nOk": n_ok, "clean": bool(clean),
              "bands": {k: [round(float(x), 3) for x in v] for k, v in bp.items()}, "idx": None}
        if self.collecting and clean and feat:
            self.base_frames.append((feat, good.copy()))
            self.clean_s += self.cfg.hop_s
            fr["baseline"] = self._baseline_progress()
            if self.clean_s >= self.cfg.baseline_s:
                self.finish_baseline(fr)
        if clean and feat:
            self.recent.append(feat)
        elif not clean:
            self.recent.clear()
        if self.baseline and clean and self.recent:
            keys = set.intersection(*[set(f.keys()) for f in self.recent])
            smooth = {k: float(np.median([f[k] for f in self.recent])) for k in keys}
            if all(k in smooth for k in ("engage", "fatigue", "load", "relax")):
                fr["idx"] = indices(smooth, self.baseline)
                fr["idx"]["windowS"] = round(len(self.recent) * self.cfg.hop_s, 2)
        return fr

    def _baseline_progress(self):
        return {"cleanS": round(self.clean_s, 2), "needS": self.cfg.baseline_s}

    def finish_baseline(self, fr: dict | None = None):
        self.collecting = False
        frames = self.base_frames
        if not frames:
            res = {"ok": False, "reason": "brak czystych okien"}
        else:
            keys = set.intersection(*[set(f.keys()) for f, _ in frames])
            vals = {k: float(np.median([f[k] for f, _ in frames])) for k in keys}
            frac = np.mean([g for _, g in frames], axis=0)
            good_names = [nm for nm, fr_ in zip(self.names, frac) if fr_ >= 0.8]
            ok = len(good_names) >= self.min_good() and all(self.names[i] in good_names for i in self.essential)
            self.baseline = Baseline(vals, self.clean_s, good_names) if ok else None
            res = {"ok": ok, "goodChannels": good_names, "badChannels": [n for n in self.names if n not in good_names], "cleanS": round(self.clean_s, 1)}
            if not ok:
                res["reason"] = "za mało poprawnych kanałów"
        if fr is not None:
            fr["baselineDone"] = res
        return res
