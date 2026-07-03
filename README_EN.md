# Lingxi AgentOS

> **An Evolvable Agent Operating System** — A highly configurable, self-evolving AI Agent platform

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688.svg)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-19-61dafb.svg)](https://react.dev)
[![MCP](https://img.shields.io/badge/MCP-Compatible-purple.svg)](https://modelcontextprotocol.io)
[![Gitee Mirror](https://img.shields.io/badge/Gitee-Mirror-red.svg)](https://gitee.com/agentos/agent-os)
[![GitHub Primary](https://img.shields.io/badge/GitHub-Primary-black.svg)](https://github.com/AI-agent-system/Agent-os)

[中文](./README.md) | [English](#)

---

## 🌊 Overview

**Lingxi AgentOS** is not an isolated chatbot — it's a **freely customizable Agent Operating System**. It reorganizes LLMs, tools, memory, tasks, and configurations (originally fragmented) into **observable, intervenable, and rollback-able pipelines**, allowing Agents to converse with you like a human while also decomposing, dispatching, validating, and delivering complex tasks like an efficient team.

### Core Innovations

- 🔧 **Highly Configurable** — Agents are YAML data + loaders, not hardcoded classes. Change a prompt without restarting (hot_swap supported)
- 🔄 **Self-Evolving Closed Loop** — From requirement clarification → task dispatch → execution validation → evaluation feedback, every step forms a closed loop
- 🔌 **Plugin-based Pipeline Architecture** — 6 routing signals + pause/resume + cross-pipeline routing
- 🧠 **Multi-layer Memory** — Episodic (EPISODE) + Semantic (SEMANTIC), with 3 retrieval methods × 3 injection methods
- 🛠️ **40+ Built-in Tools** — Files, Shell, code search, browser, network, memory, media generation, IDE integration (41 actual tool.py implementations)
- 🌐 **Multi-channel Access** — Web, CLI, DingTalk, Feishu, QQ, WeCom, HTTP API share the same kernel
- 📐 **MCP Compatible** — Full support for Model Context Protocol

### Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.10 / FastAPI 0.110+ / aiohttp / Redis / Pydantic / LiteLLM |
| Frontend | React 19 / TypeScript / Vite / Zustand / Antd / @lobehub/ui / Tailwind CSS |
| AI | OpenAI / Anthropic / DeepSeek / GLM / Ollama / multi-model routing |
| Protocol | MCP (Model Context Protocol) |
| Deployment | Docker / Docker Compose |

> **Dependencies**: `pyproject.toml` declares 24 core runtime dependencies (including fastapi, redis, PyJWT, bcrypt, cryptography, httpx, sqlalchemy, etc.), mirrored in `requirements.txt` for the launch scripts. Run `pip install -e .` or `pip install -r requirements.txt` directly — no manual supplement needed.

---

## ✨ Key Highlights

### 1. Freedom — You Define, Lingxi Executes
Almost every behavior can be customized via YAML/config files without changing code. Agent identity, prompts, toolset, model selection, hard/soft constraints, I/O schemas — all configurable.

### 2. Refined Tool Engineering
All tools follow a unified interface contract (`name` / `when_to_use` / `when_not_to_use` / `input_schema` / `examples` / `caveats`), supporting ABORT / SKIP / FALLBACK / RETRY error strategies. **Currently 41 built-in tools** (including MCP external tool integration).

### 3. Intelligent Conversation — Not Just Chatting, but "Thinking Dialog"
Streaming response + real-time thinking display + proactive clarification + approval interaction + Schema forms.

> **Planned (0.2.0+)**: Voting panels, media timelines, thinking-mode toggle and other interaction enhancements are not yet implemented in this version. See [ROADMAP.md](ROADMAP.md).

### 4. Frontend Excellence — Beautiful, Usable, Customizable
8 themes (5 built-in presets: Dark / Light / Deep Space Command Center / Ocean Breeze / High Contrast; plus 3 dynamic themes: Forest Mist / Lavender Field / Sunset Glow, discovered via a stateless backend manifest at `frontend/public/themes/*.json`), full configuration visualization, YAML-to-form auto-mapping.

### 5. Container Tasks — Engine for Complex Long-term Projects
For multi-stage tasks with deliverables ("develop an App", "write a novel", "make a game"), container tasks provide a complete solution planning → phase execution → human review → final acceptance loop.

### 6. Trigger System — Unattended Operation
Scheduled triggers (Cron), event triggers, interval triggers let Lingxi run itself.

---

## 🚀 Quick Start

### Prerequisites

- Python 3.10+ (launch scripts auto-detect 3.11/3.12/3.13)
- Node.js 18+ (for frontend build, Vite required)
- Docker (frontend container + Redis container; backend runs on the host)

> **Architecture note**: `docker compose` only manages the frontend (static hosting) and Redis containers. The **backend FastAPI process runs on the host** (started via `python -m channels.websocket.app_factory`). The scripts below orchestrate all three parts.

### Option 1: Windows One-Click (Recommended)

```bat
:: 1. Configure environment
copy .env.example .env
::    Edit .env and fill in your LLM API keys (see config/models/llm.yaml)

:: 2. Configure Docker environment first (WSL2 + docker-ce, replaces Docker Desktop)
::    Skip if Docker Desktop is already installed — jump to step 3
install_native_docker.bat

:: 3. Start the project (installs deps + launches backend/frontend/Redis)
start_web_cn.bat

:: Stop: close the "Agent OS Backend" window, then
docker compose down
```

After startup:
- Web UI: http://localhost:5289
- Backend API: http://localhost:8988 (docs at /docs)

### Option 2: Linux / macOS One-Click

```bash
# 1. Configure environment
cp .env.example .env
# Edit .env and fill in your LLM API keys

# 2. One-click deploy (installs Docker + Python deps + builds images + starts + health check)
chmod +x install.sh
./install.sh            # full deploy (bootstrap + deploy)
# or ./install.sh --deploy   # Docker already installed, skip bootstrap

# 3. Start in dev mode (backend + frontend dev server + Redis)
./start_web.sh

# Stop
./stop_web.sh
```

After startup:
- Web UI: http://localhost:5188
- Backend API: http://localhost:8988

### Option 3: Manual Development

For developers who skip the scripts and need fine-grained control.

```bash
# 1. Install dependencies (either works)
pip install -e .              # via pyproject.toml (recommended)
pip install -r requirements.txt  # via requirements.txt

# 2. Start Redis (via Docker, port aligned with .env)
docker run -d --name agent-os-redis -p 6480:6379 \
    redis:7-alpine redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru

# 3. Start backend (FastAPI + WebSocket)
PYTHONPATH=src python -m channels.websocket.app_factory
# Backend runs at http://localhost:8988

# 4. Start frontend (another terminal)
cd frontend
npm install
npm run dev
# Frontend dev server runs at http://localhost:5188
```

> **About CLI mode**: `python run.py demo` (echo) or `python run.py real` (real LLM) starts a command-line interaction, not a web service.

---

## 📖 Documentation

| Document | Description |
|----------|-------------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System architecture details |
| [ROADMAP.md](ROADMAP.md) | Version roadmap |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contribution guide |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | Code of conduct |
| [CHANGELOG.md](CHANGELOG.md) | Changelog |

---

## 🌍 Mirrors

For better access from China, this project is also maintained on:

- **GitHub** (Primary): `https://github.com/AI-agent-system/Agent-os`
- **Gitee** (Mirror): `https://gitee.com/agentos/agent-os`

---

## 🤝 Contributing

All forms of contribution are welcome — submitting Issues, PRs, improving docs, sharing use cases. See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## 📄 License

This project is licensed under the [Apache License 2.0](LICENSE).

---

## 🌟 Star History

If this project helps you, please star it ⭐️ to support us!

---

> **Lingxi, named after "spirit energy flowing like tides"** — We hope AI Agents can possess self-regulating, self-evolving vitality like tides.

---

## 📊 Project Status (Based on Actual Code, 2026-06-23)

- **Python code**: ~175K LOC (including `src/` and `tests/`)
- **Frontend code**: ~92K LOC (`frontend/src/`)
- **Built-in tools**: 41 (with `tool.py` in `src/tools/builtin/`)
- **Real channels**: 6 (CLI / DingTalk / Feishu / QQ / WeCom / WebSocket)
- **Modules**: 33 (subdirectories under `src/`)
- **Test files**: 335 (under `tests/`)