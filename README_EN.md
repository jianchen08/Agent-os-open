# Lingxi AgentOS

> **An Evolvable Agent Operating System** — A highly configurable, self-evolving AI Agent platform

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![CI](https://github.com/jianchen08/Agent-os-open/actions/workflows/ci.yml/badge.svg)](https://github.com/jianchen08/Agent-os-open/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688.svg)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-19-61dafb.svg)](https://react.dev)
[![MCP](https://img.shields.io/badge/MCP-Compatible-purple.svg)](https://modelcontextprotocol.io)
[![Gitee Mirror](https://img.shields.io/badge/Gitee-Mirror-red.svg)](https://gitee.com/jc27/Agent-os-open)
[![GitHub Primary](https://img.shields.io/badge/GitHub-Primary-black.svg)](https://github.com/jianchen08/Agent-os-open)

[中文](./README.md) | [English](#)

## 📑 Table of Contents

- [Overview](#-overview) · [Demo Video](#-demo-video) · [Key Highlights](#-key-highlights) · [Project Scale](#-project-scale)
- [Quick Start](#-quick-start)（[Windows](#option-1-windows-one-click-recommended) / [Linux·macOS](#option-2-linux--macos-one-click) / [Manual](#option-3-manual-development)）
- [Multi-Instance Config](#cross-device--multi-instance-configuration)
- [Documentation](#-documentation-navigation) · [Mirrors](#-mirror-repositories)
- [Contributing](#-contributing) · [Security](#-security-policy) · [License](#-license)

---

## 🌊 Overview

**Lingxi AgentOS** is not an isolated chatbot — it's a **freely customizable Agent Operating System**. It reorganizes LLMs, tools, memory, tasks, and configurations (originally fragmented) into **observable, intervenable, and rollback-able pipelines**, allowing Agents to converse with you like a human while also decomposing, dispatching, validating, and delivering complex tasks like an efficient team.

### Core Innovations

- 🔧 **Highly Configurable** — Agents are YAML data + loaders, not hardcoded classes. Dynamic prompt loading supports cache-hit-friendly patterns: volatile content (timestamps, session rules) is injected as a separate trailing message so the leading system prompt stays byte-stable and preserves prompt-cache hits. Injected fragments can be arranged by usage frequency at config time to maximize cache-hit rate. Change a prompt without restarting (`hot_swap` supports hot replacement, with rollback on failure).
- 🔄 **Self-Evolving Closed Loop** — Task execution → review (deep LLM review of finished pipelines, sedimenting experience reports into the knowledge base) → modify/add configuration and add plugins to enhance the system, forming a closed loop that gets smarter with use. A companion memory cleanup mechanism decides retention along three dimensions (review-status × age × capacity), ensuring reviews are sedimented before raw memories are reclaimed.
  > **Current status (the closed loop is semi-automatic and requires a human in the loop)**:
  > - **Experiences are NOT auto-injected** — after a review writes an experience into the knowledge base, **you must manually add a `type: retrieval` placeholder variable entry in the corresponding Agent's `static_vars` / `dynamic_vars`** (declaring which `tags` to retrieve by); only then will the experience be fetched and filled into the context at runtime. Without the placeholder, experiences only sit in the library and are never reused.
  > - **Improvement suggestions (action_items) are NOT auto-applied** — you need to tell the Agent the suggestion, let the Agent review and spell out "which configs / plugins / prompts to change", and **manually review and confirm before the change lands**, to avoid accidental edits.
  >
  > In other words, the entire loop currently requires a human-in-the-loop: whether experience is reused is decided by configuration, and whether the system is changed is decided by you.
- 🔌 **Plugin-based Pipeline Architecture** — The engine just holds a shared `state` and runs a `while not ended` loop; **you can freely write or configure plugins to control every state during Agent execution** (plugins are interceptors, `state` is the bus): a plugin can end the pipeline, suspend it, decide whether the next round calls the LLM or a tool, or read/write any state field — all without touching engine code. Combined with 4 routing signals (`next_llm` / `next_tool` / `end` / `wait`) and `ABORT` / `SKIP` / `RETRY` / `FALLBACK` error strategies, every decision is observable, intervenable, and rollback-able. See [Key Highlight 14](#14-plugin-based-pipeline-architecture-every-state-of-agent-execution-is-yours-to-control) below.
- 🧠 **Multi-layer Memory** — Episodic (EPISODE, compressed memory of conversations) + Semantic (SEMANTIC, sedimenting user preferences / project decisions / external knowledge base imports, etc.), retrieved on demand and injected as needed. Currently shipped: keyword retrieval, tag retrieval, and full injection. Richer retrieval modes (e.g. vector semantic retrieval) and injection modes (on-demand / summary injection) are planned for a later release.

### Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11 / FastAPI 0.110+ / aiohttp / Redis / Pydantic / LiteLLM |
| Frontend | React 19 / TypeScript / Vite / Zustand / Antd / @lobehub/ui / Tailwind CSS |
| AI | OpenAI / Anthropic / DeepSeek / GLM / Ollama / multi-model routing |
| Protocol | MCP (Model Context Protocol) |
| Deployment | Docker / Docker Compose |

> **Dependencies**: `pyproject.toml` declares 24 core runtime dependencies (including fastapi, redis, PyJWT, bcrypt, cryptography, httpx, sqlalchemy, etc.), mirrored in `requirements.txt` for the launch scripts. Run `pip install -e .` or `pip install -r requirements.txt` directly — no manual supplement needed.

### 📊 Project Scale

- **Python code**: ~308K lines (`src/` ~166K + `tests/` ~142K)
- **Frontend code**: ~96K lines (`frontend/src/`)
- **Built-in tools**: 41 (`src/tools/builtin/` with `tool.py`)
- **Live channels**: 2 (Web / CLI)
- **Modules**: 35 (subdirectories under `src/`)

---

## 🎬 Demo Video

<a href="https://www.bilibili.com/video/BV1d1NV62Efh">
  <img src="https://img.shields.io/badge/▶_Watch_Demo-Bilibili-FF69B4?style=for-the-badge&logo=bilibili&logoColor=white" alt="Lingxi AgentOS Demo Video" />
</a>

> Click the image above to watch a quick demo on Bilibili.

---

## ✨ Key Highlights

### 1. Freedom — You Define, Lingxi Executes
Almost every behavior can be customized via YAML/config files without changing code. Agent identity, prompts, toolset, model selection, hard/soft constraints, I/O schemas — all configurable.

### 2. Refined Tool Engineering
All tools follow a unified interface contract (`name` / `when_to_use` / `when_not_to_use` / `input_schema` / `examples` / `caveats`), supporting ABORT / SKIP / FALLBACK / RETRY error strategies. **Currently 41 built-in tools** (including MCP external tool integration).

### 3. Intelligent Conversation — Not Just Chatting, but "Thinking Dialog"
Streaming response + real-time thinking display + proactive clarification + approval interaction.

> **Planned (0.2.0+)**: Voting panels, media timelines, thinking-mode toggle and other interaction enhancements are not yet implemented in this version.

### 4. Frontend Excellence — Beautiful, Usable, Customizable
8 themes (5 built-in presets: Dark / Light / Deep Space Command Center / Ocean Breeze / High Contrast; plus 3 dynamic themes: Forest Mist / Lavender Field / Sunset Glow, discovered via a stateless backend manifest at `frontend/public/themes/*.json`), full configuration visualization, YAML-to-form auto-mapping.

### 5. Container Tasks — Engine for Complex Long-term Projects
For multi-stage tasks with deliverables ("develop an App", "write a novel", "make a game"), container tasks provide a complete solution planning → phase execution → human review → final acceptance loop.

### 6. Trigger System — Unattended Operation
Scheduled triggers (Cron), event triggers, interval triggers let Lingxi run itself.

### 7. Workspace Isolation & Worktree Mechanism
Each task runs in its own **isolated workspace**: folder-level isolation by default, with Docker container isolation for higher-risk execution paths. In multi-task scenarios the **git worktree** mechanism forks a dedicated working directory per task, so concurrent tasks never collide on the filesystem and any side-effect can be reviewed or rolled back at the worktree boundary.

### 8. Approval Closed Loop — Quality Gate for Human-AI Collaboration
Human approval (choice / conversation dual modes) + pipeline pause/resume + feedback injection + task rework, forming a "generate → approve → feedback → iterate" loop. Text approval is live; diff rendering components and version-comparison APIs are already in place.

### 9. Mandatory Evaluation System — Hard Constraint on Task Quality
Task submission must include acceptance criteria (evaluation metrics); after pipeline exit, a mandatory gate transitions the task into evaluation and reviews it against the metrics. Only when all metrics pass is the task marked complete; exhausted retries mean failure. Even if the Agent doesn't actively evaluate, the system forces a re-run — quality is never skipped.

### 10. 40+ Built-in Tools — Out-of-the-box Toolbox
Files, Shell, code search, browser, network, memory, media generation, IDE integration (41 actual tool.py implementations), including MCP external tool integration.

### 11. Dual-channel Access — One Kernel, Everywhere Reachable
Web and CLI share the same kernel; full MCP protocol support to integrate any MCP service.

### 12. Skill Integration — Extend Domain Capabilities on Demand
Loadable, reusable skill packages that can be injected into Agents on demand to gain new domain capabilities (document processing, PDF generation, etc.) without writing code.

### 13. Hot Swap — Evolve Without Downtime
`hot_swap` (snapshot → replace → health-check → rollback-on-failure) supports runtime hot replacement of plugins/Agents, while `hot_reload` watches config files and auto-reloads on change — debug and iterate without restarting the service.

### 14. Plugin-based Pipeline Architecture — Every State of Agent Execution Is Yours to Control

The engine does only one thing: hold a shared `state` dict and run a `while not state["ended"]` loop. **"What each round does" is entirely up to plugins** — wherever you want to intervene in Agent execution, whatever you want to rewrite, skip, or terminate, you implement it as a plugin. No engine code changes needed.

```
User message → Channel layer → Pipeline engine ┌─ Input plugins (preprocess, security, context…)
                                               ├─ Core plugin (LLM call / tool execution)
                                               └─ Output plugins (result shaping, routing…)
                                                   ↑ all plugins read/write the same shared state ↑
```

**Four ways a plugin controls state**:

| Control | What the plugin does | Effect |
|---------|----------------------|--------|
| Read/write fields | Returns `state_updates`, merged into `state` on the fly | Downstream plugins, routing table, and Core all see it |
| End / suspend | Sets `state["ended"]=True`, or a field that makes routing pick `wait` | Ends the pipeline now, or suspends until an external event (e.g. approval) then `wake()` resumes |
| Routing table picks plugins by state | Input route table re-evaluates `condition` every round | Different rounds of the same pipeline run different plugin sets — no hardcoded branching |
| Output signal picks next round | Output plugins emit a routing signal | Next round runs LLM, a tool, ends, or suspends |

**The 4 routing signals** clearly define "what the next round does":

| Signal | Meaning |
|--------|---------|
| `next_llm` | Next round calls the LLM |
| `next_tool` | Execute a tool |
| `end` | End the pipeline |
| `wait` | Suspend, wait for external input/approval |

**4 error strategies** let you declare what a plugin does on failure: `ABORT` (stop), `SKIP` (skip and continue), `RETRY` (retry), `FALLBACK` (use a fallback result) — security checks use `ABORT` (uncertain → must not continue), context building uses `FALLBACK` (degraded still works), stats plugins use `SKIP` (failure must not break the round).

**Config-driven onboarding**: write a Python class implementing `IInputPlugin` / `ICorePlugin` / `IOutputPlugin`, declare it via `name:` or `class:` in YAML, and the engine auto-discovers and instantiates it at startup. Adding a plugin touches no engine code; existing plugins support hot swap and rollback.

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+ (launch scripts auto-detect 3.11/3.12/3.13)
- Node.js 18+ (for frontend build, Vite required)
- Docker (WSL2 + docker-ce; frontend container + Redis container, backend runs on the host)

> **Architecture note**: `docker compose` only manages the frontend (static hosting) and Redis containers. The **backend FastAPI process runs on the host** (started via `python -m channels.websocket.app_factory`). The scripts below orchestrate all three parts.

### Option 1: Windows One-Click (Recommended)

```bat
:: 1. Configure environment
copy .env.example .env
::    Edit .env and fill in your LLM API keys (see config/models/llm.yaml)

:: 2. Configure Docker environment first (WSL2 + docker-ce, Docker Desktop no longer supported)
::    This deployment uses WSL2 + docker-ce only; Docker Desktop is not supported.
::    Run the script below if not yet configured; skip to step 3 if already set up.
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
- Web UI: http://localhost:5289
- Backend API: http://localhost:8988

### Cross-device / Multi-Instance Configuration

The defaults work out of the box. Adjust as needed for the cases below.

**Workspace root**: task working files are stored under the path set by `workspace.root` in `config/isolation/isolation_config.yaml`. If your project lives elsewhere, or you prefer a different drive/partition, edit that file and set `root` to your actual path (absolute paths only, e.g. `/tmp/ai_workspaces` on Linux or `D:/workspaces` on Windows). In container isolation mode `root` **must** be absolute — a relative path breaks the Docker bind mount.

**Multi-instance (running two versions side by side for comparison)**: the compose project is auto-isolated by **directory name** (different directories = different container/network/volume names, no conflict). You do **not** need to set `COMPOSE_PROJECT_NAME`. The only thing that clashes is the **host port** (frontend 5289 / Redis 6480 / backend 8988).

Host ports are parameterized (with defaults), so a single instance needs zero config. To run a second instance, just give it different ports:

```bat
:: Instance 1 (default ports): double-click start_web_cn.bat

:: Instance 2 (different ports), in the other directory's shell:
set FRONTEND_HOST_PORT=5290
set REDIS_HOST_PORT=6481
set BACKEND_PORT=8989
start_web_cn.bat
```

The two instances don't interfere: different directories → different compose projects (container/network/volume isolation); different ports → no conflict. The startup banner shows the actual ports in use. Stop each by running `docker compose down` in its own directory (project-scoped, won't affect the other).

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
# Frontend dev server runs at http://localhost:5289
```

> **About CLI mode**: besides Web mode, a command-line interactive session is available (no web services started):
> - `python run.py demo` (echo) / `python run.py real` (real LLM) — quick entry via `run.py`
> - `cli_cn.bat` (Windows) — clears `__pycache__` then launches the full CLI (`channels.cli.cli_main`), supports `--mode {normal,auto,plan}`, `--message`, etc.
> - `PYTHONPATH=src python -m channels.cli.cli_main` (cross-platform), or the registered command `agent-os` after install

---

## 📖 Documentation Navigation

| Document | Description |
|----------|-------------|
| [README.md](README.md) | Chinese README |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System architecture deep-dive |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contribution guide |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | Code of conduct |
| [CHANGELOG.md](CHANGELOG.md) | Changelog |
| [SECURITY.md](SECURITY.md) | Security policy & vulnerability reporting |
| [AUTHORS.md](AUTHORS.md) | Contributors list |
| [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) | Third-party dependency licenses |

---

## 🌍 Mirror Repositories

For users in mainland China, this project is also mirrored at:

- **GitHub** (primary): `https://github.com/jianchen08/Agent-os-open`
- **Gitee** (mirror): `https://gitee.com/jc27/Agent-os-open`

---

## 🤝 Contributing

Contributions of any form are welcome — Issues, PRs, docs, use-case sharing. See [CONTRIBUTING.md](CONTRIBUTING.md); please read the [Code of Conduct](CODE_OF_CONDUCT.md) before participating.

---

## 🔒 Security Policy

If you discover a security vulnerability, please do **not** open a public Issue. Report it privately following [SECURITY.md](SECURITY.md).

---

## 📄 License

This project is licensed under [Apache License 2.0](LICENSE).

---

## 🌟 Star History

If this project helps you, please star ⭐️ to support us!

---

> **"Lingxi" (灵汐) — from "spiritual energy like tides, endlessly renewed"** — We hope AI Agents can self-regulate and self-evolve like tides.