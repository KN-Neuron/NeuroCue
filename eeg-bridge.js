// Zarządza procesem Pythona (bridge/bridge.py) i tłumaczy jego JSON-lines na zdarzenia dla okna aplikacji.
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const readline = require('readline');

function pythonPath(root) {
  if (process.env.NEUROCUE_PYTHON) return process.env.NEUROCUE_PYTHON;
  const venv = process.platform === 'win32' ? path.join(root, '.venv', 'Scripts', 'python.exe') : path.join(root, '.venv', 'bin', 'python');
  return fs.existsSync(venv) ? venv : null;
}

class EegBridge {
  constructor(root, emit) { this.root = root; this.emit = emit; this.proc = null; }

  start() {
    if (this.proc) return true;
    const py = pythonPath(this.root);
    if (!py) {
      this.emit({ ev: 'bridge-missing', message: 'Brak środowiska Pythona dla czepka. Uruchom: npm run setup:bridge' });
      return false;
    }
    const args = [path.join(this.root, 'bridge', 'bridge.py')];
    if (process.env.NEUROCUE_SIM_SPEED) args.push('--speed', process.env.NEUROCUE_SIM_SPEED);
    this.proc = spawn(py, args, { cwd: path.join(this.root, 'bridge'), stdio: ['pipe', 'pipe', 'pipe'] });
    readline.createInterface({ input: this.proc.stdout }).on('line', (line) => {
      try { this.emit(JSON.parse(line)); } catch { console.error('bridge: niepoprawna linia', line.slice(0, 200)); }
    });
    this.proc.stderr.on('data', (d) => console.error('[bridge]', String(d).trim()));
    this.proc.on('error', (e) => { this.proc = null; this.emit({ ev: 'bridge-exit', message: e.message }); });
    this.proc.on('exit', (code) => { this.proc = null; this.emit({ ev: 'bridge-exit', code }); });
    return true;
  }

  send(cmd) {
    if (!this.start()) return false;
    this.proc.stdin.write(JSON.stringify(cmd) + '\n');
    return true;
  }

  stop() {
    if (!this.proc) return;
    try { this.proc.stdin.write(JSON.stringify({ cmd: 'shutdown' }) + '\n'); } catch { /* proces już zakończony */ }
    setTimeout(() => this.proc && this.proc.kill(), 1500);
  }
}

module.exports = { EegBridge };
