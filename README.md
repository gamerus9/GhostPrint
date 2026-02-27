# GhostPrint — WiFi Print Manager for Flying Bear & Marlin Printers

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-lightgrey)]()
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**GhostPrint** is a desktop application for sending G-code files and monitoring 3D printers over WiFi.
Designed for **Flying Bear Ghost 5 / Reborn** and any **Marlin-based printer** with **SHUI firmware** (MKS WiFi ESP8266 module).

![GhostPrint Screenshot](docs/screenshot.png)

---

## Compatible Printers & Firmware

**Tested with:**
- Flying Bear Ghost 5
- Flying Bear Ghost 5 Reborn / Reborn 3.0

**Should work with** (not tested due to lack of hardware):
- Any printer with **MKS Robin Nano** board + **MKS WiFi ESP8266** module running **SHUI firmware**
- Any Marlin-based printer with SHUI firmware on the WiFi module (TCP port 8080 + HTTP `/upload`)

**Compatible slicers** (any slicer that exports `.gcode`):
- OrcaSlicer
- PrusaSlicer
- UltiMaker Cura
- Simplify3D
- Any other slicer

> ⚠️ **Not compatible** with Klipper/Moonraker, Duet, or OctoPrint — those use different APIs.

---

## Features

- **One-click WiFi upload** — send `.gcode` directly to your printer; no SD card needed
- **Real-time print progress** — live progress bar with percentage and estimated time remaining
- **Temperature monitoring** — hotend and bed temperatures updated every 15 seconds
- **Print controls** — pause, resume, and stop print jobs from the desktop
- **G-code terminal** — send any Marlin command (M105, G28, M114…) and see the response
- **Project browser** — search and sort files by date, name, or size
- **Thumbnail preview** — displays embedded PNG previews from OrcaSlicer / PrusaSlicer
- **Drag & drop** — drop `.gcode` files directly into the window
- **Post-print cooling** — optionally inserts a cooling pause before `M84`
- **Send history** — logs every upload with timestamp and result
- **Dark UI** — easy on the eyes during long print sessions

---

## Installation

### Requirements
- Python 3.11+
- [`uv`](https://github.com/astral-sh/uv) (recommended) or pip
- Printer connected to the same local network

### Quick Start

```bash
git clone https://github.com/gamerus9/GhostPrint
cd GhostPrint
./run.sh
```

Or run directly:

```bash
uv run app.py
```

Or with pip:

```bash
pip install Pillow requests tkinterdnd2
python app.py
```

---

## Configuration

On first launch, `settings.json` is created next to `app.py`.
Edit it manually or via the ⚙ settings dialog:

| Parameter | Default | Description |
|---|---|---|
| `printer_ip` | `192.168.1.213` | Printer IP address |
| `upload_speed_kbs` | `80` | Used for upload time estimation |
| `default_cooling` | `0` | Default cooling pause in seconds |
| `projects_dir` | `projects` | Folder scanned for `.gcode` files |

---

## How It Works

SHUI firmware on the ESP8266 WiFi module exposes two interfaces:

| Interface | Protocol | Used for |
|---|---|---|
| TCP port 8080 | Plain-text Marlin | Status polling (M105 temps, M27 progress), terminal commands |
| HTTP `/upload` | multipart/form-data | G-code file transfer |

GhostPrint polls the printer every 15 seconds over TCP and uploads G-code over HTTP.
The app never modifies your G-code — it only optionally appends a cooling block before `M84`.

---

## Platform Notes

| Platform | Status |
|---|---|
| macOS | ✅ Full support |
| Windows | ✅ Core features work |
| Linux | ✅ Core features work |

"Open in Finder" and "Open in OrcaSlicer" context menu items use macOS `open` command and won't work on Windows/Linux.

---

## Development

```bash
# Run tests
uv run --with pytest --no-project pytest tests/ -v
```

---

## Keywords

Flying Bear Ghost 5 WiFi · Flying Bear Reborn · MKS WiFi manager · SHUI firmware ·
Marlin WiFi print manager · gcode sender WiFi · MKS Robin Nano · ESP8266 3D printer ·
Flying Bear print software · WiFi 3D printing desktop app

---

## На русском

**GhostPrint** — десктопное приложение для отправки G-code файлов и мониторинга 3D-принтера по WiFi.

Разработано и проверено на **Flying Bear Ghost 5 / Reborn**. Должно работать на любом принтере с модулем **MKS WiFi (ESP8266)** на прошивке **SHUI** — но из-за отсутствия другого железа не проверялось.

**Возможности:** отправка файлов по WiFi · прогресс-бар печати · мониторинг температур · управление печатью (пауза/стоп) · G-code терминал · браузер проектов · превью миниатюр · drag & drop · история отправок

**Запуск:**
```bash
git clone https://github.com/gamerus9/GhostPrint
cd GhostPrint
uv run app.py
```

---

## License

MIT © 2026
