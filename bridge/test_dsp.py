"""Testy DSP bez sprzętu: uruchom `python bridge/test_dsp.py`."""
import math
import numpy as np

from dsp import Config, Engine, band_powers, channel_flags, impedance_kohm, StreamFilter, pick_bias
from sources import SimSource, CAPS, MIDI_CAP, MAXI_CAP

cfg = Config()
fs = cfg.fs
t = np.arange(2 * fs) / fs


def run(name, fn):
    fn()
    print("ok  ", name)


def test_band_power_amplitude():
    x = 10.0 * np.sin(2 * math.pi * 10 * t)           # 10 µV, 10 Hz → moc A²/2 = 50 µV²
    bp = band_powers(x[None, :], fs)
    assert abs(bp["alpha"][0] - 50.0) < 5.0, bp
    assert bp["theta"][0] < 2.0 and bp["beta"][0] < 2.0


def test_impedance_formula():
    # czepek zwraca Vpp[µV] stałe na kanał; Z[kΩ] = Vpp / 7 nA:  140 → 20 kΩ; 0 = jeszcze nie policzone
    z = impedance_kohm(np.array([140.0, 0.0, -5.0, 700.0]), cfg)
    assert abs(z[0] - 20.0) < 1e-9 and z[1] is None and z[2] is None and abs(z[3] - 100.0) < 1e-9, z


def test_filter_removes_dc_and_mains():
    x = 5 * np.sin(2 * math.pi * 10 * t) + 500 + 30 * np.sin(2 * math.pi * 50 * t)
    f = StreamFilter(1, cfg)
    y = np.concatenate([f(x[None, :250]), f(x[None, 250:])], axis=1)[0, 250:]
    assert abs(y.mean()) < 1.0
    bp = band_powers(y[None, :], fs)
    assert abs(bp["alpha"][0] - 12.5) < 3.0, bp          # 5 µV → 12.5 µV²


def test_artifact_flags():
    rng = np.random.default_rng(0)
    good = rng.normal(0, 5, (3, 2 * fs))
    big = good.copy(); big[1] += 400 * np.sin(2 * math.pi * 1.5 * t)
    flat = good.copy(); flat[2] = 100.0
    f = StreamFilter(3, cfg); filt = f(big)
    flags = channel_flags(big, filt, cfg)
    assert flags[0] and not flags[1], flags
    f2 = StreamFilter(3, cfg); flags2 = channel_flags(flat, f2(flat), cfg)
    assert not flags2[2], flags2


def feed(sim, eng, seconds):
    frames = []
    for _ in range(int(seconds * 10)):
        e, a = sim._gen(25)
        frames += eng.push(e, a)
    return frames


def test_sim_clean_and_motion():
    sim = SimSource(); eng = Engine(sim.names, cfg, has_accel=True)
    fr = [f for f in feed(sim, eng, 6) if f["ev"] == "frame"]
    assert fr and all(f["clean"] for f in fr[-8:]), fr[-1]
    sim.set_params(motion=True)
    fr = [f for f in feed(sim, eng, 3) if f["ev"] == "frame"]
    assert not fr[-1]["still"] and not fr[-1]["clean"]


def test_bad_contact_detected():
    sim = SimSource(); eng = Engine(sim.names, cfg, has_accel=True)
    sim.set_params(bad=["P3"])                    # kanał nie-kluczowy: 7 z 8 → okno nadal czyste
    fr = [f for f in feed(sim, eng, 6) if f["ev"] == "frame"][-1]
    assert not fr["chOk"][sim.names.index("P3")] and fr["nOk"] == 7 and fr["clean"]
    sim.set_params(bad=["C3"])                    # C3 jest kluczowy (ERD ruchowe) → okno NIE jest czyste mimo 7 z 8
    fr = [f for f in feed(sim, eng, 4) if f["ev"] == "frame"][-1]
    assert fr["nOk"] == 7 and not fr["clean"], fr
    sim.set_params(bad=["C3", "C4", "P3"])
    fr = [f for f in feed(sim, eng, 4) if f["ev"] == "frame"][-1]
    assert fr["nOk"] <= 5 and not fr["clean"], fr["nOk"]


def test_impedance_sim_settles():
    sim = SimSource(); sim.mode = "impedance"; sim.set_params(zfinal={"P4": 35.0})
    eng = Engine(sim.names, cfg); eng.mode = "impedance"
    fr = [f for f in feed(sim, eng, 12) if f["ev"] == "impedance"][-1]
    p4 = sim.names.index("P4")
    assert fr["level"][p4] == 1 and abs(fr["kohm"][p4] - 35) < 4, fr
    assert all(l == 0 for i, l in enumerate(fr["level"]) if i != p4), fr


def test_baseline_and_indices():
    sim = SimSource(); eng = Engine(sim.names, Config(baseline_s=20.0), has_accel=True)
    eng.start_baseline()
    done = None
    for f in feed(sim, eng, 40):
        if f.get("baselineDone"):
            done = f["baselineDone"]
    assert done and done["ok"], done
    ref = [f for f in feed(sim, eng, 5) if f.get("idx")][-1]["idx"]
    for k in ("focus", "fatigue", "load", "relax"):
        assert 30 < ref[k] < 70, (k, ref)             # w warunkach baseline'u ≈ 50 (z naturalnym rozrzutem)
    assert abs(ref["erd"]["C3"]) < 30, ref
    sim.set_params(fatigue=1.0)
    fat = [f for f in feed(sim, eng, 8) if f.get("idx")][-1]["idx"]
    assert fat["fatigue"] > ref["fatigue"] + 15, (ref, fat)
    sim.set_params(fatigue=0.0, erd=1.0)
    erd = [f for f in feed(sim, eng, 8) if f.get("idx")][-1]["idx"]
    assert erd["erd"]["C3"] > 40 and erd["erd"]["C4"] > 40, erd


