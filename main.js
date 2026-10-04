const { app, BrowserWindow, dialog, ipcMain, Menu, net, protocol, shell } = require('electron');
const fs = require('fs');
const { createStore } = require('./store');
const { EegBridge } = require('./eeg-bridge');
const path = require('path');
const { pathToFileURL } = require('url');

const root = __dirname;
const local = (p) => pathToFileURL(path.join(root, p)).href;

// Runtime zależności prototypu (CDN) podawane lokalnie, żeby apka działała offline.
const REDIRECTS = [
  ['https://unpkg.com/react@18.3.1/umd/react.production.min.js', 'node_modules/react/umd/react.production.min.js'],
  ['https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js', 'node_modules/react-dom/umd/react-dom.production.min.js'],
  ['https://unpkg.com/@babel/standalone@7.29.0/babel.min.js', 'node_modules/@babel/standalone/babel.min.js'],
  ['https://fonts.googleapis.com/css2', 'assets/fonts.css'],
];

function serveLocalCdn() {
  protocol.handle('https', async (request) => {
    const hit = REDIRECTS.find(([u]) => request.url.startsWith(u));
    if (hit) {
      const res = await net.fetch(local(hit[1]));
      const headers = new Headers(res.headers);
      headers.set('Access-Control-Allow-Origin', '*'); // wymagane przez SRI w <script integrity>
      if (hit[1].endsWith('.css')) headers.set('Content-Type', 'text/css');
      return new Response(res.body, { status: 200, headers });
    }
    const font = request.url.match(/^https:\/\/fonts\.gstatic\.com\/local\/((ibm-plex-(?:sans|mono))-[\w-]+\.woff2)$/);
    if (font) {
      const res = await net.fetch(local(`node_modules/@fontsource/${font[2]}/files/${font[1]}`));
      return new Response(res.body, { status: 200, headers: { 'Content-Type': 'font/woff2', 'Access-Control-Allow-Origin': '*' } });
    }
    if (/^https:\/\/(unpkg\.com|fonts\.gstatic\.com|fonts\.googleapis\.com)\//.test(request.url)) return new Response('', { status: 404 });
    return net.fetch(request, { bypassCustomProtocolHandlers: true });
  });
}

const safeName = (n) => String(n || 'raport').replace(/[\\/:*?"<>|]+/g, '-').slice(0, 120);

// Raport HTML → PDF (A4) w ukrytym oknie; użytkownik wybiera miejsce zapisu.
async function exportPdf(parent, html, defaultName) {
  const { canceled, filePath } = await dialog.showSaveDialog(parent, { defaultPath: safeName(defaultName) + '.pdf', filters: [{ name: 'PDF', extensions: ['pdf'] }] });
  if (canceled || !filePath) return { canceled: true };
  const win = new BrowserWindow({ show: false, webPreferences: { sandbox: true, contextIsolation: true } });
  try {
    await win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html));
    await win.webContents.executeJavaScript('document.fonts.ready.then(() => true)');
    const pdf = await win.webContents.printToPDF({ pageSize: 'A4', printBackground: true, margins: { top: 0.5, bottom: 0.5, left: 0.55, right: 0.55 } });
    fs.writeFileSync(filePath, pdf);
    return { ok: true, path: filePath };
  } finally { win.destroy(); }
}

async function exportText(parent, content, defaultName) {
  const { canceled, filePath } = await dialog.showSaveDialog(parent, { defaultPath: safeName(defaultName), filters: [{ name: 'CSV', extensions: ['csv'] }] });
  if (canceled || !filePath) return { canceled: true };
  fs.writeFileSync(filePath, '\ufeff' + content); // BOM, żeby Excel poprawnie czytał polskie znaki
  return { ok: true, path: filePath };
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1440, height: 900, minWidth: 900, minHeight: 600,
    title: 'NeuroCue Clinic', backgroundColor: '#cfd6da',
    webPreferences: { preload: path.join(root, 'preload.js'), contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  win.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: 'deny' }; });
  win.loadFile(path.join(root, 'NeuroCue Clinic Prototype v2.dc.html'));
  return win;
}

app.whenReady().then(() => {
  const store = createStore(app.getPath('userData'));
  ipcMain.handle('store:load', () => store.load());
  ipcMain.handle('store:save', (_e, patch) => { store.save(patch); return true; });
  ipcMain.on('store:saveSync', (e, patch) => { try { store.save(patch); e.returnValue = true; } catch (err) { console.error(err); e.returnValue = false; } });
  ipcMain.handle('export:pdf', (e, { html, defaultName }) => exportPdf(BrowserWindow.fromWebContents(e.sender), html, defaultName));
  ipcMain.handle('export:text', (e, { content, defaultName }) => exportText(BrowserWindow.fromWebContents(e.sender), content, defaultName));
  const eeg = new EegBridge(root, (ev) => BrowserWindow.getAllWindows().forEach((w) => !w.isDestroyed() && w.webContents.send('eeg:event', ev)));
  ipcMain.handle('eeg:send', (_e, cmd) => eeg.send(cmd));
  app.on('before-quit', () => eeg.stop());
  serveLocalCdn();
  Menu.setApplicationMenu(null); // prototyp rysuje własny pasek menu
  createWindow();
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
