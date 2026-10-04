"""Test protokołu mostu jako procesu (symulator, przyspieszenie x20): `python bridge/test_bridge.py`."""
import json
import subprocess
import sys
import threading
import time
from queue import Queue, Empty

p = subprocess.Popen([sys.executable, "bridge.py", "--speed", "20"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
q: Queue = Queue()
threading.Thread(target=lambda: [q.put(json.loads(l)) for l in p.stdout], daemon=True).start()


def send(**m):
    p.stdin.write(json.dumps(m) + "\n"); p.stdin.flush()


def wait(pred, timeout=20, label=""):
    end, seen = time.time() + timeout, []
    while time.time() < end:
        try:
            ev = q.get(timeout=0.5)
        except Empty:
            continue
        if ev["ev"] == "error":
            raise AssertionError(f"błąd mostu: {ev}")
        if pred(ev):
            return ev
        seen.append(ev["ev"])
    raise AssertionError(f"timeout: {label}; widziane: {sorted(set(seen))}")


wait(lambda e: e["ev"] == "ready", label="ready")
send(cmd="scan")
d = wait(lambda e: e["ev"] == "devices", label="devices")
assert d["list"][0]["sim"] and d["list"][0]["name"] == "SYMULATOR", d
print("ok   scan")

send(cmd="connect", name="SYMULATOR", sim=True)
st = wait(lambda e: e["ev"] == "status" and e["state"] == "connected", label="connected")
assert st["device"]["channels"] == ["F3", "F4", "C3", "C4", "P3", "P4", "O1", "O2"] and st["caps"]["erd"], st
print("ok   connect + capabilities")

send(cmd="sim", zfinal={"P4": 35.0})
send(cmd="impedance", on=True)
z = wait(lambda e: e["ev"] == "impedance" and e["t"] > 8 and e["level"][5] == 1, label="impedance")
assert z["level"].count(0) == 7, z
print("ok   impedance (P4 słaby, reszta dobra)")

send(cmd="stream", on=True)
send(cmd="baseline", action="start")
prog = wait(lambda e: e["ev"] == "frame" and e.get("baseline"), label="baseline progress")
done = wait(lambda e: e["ev"] == "frame" and e.get("baselineDone"), timeout=40, label="baseline done")["baselineDone"]
assert done["ok"] and len(done["goodChannels"]) == 8, done
print("ok   baseline 90 s czystego sygnału:", done["cleanS"], "s")

send(cmd="sim", fatigue=1.0)
f = wait(lambda e: e["ev"] == "frame" and e.get("idx") and e["idx"]["fatigue"] > 65, timeout=30, label="fatigue")
print("ok   wskaźnik zmęczenia reaguje:", round(f["idx"]["fatigue"]))

send(cmd="sim", motion=True)
m = wait(lambda e: e["ev"] == "frame" and not e["clean"], timeout=15, label="motion")
assert not m["still"]
print("ok   ruch odrzuca okno")

send(cmd="disconnect")
wait(lambda e: e["ev"] == "status" and e["state"] == "idle", label="idle")

# --- MAXI: 32 kanały, mapowanie wejść wg instrukcji ---
send(cmd="scan")
d = wait(lambda e: e["ev"] == "devices", label="devices2")
assert any(x["name"] == "SYMULATOR MAXI" for x in d["list"]), d
send(cmd="connect", name="SYMULATOR MAXI", sim=True, model="MAXI", impGood=20, impBad=50)
st = wait(lambda e: e["ev"] == "status" and e["state"] == "connected", label="maxi connected")
ch = st["device"]["channels"]
assert len(ch) == 32 and ch[13] == "C3" and ch[3] == "C4" and st["caps"]["erd"] and st["device"]["model"] == "MAXI", st
print("ok   MAXI: 32 kanały, C3=wejście 13, C4=wejście 3")
send(cmd="sim", zfinal={"CP5": 90.0})
send(cmd="impedance", on=True)
z = wait(lambda e: e["ev"] == "impedance" and e["t"] > 8 and e["level"][ch.index("CP5")] == 2, label="maxi impedance")
assert len(z["kohm"]) == 32 and z["level"].count(0) == 31, z["level"]
send(cmd="config", impGood=200, impBad=500)
wait(lambda e: e["ev"] == "impedance" and e["level"][ch.index("CP5")] == 0, label="thresholds applied")
print("ok   MAXI: impedancja 32 kanałów, zmiana progów w locie")
send(cmd="stream", on=True)
send(cmd="baseline", action="start")
done = wait(lambda e: e["ev"] == "frame" and e.get("baselineDone"), timeout=120, label="maxi baseline")["baselineDone"]
assert done["ok"] and len(done["goodChannels"]) == 32, done
print("ok   MAXI: baseline 90 s")
send(cmd="disconnect")
wait(lambda e: e["ev"] == "status" and e["state"] == "idle", label="idle2")
send(cmd="shutdown")
p.wait(timeout=10)
print("WSZYSTKO OK")
