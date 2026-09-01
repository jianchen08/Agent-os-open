# Lingxi AgentOS

> **An Evolvable Agent Operating System** — Everything is a plugin: a highly configurable, self-evolving AI Agent platform

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![CI](https://github.com/jianchen08/Agent-os-open/actions/workflows/ci.yml/badge.svg)](https://github.com/jianchen08/Agent-os-open/actions/workflows/ci.yml)
[![Rust](https://img.shields.io/badge/Rust-kernel-orange.svg)](https://www.rust-lang.org)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org)
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

- 🔌 **Everything is a Plugin** — The kernel is just an execution substrate (pipeline interpretation, capability registry, plugin loading, storage); LLM, memory, evaluation, approval, triggers, channels, themes — every capability is a plugin. Changing any business behavior = adding/modifying plugins or config, hot-effective across the chain, never touching the kernel.
- ⚙️ **Highly Configurable (a direct consequence)** — Agents are YAML data: persona, prompts, tool whitelist, constraints — all configurable; dynamic prompts inject as trailing messages without breaking prompt cache; changes apply without restart.
- 🔄 **Self-Evolving Loop** — Execute → review & sediment experience → generate new plugins and hot-load them: the system modifies itself by adding plugins, never touching the kernel.
- 🔀 **Plugin-based Pipeline** — The engine just interprets one pipeline YAML: plugins read/write a shared `state`, and a declarative routing DSL decides every transition (see [Key Highlight 1](#1-plugin-based-pipeline-architecture--everything-is-a-plugin-every-state-of-agent-execution-is-yours-to-control)).
- 🧠 **Memory System** — Carried by the hindsight memory plugin: automatic retention, on-demand recall, and document import; read/written by the LLM via the memory tool.

### Tech Stack

| Layer | Technology |
|-------|-----------|
| Kernel | Rust (axum / tokio / SQLite), single-process micro-kernel on :9100 |
| Plugins | Python 3.11 sidecars (MCP over stdio, isolated uv venvs) + Rust cdylib native track |
| Frontend | React 19 / TypeScript / Vite / Zustand / Antd / @lobehub/ui / Tailwind CSS |
| AI | OpenAI / Anthropic / DeepSeek / GLM / Ollama / multi-model routing (LiteLLM) |
| Protocol | MCP (Model Context Protocol) |
| Deployment | Docker / Docker Compose (on-demand Redis container) |

> **Build**: kernel via `cd kernel && cargo build --release --bin agentos-kernel`; every Python plugin directory carries its own `pyproject.toml` — create its isolated venv with `uv sync --project <plugin-dir>`; frontend via `npm install`. The `start_web_02.*` launch scripts do all of this in one step.

### 📊 Project Scale

- **Rust kernel**: ~95K lines (`kernel/`, ~20K of which are Rust tests)
- **Python plugins**: ~166K lines (`plugins/`, 97 `plugin.json` manifests)
- **Frontend code**: ~143K lines (`frontend/src/`)
- **Python tests**: ~124K lines (`tests/` + in-plugin tests)
- **Tool plugins**: 19 (41 built-in tool declarations, plus zero-code MCP external tool integration)
- **Client**: Web frontend (talking straight to the kernel)

---

## 🎬 Demo Video

<a href="https://www.bilibili.com/video/BV1d1NV62Efh">
  <img src="https://img.shields.io/badge/▶_Watch_Demo-Bilibili-FF69B4?style=for-the-badge&logo=bilibili&logoColor=white" alt="Lingxi AgentOS Demo Video" />
</a>

> Click the image above to watch a quick demo on Bilibili.

---

## ✨ Key Highlights

> Every highlight below stands on "everything is a plugin": apart from the engine and the frontend shell, each capability is carried by plugins (capability→plugin mapping in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).

### 1. Plugin-based Pipeline Architecture — Everything Is a Plugin, Every State of Agent Execution Is Yours to Control

**Everything is a plugin**: the kernel is just an execution substrate (pipeline interpretation, capability registry, plugin loading, storage) — LLM calls, memory, evaluation, approval, triggers, channels, themes and every other capability live in plugins. This is how "an evolvable Agent OS" materializes: the system modifies itself by generating new plugins that hot-load in seconds, without ever touching the kernel; plugin crashes are isolated from each other, the Python ecosystem plugs in directly, and third-party services integrate with zero code via MCP; the Web frontend talks straight to the kernel (HTTP / WebSocket) while plugins communicate over MCP.

The kernel engine does only one thing: interpret `config/pipelines/autonomous.yaml`, holding a shared `state` dict and scheduling loop bodies. **"What each round does" is entirely up to plugins** — wherever you want to intervene in Agent execution, whatever you want to rewrite, skip, or terminate, you implement it as a plugin. No kernel code changes needed.

```
User message → Web frontend → Rust kernel engine ┌─ init body (workspace/env resolution)
                                                  ├─ main body (while loop)
                                                  │    ├─ prepare: Input plugin chain (context build / tool surface / prompts / security guards…)
                                                  │    ├─ core: LLM call ↔ tool execution (switched by routing)
                                                  │    └─ post: Output plugin chain (stats / evaluation gate / stuck detection…) + routing arbitration
                                                  └─ exit body (workspace finalize, runs even on error)
                                                      ↑ all plugins read/write the same shared state ↑
```

**Division of labor**: plugins only read/write state (returning `state_updates` merged on the fly); "what the next round does" is decided by the pipeline YAML's routing DSL on state conditions — compiled at load time, zero parsing at runtime.

| `then` transfer target | Meaning |
|--------|---------|
| `loop` | Continue the loop (with `set:` to switch round type, e.g. `core_type=tool_execute` enters tool execution) |
| `end` | End the pipeline |
| step id / loop-body id | Jump between steps / transfer across loop bodies |

Exit-transfer conditions and side-effect writes are fully declared in the pipeline config (e.g. "tool calls pending → loop into tool execution; tools just executed → back to LLM; otherwise → end") — observable, intervenable, and rollback-able.

**Plugins-as-declarations**: a plugin = a directory + a `plugin.json` manifest (declaring tools / services / lifecycle hooks / HTTP endpoints), uniformly discovered, validated, and registered by the kernel. Choose your hosting track freely — Python sidecar (separate process, MCP over stdio, uv venv isolation) or Rust native (cdylib, zero-IPC in-process); third-party MCP services plug in with zero code. Plugin errors are handled uniformly by the engine: crashed sidecars auto-restart with one retry; failed tool results are fed back to the LLM for self-correction.

Dev docs: [docs/guides/plugin-protocol.md](docs/guides/plugin-protocol.md) (protocol authority) · [docs/guides/README.md](docs/guides/README.md) (step-by-step guides).

### 2. Hot Configuration — Evolve Without Downtime
Agent configs (YAML) hot-apply via mtime caching — change prompts or tool whitelists without restarting; plugin directories and manifest changes are hot-discovered and auto-(re)registered (file-watch + polling fallback, seconds-level); Python plugin processes are idle-collected, hot-reloaded on code change, and auto-revived after crashes. Pipeline configs hot-reload too — file changes are detected before each run and recompiled automatically; a broken config falls back to the last good one with a warning.

### 3. Freedom — You Define, Lingxi Executes
Almost every behavior is customizable via YAML: agent identity, prompts, tool whitelist, constraints, model selection — a direct consequence of everything-is-a-plugin + configuration; changes apply instantly (see Highlight 2).

### 4. Tool System — Contract-based Design, Out of the Box
Unified contract (`input_schema` + `output_schema` + `render` intent): the kernel validates results against `output_schema` (fail-closed), the frontend renders result cards by `render`, and `tool_ids` whitelists precisely control the LLM-visible surface. 19 built-in tool plugins with 41 tools (files, shell, code search, browser, network, memory, media generation, IDE integration), plus zero-code integration of any third-party MCP service.

### 5. Mandatory Evaluation System — Hard Constraint on Task Quality
Task submission must include acceptance criteria (evaluation metrics); after pipeline exit, a mandatory gate transitions the task into evaluation and reviews it against the metrics. Only when all metrics pass is the task marked complete; exhausted retries mean failure. Even if the Agent doesn't actively evaluate, the system forces the task into the evaluation gate (no completion without evaluation evidence) — quality is never skipped.

### 6. Approval Closed Loop — Quality Gate for Human-AI Collaboration
Human approval (choice / conversation dual modes; the human-interaction tool blocks awaiting the user's response) + feedback injection + task rework, forming a "generate → approve → feedback → iterate" quality-gate loop.

### 7. Container Tasks — Engine for Complex Long-term Projects
For multi-stage tasks with deliverables ("develop an App", "write a novel", "make a game"), container tasks provide a complete solution planning → phase execution → human review → final acceptance loop.

### 8. Workspace Isolation & Worktree Mechanism
Each task runs in its own **isolated workspace**: folder-level isolation by default, with Docker container isolation for higher-risk execution paths. In multi-task scenarios the **git worktree** mechanism forks a dedicated working directory per task, so concurrent tasks never collide on the filesystem and any side-effect can be reviewed or rolled back at the worktree boundary.

### 9. Trigger System — Unattended Operation
Scheduled triggers (Cron), event triggers, interval triggers let Lingxi run itself.

### 10. Intelligent Conversation — Not Just Chatting, but "Thinking Dialog"
Streaming response + real-time thinking display with mode toggle + proactive clarification + (document) approval interaction.

> **Planned (0.2.0+)**: Voting panels, media timelines and other interaction enhancements are not yet implemented. See [ROADMAP.md](ROADMAP.md).

### 11. Frontend Excellence — Beautiful, Usable, Customizable
7 compile-time preset themes (Dark / Light / Deep Space Command Center / Ocean Breeze / Pixel Candy / Moe Soft / High Contrast) + 3 dynamic JSON themes + plugin-delivered themes/skins (`contributes.themes`), full configuration visualization, YAML-to-form auto-mapping.

### 12. Skill Integration — Extend Domain Capabilities on Demand
Reusable skill packages (SKILL.md) under `skills/`: Agents lazy-load them via prompt guidance (file_read on demand, then follow the instructions) — new domain capabilities without code changes; skills, rules and prompts are three decoupled layers, added/removed independently.

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+ (launch scripts auto-detect 3.11/3.12/3.13)
- [uv](https://docs.astral.sh/uv/) (per-plugin venv management; the launch script runs `uv sync` automatically on first run)
- Rust stable (to build the kernel; install via `rustup`)
- Node.js 18+ (for frontend build, Vite required)
- Docker (WSL2 + docker-ce; docker compose provides the Redis container on demand, kernel and frontend both run on the host)

> **Architecture note**: 0.2 is a Rust kernel (`kernel/`) + Vite frontend talking straight to the
> kernel: `start_web_02.*` builds and starts `agentos-kernel` (:9100, host process) and the Vite
> dev server (:6390, proxying to the kernel); `docker compose` provides the Redis container
> on demand (the start script launches only the compose `redis` service, see docker/0.2/docker-compose.yml).

### Option 1: Windows One-Click (Recommended)

```bat
:: 1. Configure environment
copy .env.example .env
::    Edit .env and fill in your LLM API keys (see config/models/llm.yaml)

:: 2. (optional) Set up WSL2 + docker-ce for the Redis container; skip if already configured
install_native_docker.bat

:: 3. Start the project (builds the Rust kernel + starts kernel :9100 / frontend :6390 / Redis)
start_web_02.bat

:: Stop
stop_web_02.sh   (or stop by port, see the hint printed at the end of the script)
```

After startup:
- Web UI: http://localhost:6390
- Kernel API: http://localhost:9100

### Option 2: Linux / macOS One-Click

```bash
# 1. Configure environment
cp .env.example .env
# Edit .env and fill in your LLM API keys

# 2. Start (builds the Rust kernel + starts kernel :9100 / frontend :6390 / Redis)
#    NOTE: the 0.1 start_web.sh was removed; use start_web_02.sh / stop_web_02.sh for 0.2
chmod +x start_web_02.sh
./start_web_02.sh            # full start (build + kernel + frontend)
# or ./start_web_02.sh --no-build   # skip the build step

# Stop
./stop_web_02.sh
```

After startup:
- Web UI: http://localhost:6390
- Kernel API: http://localhost:9100

### Cross-device / Multi-Instance Configuration

The defaults work out of the box. Adjust as needed for the cases below.

**Workspace root**: task working files are stored under the path set by `workspace.root` in `config/isolation/isolation_config.yaml`. If your project lives elsewhere, or you prefer a different drive/partition, edit that file and set `root` to your actual path (absolute paths only, e.g. `/tmp/ai_workspaces` on Linux or `D:/workspaces` on Windows). In container isolation mode `root` **must** be absolute — a relative path breaks the Docker bind mount.

**Multi-instance (running two versions side by side for comparison)**: the compose project is auto-isolated by **directory name** (different directories = different container/network/volume names, no conflict). You do **not** need to set `COMPOSE_PROJECT_NAME`. The only thing that clashes is the **host port** (frontend 6390 / kernel 9100 / Redis 6690).

Host ports are parameterized (with defaults), so a single instance needs zero config. To run a second instance, just give it different ports:

```bat
:: Instance 1 (default ports 9100/6390): double-click start_web_02.bat

:: Instance 2 (different ports), in the other directory's shell:
set FRONTEND_HOST_PORT=5290
set REDIS_HOST_PORT=6691
set AGENTOS_KERNEL_PORT=9101
set AGENTOS_FRONTEND_PORT=6391
start_web_02.bat
```

The two instances don't interfere: different directories → different compose projects (container/network/volume isolation); different ports → no conflict. The startup banner shows the actual ports in use. Stop each by running `docker compose down` in its own directory (project-scoped, won't affect the other).

### Option 3: Manual Development

For developers who skip the scripts and need fine-grained control.

```bash
# 1. Build and start the Rust kernel (the 0.1 src/ + channels.websocket entries are gone)
cd kernel && cargo build --release --bin agentos-kernel
export AGENTOS_PLUGINS_DIR=../plugins/shared AGENTOS_CONFIG_ROOT=../config
./target/release/agentos-kernel    # kernel runs at http://localhost:9100

# 2. Start frontend (separate terminal)
cd frontend
npm install
npm run dev    # frontend dev server at http://localhost:6390 (proxies to kernel :9100)
```

> **About CLI mode**: the 0.1 standalone CLI entries (`cli_cn.bat` / `run.py` /
> `channels.cli.cli_main`) were cut by decision on 2026-08-20 (CLI pluginization inventory):
> the interactive REPL depended on the 0.1 in-process Python pipeline engine
> (`pipeline.engine` / `infrastructure.*`), which 0.2 replaced with the Rust kernel engine —
> no equivalent, no real consumers; interactive usage is unified in the Web frontend
> (`start_web_02.bat` / `start_web_02.sh`). `plugins/shared/system/channel_cli` remains a
> plugin shell loaded via sidecar (the only supported path), exposing the
> `cli.get_status` / `cli.sanitize_text` services plus `CLIOutputAdapter` (terminal text
> sanitizing; interface from the `channel_common` shared package).

---

## 📖 Documentation Navigation

| Document | Description |
|----------|-------------|
| [README.md](README.md) | Chinese README |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System architecture deep-dive |
| [docs/guides/README.md](docs/guides/README.md) | Dev guides index (plugin protocol authority / plugin dev / themes / Agent / pipeline config / troubleshooting) |
| [docs/vision.md](docs/vision.md) · [docs/guides/logging.md](docs/guides/logging.md) | Project vision · Logging system |
| [ROADMAP.md](ROADMAP.md) | Version roadmap |
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
