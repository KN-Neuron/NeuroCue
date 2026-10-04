const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('neurocue', {
  load: () => ipcRenderer.invoke('store:load'),
  save: (patch) => ipcRenderer.invoke('store:save', patch),
  // Synchroniczny zapis: używany tuż przed zamknięciem okna (szkic trwającej sesji).
  saveSync: (patch) => ipcRenderer.sendSync('store:saveSync', patch),
  exportPdf: (html, defaultName) => ipcRenderer.invoke('export:pdf', { html, defaultName }),
  eeg: {
    send: (cmd) => ipcRenderer.invoke('eeg:send', cmd),
    onEvent: (cb) => { const h = (_e, ev) => cb(ev); ipcRenderer.on('eeg:event', h); return () => ipcRenderer.removeListener('eeg:event', h); },
  },
  saveText: (content, defaultName) => ipcRenderer.invoke('export:text', { content, defaultName }),
});
