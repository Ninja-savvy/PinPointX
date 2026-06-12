# PinPointX 🎯

**PinPointX** is an AI-powered security analysis platform built for IoT devices and embedded hardware. Getting a clear picture of hardware security can feel like a massive puzzle - PinPointX puts those pieces together automatically.

Give it your firmware binaries, PCB images, or schematics and the AI engine maps out critical components, debug interfaces, known CVEs, and potential attack paths - no manual grind required.

> 🤝 **Built with:** Pallavi and Satyam Singh

---

## ✨ Features

- ✅ **Multi-Source Ingestion** - Firmware binaries, PCB images, schematics, and hardware artifacts all in one place
- ✅ **AI Hardware Analysis** - Automatically identifies critical chips, SoCs, flash memory, debug headers, and component architecture
- ✅ **Attack Path Mapping** - Discovers debug interfaces (UART, JTAG, SWD, SPI, I2C), entry points, and threat signals
- ✅ **CVE Cross-Referencing** - Extracts component and software details from firmware to flag known CVEs and EOL parts
- ✅ **Firmware Diff Analysis** - Compares two firmware versions side-by-side to detect new vulnerabilities introduced between releases
- ✅ **Tool Intelligence** - Upload or name any hardware pentesting tool and get a full usage guide and attack methodology
- ✅ **Actionable Reports** - Generates clear, downloadable HTML reports with prioritised findings and remediation steps
- ✅ **EMBA Integration** - Full EMBA firmware analysis pipeline built in (Linux only)

---

## 🗺️ Roadmap

-  **Visual PCB Overlay** - Highlights exact components, test points, and debug interfaces directly on your uploaded PCB image so you know precisely where to probe

---

## 🚀 Quick Start

### Prerequisites

- **Docker** and **Docker Compose v2** installed and running
- **One LLM/Vision provider** configured — Ollama (local) or HuggingFace API
- **Python 3** installed on the host (required for EMBA service on Linux)
- **EMBA** *(Linux only, optional)* - only needed for firmware analysis. Install guide: **https://github.com/e-m-b-a/emba** - see [EMBA Setup](#-emba-setup-linux--firmware-analysis-only) below.

---

### Linux / macOS

```bash
# 1. Clone the repository
git clone https://github.com/your-username/PinPointX.git
cd PinPointX

# 2. Copy and configure environment
cp .env.example .env
# Edit .env — set your LLM provider, EMBA paths, and model names

# 3. Make startup script executable
chmod +x startup.sh

# 4. Launch (starts EMBA service on host + app container)
sudo ./startup.sh
```

Open your browser: **http://localhost:5050**

> `sudo` is required because `emba_service.py` runs on the host and EMBA needs root to analyse firmware. The Docker container itself runs as a normal user.

---

### Windows

```bat
REM 1. Clone the repository
git clone https://github.com/your-username/PinPointX.git
cd PinPointX

REM 2. Copy and configure environment
copy .env.example .env
REM Edit .env — set your LLM provider and model names

REM 3. Launch
startup.bat
```

Open your browser: **http://localhost:5050**

> **Note:** EMBA firmware analysis requires Linux. All other features work fully on Windows.

---

## ⚙️ Configuration

All settings live in a single `.env` file. Copy `.env.example` and fill in the values:

```env
# ── Vision / LLM (required) ───────────────────────────────────────────────────
# Option A: Ollama (local, free)
OLLAMA_HOST=http://host.docker.internal:11434
OLLAMA_MODEL=your-vision-model           # e.g. qwen2-vl, llava

# Option B: HuggingFace 
HUGGINGFACE_API_URL=https://api-inference.huggingface.co
HUGGINGFACE_API_KEY=hf-...
HUGGINGFACE_MODEL=your-model-id

# ── Text / Reasoning model (required) ────────────────────────────────────────
OLLAMA_TEXT_MODEL=your-text-model        # e.g. mistral, gpt-oss
OLLAMA_TEXT_API_BASE=http://host.docker.internal:11434
OLLAMA_TIMEOUT_SEC=300
OLLAMA_TEXT_TIMEOUT_SEC=300

# ── EMBA paths (Linux only - optional) ──────────────────
EMBA_PATH=/opt/emba
EMBA_LOG_DIR=/path/to/pinpointx/emba_logs
EMBA_DIFF_LOG_DIR=/path/to/pinpointx/emba_diff_logs
EMBA_FIRMWARE_DIR=/path/to/pinpointx/uploads/firmware

# ── PCB pipeline tuning (optional) ──────────────────────────
PCB_USE_VLM_HINTS=1
PCB_PREFILTER_REJECT_THRESHOLD=0.40
PCB_PREFILTER_PASS_THRESHOLD=0.70
PCB_OCR_FALLBACK_THRESHOLD=0.60
PCB_CLIP_MODEL_ID=openai/clip-vit-base-patch32
PCB_OCR_LANGS=en
```

---



## 🧩 Analysis Modules

| Module | What it does |
|---|---|
| **Firmware Analysis** | EMBA-powered deep scan - CVEs, hardcoded creds, vulnerable components |
| **Firmware Diff** | Side-by-side version comparison - spots new vulnerabilities between releases |
| **PCB Image Analysis** | Multi-stage pipeline |
| **Schematic / Artifact Analysis** | Reads schematics and cross-references component data |
| **Tool Intelligence** | Name or photograph any hardware pentesting tool - get full profile and usage guide |
| **Component Lookup** | Part-number search against live CVE, exploit, and EOL chip intelligence feeds |

---


## 🔧 EMBA Setup

> **Optional prerequisite** — only needed for the Firmware Analysis and Firmware Diff features.

EMBA runs on the **host machine** (not inside the container) because it requires direct system access to analyse firmware. The `emba_service.py` agent bridges the container and EMBA.

**EMBA GitHub:** https://github.com/e-m-b-a/emba

```bash
# Clone EMBA to any directory on your machine
git clone https://github.com/e-m-b-a/emba.git /path/to/emba
cd /path/to/emba
sudo ./installer.sh -d
```

Then point `EMBA_PATH` in your `.env` to that directory:

```env
EMBA_PATH=/path/to/emba
```

> The default fallback is `/opt/emba` if `EMBA_PATH` is not set in `.env`, but you can install EMBA anywhere — just make sure `EMBA_PATH` matches where you cloned it.

---


## 🔒 Security & Privacy

- Your firmware binaries, PCB images, and schematics **never leave your machine** unless you configure a cloud LLM provider
- API keys are passed via your local `.env` file only - never baked into the Docker image

---


## 🤝 Contributing

Issues, PRs, and feedback welcome. If you find a bug or want to suggest a feature, open an issue on GitHub.

---

## ⚠️ Disclaimer

PinPointX is intended for authorised security research, penetration testing, and educational use only. Always obtain explicit permission before analysing any hardware or firmware you do not own. The authors accept no liability for misuse.

---
## 📹 Demo



https://github.com/user-attachments/assets/cd8217d0-ab93-462e-b368-d1f040f97f7b




