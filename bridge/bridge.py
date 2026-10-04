#!/usr/bin/env python3
"""Most NeuroCue ↔ czepek EEG. Protokół: JSON-lines po stdin/stdout (jedno polecenie/zdarzenie na linię).

Polecenia (stdin):  {"cmd":"scan"} {"cmd":"connect","name":"...","sim":bool,"cap":[...]} {"cmd":"impedance","on":bool}
                    {"cmd":"stream","on":bool} {"cmd":"baseline","action":"start|stop|clear"} {"cmd":"sim",...}
                    {"cmd":"disconnect"} {"cmd":"shutdown"}
Zdarzenia (stdout): ready, devices, status, impedance, frame, battery, disconnected, error, ack
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

import numpy as np

from dsp import Config, Engine, pick_bias
from sources import BrainAccessSource, SimSource, ba_close

# Natywna biblioteka BrainAccess pisze własne logi na stdout (np. „Configs(32): …”) i psułaby protokół.
# Protokół idzie więc osobnym duplikatem deskryptora 1, a zwykły stdout trafia na stderr.
_proto = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
out_lock = threading.Lock()


def friendly(e: Exception) -> str:
    """Typowe błędy SDK po polsku, z podpowiedzią co zrobić."""
    m = f"{type(e).__name__}: {e}"
    low = str(e).lower()
    if "bluetooth" in low and ("disabled" in low or "not enabled" in low):
        return "Bluetooth jest wyłączony lub brak adaptera. Włącz Bluetooth w systemie i spróbuj ponownie."
    if "no devices" in low or "not found" in low:
        return "Nie znaleziono czepka. Włącz go i zbliż do komputera."
    if isinstance(e, ModuleNotFoundError):
        return "Brak biblioteki BrainAccess w środowisku Pythona (npm run setup:bridge)."
    return m


def emit(**ev):
    with out_lock:
        _proto.write(json.dumps(ev, separators=(",", ":"), default=float) + "\n")
        _proto.flush()


class Bridge:
    def __init__(self, speed: float = 1.0):
        self.speed = speed
        self.src = None
        self.engine: Engine | None = None
        self.state = "idle"
        self.lock = threading.Lock()
        self.info: dict | None = None
        self._bat_stop = threading.Event()

    # ---- narzędzia ----
    def status(self, **extra):
        emit(ev="status", state=self.state, device=self.info, caps=self._caps(), **extra)

    def _caps(self):
        if not self.info:
            return None
        n = self.info["channels"]
        return {"erd": "C3" in n and "C4" in n, "accel": bool(self.info.get("accel")), "channels": len(n)}

    def on_chunk(self, eeg, acc):
        with self.lock:
            eng = self.engine
            frames = eng.push(eeg, acc) if eng else []
        for f in frames:
            emit(**f)

    # ---- polecenia ----
    def cmd_scan(self, m):
        devs = [{"name": "SYMULATOR", "mac": "sim", "sim": True, "model": "MINI"},
                {"name": "SYMULATOR MAXI", "mac": "sim", "sim": True, "model": "MAXI"}]
        err = None
        try:
            devs += BrainAccessSource.scan()
        except Exception as e:  # brak SDK / adaptera BLE
            err = friendly(e)
        emit(ev="devices", list=devs, scanError=err)

    def cmd_connect(self, m):
        self.cmd_disconnect({})
        cap = m.get("cap")
        if m.get("sim"):
            self.src = SimSource(cap=cap, speed=self.speed, model=m.get("model", "MINI"))
        else:
            self.src = BrainAccessSource(cap=cap)
            self.src.on_disconnect = lambda: (self._lost())
            self.src.on_battery = lambda lvl: emit(ev="battery", level=lvl)
        self.state = "connecting"
        self.status()
        self.info = self.src.connect(m.get("name", ""))
        cfg = Config(fs=self.info["fs"], mains_hz=float(m.get("mains", 50)))
        if not self.info.get("sim"):
            # elektrody suche BrainAccess: typowo 50–180 kΩ (złocone) i 100–200 kΩ (SoftPulse); progi z zapasem
            cfg.imp_good_kohm, cfg.imp_bad_kohm = 250.0, 500.0
        for k_json, k_cfg in (("impGood", "imp_good_kohm"), ("impBad", "imp_bad_kohm")):
            if m.get(k_json) is not None:
                setattr(cfg, k_cfg, float(m[k_json]))
        self.engine = Engine(self.info["channels"], cfg, has_accel=bool(self.info.get("accel")))
        self.state = "connected"
        self.status()
        if not self.info.get("sim"):
            self._bat_stop.clear()
            threading.Thread(target=self._battery_loop, daemon=True).start()

    def _battery_loop(self):
        while not self._bat_stop.wait(30):
            if self.src and hasattr(self.src, "battery"):
                lvl = self.src.battery()
                if lvl is not None:
                    emit(ev="battery", level=lvl)

    def _lost(self):
        self.state = "idle"
        emit(ev="disconnected")

    def _need(self):
        if not self.src or not self.engine:
            raise RuntimeError("Czepek nie jest połączony")

    def cmd_impedance(self, m):
        self._need()
        self.state = "switching"   # zmiana trybu na prawdziwym czepku trwa kilka sekund (reconnect)
        self.status()
        self.src.stop()
        with self.lock:
            self.engine.mode = "impedance"
            self.engine.reset_stream()
        if m.get("on", True):
            self.src.start("impedance", self.on_chunk)
            self.state = "impedance"
        else:
            self.state = "connected"
        self.status()

    def cmd_stream(self, m):
        self._need()
        self.state = "switching"
        self.status()
        self.src.stop()
        with self.lock:
            self.engine.mode = "eeg"
            self.engine.reset_stream()
        if m.get("on", True):
            self.src.start("eeg", self.on_chunk, bias=pick_bias(self.engine.names, self.engine.last_imp))
            self.state = "streaming"
        else:
            self.state = "connected"
        self.status()

    def cmd_config(self, m):
        """Zmiana progów impedancji w trakcie pracy: {"cmd":"config","impGood":20,"impBad":50}."""
        self._need()
        with self.lock:
            self.engine.set_thresholds(m.get("impGood"), m.get("impBad"))

    def cmd_baseline(self, m):
        self._need()
        a = m.get("action", "start")
        with self.lock:
            if a == "start":
                self.engine.baseline = None
                self.engine.start_baseline()
            elif a == "stop":
                self.engine.stop_baseline()
            elif a == "clear":
                self.engine.baseline = None
                self.engine.stop_baseline()

    def cmd_sim(self, m):
        if isinstance(self.src, SimSource):
            self.src.set_params(**{k: v for k, v in m.items() if k != "cmd"})

    def cmd_disconnect(self, m):
        self._bat_stop.set()
        if self.src:
            try:
                self.src.disconnect()
            except Exception as e:
                emit(ev="error", where="disconnect", message=str(e))
        self.src, self.engine, self.info, self.state = None, None, None, "idle"
        if m is not None and m.get("cmd") == "disconnect":
            self.status()

    def handle(self, m):
        cmd = m.get("cmd")
        fn = getattr(self, "cmd_" + str(cmd), None)
        if not fn:
            emit(ev="error", where=cmd, message="nieznane polecenie")
            return
        try:
            fn(m)
            emit(ev="ack", cmd=cmd, id=m.get("id"), ok=True)
        except Exception as e:
            self.state = "idle" if cmd == "connect" else self.state
            emit(ev="ack", cmd=cmd, id=m.get("id"), ok=False)
            emit(ev="error", where=cmd, message=friendly(e))
            if cmd == "connect":
                self.src, self.engine, self.info = None, None, None
                self.status()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", type=float, default=1.0, help="przyspieszenie symulatora (testy)")
    args = ap.parse_args()
    br = Bridge(speed=args.speed)
    emit(ev="ready", version=1)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            emit(ev="error", where="parse", message="niepoprawny JSON")
            continue
        if m.get("cmd") == "shutdown":
            break
        br.handle(m)
    br.cmd_disconnect(None)
    try:
        ba_close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