def test_baseline_fails_with_bad_channels():
    sim = SimSource(); eng = Engine(sim.names, Config(baseline_s=15.0), has_accel=True)
    sim.set_params(bad=["C3", "C4", "P3"])        # 5 z 8 < 6 wymaganych → okno nigdy nie jest czyste
    eng.start_baseline()
    feed(sim, eng, 30)
    assert eng.baseline is None and eng.clean_s == 0.0


def test_maxi_cap_mapping():
    # wg instrukcji BrainAccess „Electrodes & Cables”, tabele 1–3
    assert len(MAXI_CAP) == 32 and len(set(MAXI_CAP)) == 32 and len(MIDI_CAP) == 16 and MAXI_CAP[:16] == MIDI_CAP
    assert (MAXI_CAP[13], MAXI_CAP[3], MAXI_CAP[8], MAXI_CAP[24], MAXI_CAP[31]) == ("C3", "C4", "Fz", "AFz", "PO3")
    assert MIDI_CAP[0] == "P8" and MAXI_CAP[16] == "T8" and MAXI_CAP[22] == "AF4"
    assert CAPS["MINI"] == ["F3", "F4", "C3", "C4", "P3", "P4", "O1", "O2"]
    figure = set("AF3 AFz AF4 F7 F3 Fz F4 F8 FC5 FC1 FC2 FC6 T7 C3 Cz C4 T8 CP5 CP1 CP2 CP6 P7 P3 Pz P4 P8 PO3 POz PO4 O1 Oz O2".split())
    assert set(MAXI_CAP) == figure                 # te same 32 pozycje, co na rysunku z instrukcji czapki


def maxi():
    sim = SimSource(model="MAXI"); return sim, Engine(sim.names, cfg, has_accel=True)


def test_maxi_channel_quality_rule():
    sim, eng = maxi()
    assert eng.n == 32 and eng.min_good() == 24 and sorted(sim.names[i] for i in eng.essential) == ["C3", "C4"]
    sim.set_params(bad=["CP1", "CP2", "FC5", "FC6", "T7"])       # 27 z 32 ≥ 24, C3/C4 dobre → czyste
    fr = [f for f in feed(sim, eng, 6) if f["ev"] == "frame"][-1]
    assert fr["nOk"] == 27 and fr["clean"], fr["nOk"]
    sim.set_params(bad=["CP1", "CP2", "FC5", "FC6", "T7", "T8", "AF3", "AF4", "PO3"])   # 23 < 24 → nieczyste
    fr = [f for f in feed(sim, eng, 4) if f["ev"] == "frame"][-1]
    assert fr["nOk"] == 23 and not fr["clean"], fr["nOk"]
    sim.set_params(bad=["C4"])                                     # kluczowy C4 zły → nieczyste mimo 31 z 32
    fr = [f for f in feed(sim, eng, 4) if f["ev"] == "frame"][-1]
    assert fr["nOk"] == 31 and not fr["clean"]


def test_maxi_baseline_and_erd_on_correct_channels():
    sim, _ = maxi(); eng = Engine(sim.names, Config(baseline_s=20.0), has_accel=True)
    eng.start_baseline(); done = None
    for f in feed(sim, eng, 40):
        if f.get("baselineDone"):
            done = f["baselineDone"]
    assert done and done["ok"] and len(done["goodChannels"]) == 32, done
    sim.set_params(erd=1.0)
    idx = [f for f in feed(sim, eng, 8) if f.get("idx")][-1]["idx"]
    assert idx["erd"]["C3"] > 40 and idx["erd"]["C4"] > 40, idx    # ERD z kanałów C3/C4 (wejścia 13 i 3), nie z przypadkowych


def test_maxi_baseline_fails_when_motor_channel_bad():
    sim, _ = maxi(); eng = Engine(sim.names, Config(baseline_s=15.0), has_accel=True)
    sim.set_params(bad=["C3"]); eng.start_baseline(); feed(sim, eng, 30)
    assert eng.baseline is None and eng.clean_s == 0.0


def test_pick_bias():
    names = ["A", "B", "C", "D"]
    assert pick_bias(names, [50.0, 8.0, 30.0, 12.0]) == [1, 3]
    assert pick_bias(["F3", "Pz", "C3"], None) == [1] and pick_bias(["F3", "C3"], None) == [1]


def test_impedance_thresholds_runtime():
    sim = SimSource(); sim.mode = "impedance"; sim.set_params(zfinal={"P4": 120.0})
    eng = Engine(sim.names, cfg); eng.mode = "impedance"
    fr = [f for f in feed(sim, eng, 12) if f["ev"] == "impedance"][-1]
    assert fr["level"][sim.names.index("P4")] == 2                 # 120 kΩ > 50 kΩ
    eng.set_thresholds(good=200, bad=500)                          # elektrody suche: wyższe progi
    fr = [f for f in feed(sim, eng, 2) if f["ev"] == "impedance"][-1]
    assert fr["level"][sim.names.index("P4")] == 0, fr


for n, f in list(globals().items()):
    if n.startswith("test_"):
        run(n, f)
print("WSZYSTKO OK")
