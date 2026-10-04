// Trwały lokalny zapis danych kliniki: jeden plik JSON w katalogu użytkownika.
// Zapis atomowy (plik tymczasowy + rename), żeby awaria w trakcie zapisu nie zniszczyła danych.
const fs = require('fs');
const path = require('path');

function createStore(dir) {
  const file = path.join(dir, 'neurocue-data.json');
  let data = {};
  try { data = JSON.parse(fs.readFileSync(file, 'utf8')); } catch (e) { if (e.code !== 'ENOENT') console.error('store: nie można odczytać danych', e); }

  const flush = () => {
    fs.mkdirSync(dir, { recursive: true });
    const tmp = file + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(data, null, 2));
    fs.renameSync(tmp, file);
  };
  return {
    file,
    load: () => data,
    save(patch) { data = { ...data, ...patch }; flush(); },
  };
}
module.exports = { createStore };
