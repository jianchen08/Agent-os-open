# Lingxi AgentOS

> **An Evolvable Agent Operating System** — A highly configurable, self-evolving AI Agent platform

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![CI](https://github.com/AI-agent-system/Agent-os/actions/workflows/ci.yml/badge.svg)](https://github.com/AI-agent-system/Agent-os/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org)
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

- 🔧 **Highly Configurable** — Agents are YAML data + loaders, not hardcoded classes. Dynamic prompt loading supports cache-hit-friendly patterns: volatile content (timestamps, session rules) is injected as a separate trailing message so the leading system prompt stays byte-stable and preserves prompt-cache hits. Injected fragments can be arranged by usage frequency at config time to maximize cache-hit rate. Change a prompt without restarting (`hot_swap` supports hot replacement, with rollback on failure).
- 🔄 **Self-Evolving Closed Loop** — Task execution → review (deep LLM review of finished pipelines, sedimenting experience reports into the knowledge base) → modify, enhance and refactor the system, forming a closed loop that gets smarter with use. A companion memory cleanup mechanism decides retention along three dimensions (review-status × age × capacity), ensuring reviews are sedimented before raw memories are reclaimed.
- 🔌 **Plugin-based Pipeline Architecture** — 4 routing signals (`next_llm` / `next_tool` / `end` / `wait`) + pause/resume + every decision observable as state; plugin-level errors via `ABORT` / `SKIP` / `RETRY` / `FALLBACK` strategies (currently default `RETRY`, extensible per tool)
- 🧠 **Multi-layer Memory** — Episodic (EPISODE, compressed memory of conversations) + Semantic (SEMANTIC, sedimenting user preferences / project decisions / external knowledge base imports, etc.), retrieved on demand and injected as needed. Currently shipped: keyword retrieval, tag retrieval, and full injection. Richer retrieval modes (e.g. vector semantic retrieval) and injection modes (on-demand / summary injection) are planned for a later release — see [ROADMAP.md](ROADMAP.md).

### Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11 / FastAPI 0.110+ / aiohttp / Redis / Pydantic / LiteLLM |
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
Streaming response + real-time thinking display + proactive clarification + approval interaction.

> **Planned (0.2.0+)**: Voting panels, media timelines, thinking-mode toggle and other interaction enhancements are not yet implemented in this version. See [ROADMAP.md](ROADMAP.md).

### 4. Frontend Excellence — Beautiful, Usable, Customizable
8 themes (5 built-in presets: Dark / Light / Deep Space Command Center / Ocean Breeze / High Contrast; plus 3 dynamic themes: Forest Mist / Lavender Field / Sunset Glow, discovered via a stateless backend manifest at `frontend/public/themes/*.json`), full configuration visualization, YAML-to-form auto-mapping.

### 5. Container Tasks — Engine for Complex Long-term Projects
For multi-stage tasks with deliverables ("develop an App", "write a novel", "make a game"), container tasks provide a complete solution planning → phase execution → human review → final acceptance loop.

### 6. Trigger System — Unattended Operation
Scheduled triggers (Cron), event triggers, interval triggers let Lingxi run itself.

### 7. Workspace Isolation & Worktree Mechanism
Each task runs in its own **isolated workspace**: folder-level isolation by default, with Docker container isolation for higher-risk execution paths. In multi-task scenarios the **git worktree** mechanism forks a dedicated working directory per task, so concurrent tasks never collide on the filesystem and any side-effect can be reviewed or rolled back at the worktree boundary.

### 8. Approval Closed Loop — Quality Gate for Human-AI Collaboration
Human approval (choice / conversation dual modes) + pipeline pause/resume + feedback injection + task rework, forming a "generate → approve → feedback → iterate" loop. Text approval is live; diff rendering components and version-comparison APIs are already in place (see [ROADMAP.md](ROADMAP.md)).

### 9. Mandatory Evaluation System — Hard Constraint on Task Quality
Task submission must include acceptance criteria (evaluation metrics); after pipeline exit, a mandatory gate transitions the task into evaluation and reviews it against the metrics. Only when all metrics pass is the task marked complete; exhausted retries mean failure. Even if the Agent doesn't actively evaluate, the system forces a re-run — quality is never skipped.

### 10. 40+ Built-in Tools — Out-of-the-box Toolbox
Files, Shell, code search, browser, network, memory, media generation, IDE integration (41 actual tool.py implementations), including MCP external tool integration.

### 11. Multi-channel Access — One Kernel, Everywhere Reachable
Web, CLI, DingTalk, Feishu, QQ, WeCom, HTTP API share the same kernel; full MCP protocol support to integrate any MCP service.

### 12. Skill Integration — Extend Domain Capabilities on Demand
Loadable, reusable skill packages that can be injected into Agents on demand to gain new domain capabilities (document processing, PDF generation, etc.) without writing code.

### 13. Hot Swap — Evolve Without Downtime
`hot_swap` (snapshot → replace → health-check → rollback-on-failure) supports runtime hot replacement of plugins/Agents, while `hot_reload` watches config files and auto-reloads on change — debug and iterate without restarting the service.

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+ (launch scripts auto-detect 3.11/3.12/3.13)
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

# 2. Start Redis (Docker, port aligned with .env)
docker run -d --name agent-os-redis -p 6480:6379 \
    redis:7-alpine redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru

# 3. Start backend (FastAPI + WebSocket)
PYTHONPATH=src python -m channels.websocket.app_factory
# Backend runs at http://localhost:8988

# 4. Start frontend (separate terminal)
cd frontend
npm install
npm run dev
# Frontend dev server runs at http://localhost:5188
```

> **About CLI mode**: `python run.py demo` (echo) or `python run.py real` (real LLM) starts a CLI session — it does NOT start the web services.

---

## 📖 Documentation Navigation

| Document | Description |
|----------|-------------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System architecture deep-dive |
| [ROADMAP.md](ROADMAP.md) | Version roadmap |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contribution guide |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | Code of conduct |
| [CHANGELOG.md](CHANGELOG.md) | Changelog |

---

## 🌍 Mirror Repositories

For users in mainland China, this project is also mirrored at:

- **GitHub** (primary): `https://github.com/AI-agent-system/Agent-os`
- **Gitee** (mirror): `https://gitee.com/agentos/agent-os`

---

## 🤝 Contributing

Contributions of any form are welcome — Issues, PRs, docs, use-case sharing. See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## 📄 License

This project is licensed under [Apache License 2.0](LICENSE).

---

## ✨ Star History

If this project helps you, please star ⭐️ to support us!

---

> **"Lingxi" (灵汐) — from "spiritual energy like tides, endlessly renewed"** — We hope AI Agents can self-regulate and self-evolve like tides.