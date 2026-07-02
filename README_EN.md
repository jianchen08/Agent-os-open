# Lingxi AgentOS

> **An Evolvable Agent Operating System** — A highly configurable, self-evolving AI Agent platform

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688.svg)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-19-61dafb.svg)](https://react.dev)
[![MCP](https://img.shields.io/badge/MCP-Compatible-purple.svg)](https://modelcontextprotocol.io)
[![Gitee Mirror](https://img.shields.io/badge/Gitee-Mirror-red.svg)](https://gitee.com)
[![GitHub Primary](https://img.shields.io/badge/GitHub-Primary-black.svg)](https://github.com)

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

> **Note**: `pyproject.toml` currently declares only 9 core dependencies (pyyaml / rich / aiohttp / watchdog / litellm / pydantic / jsonschema / simpleeval / python-lsp-server). FastAPI and Redis are imported in 25+ files but NOT declared in `pyproject.toml`. Install them manually (`pip install fastapi>=0.110 redis>=5.0`) or use the Docker image (recommended).

---

## ✨ Key Highlights

### 1. Freedom — You Define, Lingxi Executes
Almost every behavior can be customized via YAML/config files without changing code. Agent identity, prompts, toolset, model selection, hard/soft constraints, I/O schemas — all configurable.

### 2. Refined Tool Engineering
All tools follow a unified interface contract (`name` / `when_to_use` / `when_not_to_use` / `input_schema` / `examples` / `caveats`), supporting ABORT / SKIP / FALLBACK / RETRY error strategies. **Currently 41 built-in tools** (including MCP external tool integration).

### 3. Intelligent Conversation — Not Just Chatting, but "Thinking Dialog"
Streaming response + real-time thinking display + proactive clarification + approval interaction + Schema forms + voting panels + media timelines + thinking mode toggle.

### 4. Frontend Excellence — Beautiful, Usable, Customizable
7 preset themes (Deep Space Command Center, Dark, Light, Ocean Breeze, etc.), full configuration visualization, YAML-to-form auto-mapping.

### 5. Container Tasks — Engine for Complex Long-term Projects
For multi-stage tasks with deliverables ("develop an App", "write a novel", "make a game"), container tasks provide a complete solution planning → phase execution → human review → final acceptance loop.

### 6. Trigger System — Unattended Operation
Scheduled triggers (Cron), event triggers, interval triggers let Lingxi run itself.

---

## 🚀 Quick Start

### Prerequisites

- Python 3.10 (project `pyproject.toml` requires `requires-python = ">=3.10"`)
- Node.js 18+ (for frontend dev, Vite 8 required)
- Redis 7+ (token revocation + event bus)
- Docker & Docker Compose (recommended)

### Option 1: Docker (Recommended)

```bash
# Clone the repository
git clone https://github.com/lingxi-agentos/lingxi-agentos.git
# Or Gitee mirror
# git clone https://gitee.com/lingxi-agentos/lingxi-agentos.git

cd lingxi-agentos

# Copy environment template
cp .env.example .env
# Edit .env and fill in your LLM API keys (see config/models/llm.yaml)

# Start
docker compose up -d

# Access Web UI
open http://localhost:8000
```

### Option 2: Local Development

```bash
# Backend
cd src
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
# Supplement runtime deps not declared in pyproject.toml
pip install fastapi>=0.110 redis>=5.0

# Start Redis (if not running)
redis-server

# Start backend
python run.py

# Frontend (another terminal)
cd frontend
npm install
npm run dev
```

---

## 📖 Documentation

| Document | Description |
|----------|-------------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System architecture details |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Version roadmap |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contribution guide |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | Code of conduct |
| [CHANGELOG.md](CHANGELOG.md) | Changelog |
| [gitee_sync_guide.md](gitee_sync_guide.md) | GitHub/Gitee dual-mirror sync guide |

---

## 🌍 Mirrors

For better access from China, this project is also maintained on:

- **GitHub** (Primary): `https://github.com/lingxi-agentos/lingxi-agentos`
- **Gitee** (Mirror): `https://gitee.com/lingxi-agentos/lingxi-agentos`

See [gitee_sync_guide.md](gitee_sync_guide.md) for sync instructions.

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