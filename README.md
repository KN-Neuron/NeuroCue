# NeuroCue Clinic

Desktopowa aplikacja neurofeedback dla neurorehabilitacji (prototyp). Terapeuta prowadzi sesję ćwiczeń z pacjentem,
a aplikacja na bieżąco pokazuje jakość sygnału EEG i prosty wskaźnik stanu pacjenta (**Stabilnie / Sprawdź pacjenta / Brak pomiaru**).
Działa jako Electron z mostem w Pythonie do czepka **BrainAccess** (MINI/MIDI/MAXI), a bez sprzętu na wbudowanym symulatorze.
Jest też wersja przeglądarkowa do pokazów (patrz [Demo online](#demo-online-przeglądarka)).

> **Uczciwie o statusie:** to prototyp do demonstracji. Wskaźnik jest sygnałem pomocniczym, **nie diagnozą** i nie wyrobem medycznym.
> Tryb z prawdziwym czepkiem BrainAccess wymaga SDK i weryfikacji na sprzęcie (`bridge/probe.py`).
> Moduł zgody pacjenta to szkic techniczny, nie porada prawna.

## Co potrafi

| Obszar | Opis |
|---|---|
| Pacjenci i plan dnia | lista pacjentów z diagnozą, zgodą, ostrzeżeniami i numerem sesji; zarządzanie pacjentami |
| Zgoda | bramka zgody: bez zgody sesja się nie zaczyna |
| Sprzęt | skan czepków, pomiar impedancji elektrod, podgląd kanałów, bateria |
| Kalibracja | ok. 90 s czystego sygnału jako baseline pacjenta; ruch lub słaby kontakt wstrzymują pomiar |
| Sesja | fazy ćwiczenia, wskaźnik w czasie rzeczywistym, markery zdarzeń, decyzje terapeuty po komunikacie "Sprawdź pacjenta" |
| Raporty | podpisanie i zapis sesji lokalnie, archiwum, eksport PDF (klinicznego i dla rodziny) oraz zanonimizowany CSV |
| Odzyskiwanie | szkic trwającej sesji przeżywa zamknięcie okna |
| Tryby źródła | **demo** (scenariusze w UI), **symulator** (most Pythona), **urządzenie** (prawdziwy czepek) |

## Szybki start

Wymagania: Node.js 20+, Python 3.10+.

```bash
npm install
python3 -m venv .venv && .venv/bin/pip install numpy scipy   # symulator czepka
npm start
```

Otworzy się okno aplikacji. Bez czepka wybierz tryb **demo** lub **symulator**.

Pełne `npm run setup:bridge` instaluje także pakiet `brainaccess` (SDK sprzętu). Potrzebny jest tylko z prawdziwym czepkiem
i na części systemów (np. macOS) się nie buduje. Do symulatora wystarczą `numpy` i `scipy`.

## Przykładowy przebieg sesji (demo)

1. Na liście wybierz pacjenta, np. *Basia Kowalska*, i sprawdź ostrzeżenia oraz zgodę.
2. Przejdź do sprzętu: wybierz źródło (demo, symulator albo czepek), poczekaj aż elektrody będą zielone.
3. Kalibracja: ok. 90 s spokoju. Ruch przerywa zbieranie i trzeba je powtórzyć.
4. Sesja: ćwiczenia, a w tle wskaźnik. Pasek **DEMO** u góry przełącza scenariusze, żeby pokazać wszystkie stany:

```text
Auto | Stabilnie | Sprawdź pacjenta | Brak danych | Rozłączenie czepka     (tryb demo)
Spokojnie | Ruch | Słaby kontakt C3 | Zmęczenie | Desynchronizacja mu     (tryb symulatora)
```

5. Po komunikacie **Sprawdź pacjenta** wybierz reakcję. Trafi do raportu.
6. Zakończ sesję, podpisz i zapisz, wyeksportuj PDF lub CSV.

## Kolory wskaźnika

| Stan | Kolor |
|---|---|
| Stabilnie | zielony `oklch(0.62 0.12 155)` |
| Sprawdź pacjenta / uwaga | bursztyn `oklch(0.8 0.14 85)` |
| Alarm | czerwony `oklch(0.58 0.17 28)` |
| Brak pomiaru | szary `#7f8b95` |
| Akcent interfejsu | turkus `oklch(0.5 0.1 195)` |

## Czepek BrainAccess

Układy elektrod w `bridge/sources.py`: `MINI` (8), `MIDI` (16), `MAXI` (32), `HALO` (4). REF = Fp1, BIAS = Fp2 (nie są kanałami EEG).
Kolejność wejść w MIDI/MAXI jest inna niż w MINI.

Pierwsza weryfikacja na sprzęcie, bez Electrona:

```bash
cd bridge
../.venv/bin/python probe.py                         # lista czepków w zasięgu
../.venv/bin/python probe.py "BA MAXI 001"           # impedancja 10 s, potem EEG 15 s ze statystykami
../.venv/bin/python probe.py "BA MAXI 001" --tap C3  # stukaj w C3, narzędzie pokaże który kanał zareagował
```

Elektrody suche mogą mieć znacznie więcej niż 20 kΩ. Wtedy podnieś progi w aplikacji.

## Testy

```bash
npm run test:bridge     # DSP (impedancja, baseline, ruch, zmęczenie) + most + MAXI 32 kanały
```

Oczekiwany koniec wyjścia: `WSZYSTKO OK`. Testy używają symulatora, więc nie potrzeba sprzętu ani SDK.

## Zmienne środowiskowe

| Zmienna | Znaczenie | Przykład |
|---|---|---|
| `NEUROCUE_PYTHON` | własny interpreter Pythona dla mostu | `NEUROCUE_PYTHON=/usr/bin/python3 npm start` |
| `NEUROCUE_SIM_SPEED` | przyspieszenie symulatora (do testów) | `NEUROCUE_SIM_SPEED=10 npm start` |

## Architektura

```text
 okno (HTML + React)  ←→  preload.js  ←→  main.js  ←→  eeg-bridge.js  ←→  bridge/bridge.py
 NeuroCue Clinic…dc.html   window.neurocue   IPC       proces Pythona     JSON-lines po stdin/stdout
                                              │                           ├─ dsp.py      impedancja, baseline, ruch, wskaźnik
                                              └─ store.js                 └─ sources.py  symulator i adapter BrainAccess
                                                 zapis danych: neurocue-data.json (atomowy)
```

- Protokół mostu: polecenia `scan`, `connect`, `impedance`, `stream`, `baseline`, `sim`, `disconnect`, `shutdown`;
  zdarzenia `ready`, `devices`, `status`, `impedance`, `frame`, `battery`, `disconnected`, `error`, `ack`.
- Dane kliniki leżą w jednym pliku JSON w katalogu użytkownika (`userData` Electrona).

Struktura repo:

```text
NeuroCue Clinic Prototype v2.dc.html   interfejs i logika aplikacji
main.js, preload.js, store.js          proces główny Electrona, most IPC, zapis danych
eeg-bridge.js                          uruchamianie i obsługa procesu Pythona
bridge/                                DSP, symulator, adapter czepka, testy, probe.py
web/, deploy/                          wersja przeglądarkowa i pliki wdrożeniowe
README.eeg-starter.md                  opis osobnego szkieletu badawczego (src/, scripts/, dashboard/)
```

## Demo online (przeglądarka)

Serwer `web/server.js` (Node + WebSocket) wystawia tę samą aplikację w przeglądarce. Każde połączenie dostaje własny symulator czepka,
a dane trzymane są w `localStorage`, więc każdy użytkownik widzi tylko swoje.

```bash
npm install --no-save ws
node web/server.js            # http://localhost:3000   (PORT=... zmienia port)
```

Różnice względem desktopa: CSV to pobranie pliku, PDF idzie przez okno drukowania ("Zapisz jako PDF"), brak prawdziwego czepka.
Wdrożenie na VPS (Docker lub systemd + nginx): [deploy/DEPLOY.md](deploy/DEPLOY.md), gotowy zip budujesz poleceniem
`bash deploy/build-zip.sh`.

## Pakowanie aplikacji desktopowej

```bash
npm run dist     # electron-builder
```

## Szkielet badawczy (EEG discomfort starter)

Osobny zestaw skryptów w Pythonie (`src/`, `scripts/`, `dashboard/`, `tests/`) do offline'owej walidacji wskaźnika dyskomfortu
na danych syntetycznych i EEGMAT. Opis: [README.eeg-starter.md](README.eeg-starter.md).
