# 灵汐 AgentOS

> **可进化的智能体操作系统** —— 高度可配置化、自进化闭环的 AI Agent 平台

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688.svg)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-19-61dafb.svg)](https://react.dev)
[![MCP](https://img.shields.io/badge/MCP-Compatible-purple.svg)](https://modelcontextprotocol.io)
[![Gitee](https://img.shields.io/badge/Gitee-镜像-red.svg)](https://gitee.com/agentos/agent-os)
[![GitHub](https://img.shields.io/badge/GitHub-主仓库-black.svg)](https://github.com/AI-agent-system/Agent-os)

[English](./README_EN.md) | [中文](#)

---

## 🌊 项目简介

**灵汐 AgentOS** 不是一个孤立的聊天机器人，而是一个**可自由定制的智能体操作系统**。它把 LLM、工具、记忆、任务、配置这些原本割裂的环节，重新组织成一条条**可观测、可干预、可回滚**的管道（Pipeline），让 Agent 既能像人一样与你交流，也能像一支高效团队一样把复杂任务拆解、派发、验证、交付。

### 核心创新

- 🔧 **高度可配置化** —— Agent 不是写死的代码，而是 YAML 数据 + 加载器。改一个提示词不用重启服务（`hot_swap` 支持热替换）
- 🔄 **自进化闭环** —— 从需求澄清 → 任务派发 → 执行验证 → 评估反馈，每个环节都形成闭环，系统越用越聪明
- 🔌 **插件化管道架构** —— 6 种路由信号 + 暂停/恢复机制 + 跨管道路由，把每一步决策都变成可观测的状态
- 🧠 **多层记忆系统** —— 情景记忆（EPISODE）+ 语义记忆（SEMANTIC），三种检索方式 × 三种注入方式灵活组合
- 🛠️ **40+ 内置工具** —— 文件、Shell、代码搜索、浏览器、网络、记忆、媒体生成、IDE 集成（实际 41 个 tool.py 实现）
- 🌐 **多通道接入** —— Web、CLI、钉钉、飞书、QQ、企微、HTTP API 共享同一套内核
- 📐 **MCP 协议兼容** —— 完整支持 Model Context Protocol，可接入任何 MCP 服务
- 🔁 **复盘系统** —— 情景记忆积累到阈值或到达间隔时自动触发，由 `review_agent` 调用 LLM 深度复盘管道（失败时降级到 `ReviewEngine`），产出可复用经验并清理过期记忆（实现于 `src/memory/maintenance/{review_engine,service,cleanup_engine}.py` + `src/tools/builtin/trigger_review/` + `config/agents/system/review_agent.yaml`）

### 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | Python 3.10 / FastAPI 0.110+ / aiohttp / Redis / Pydantic / LiteLLM |
| 前端 | React 19 / TypeScript / Vite / Zustand / Antd / @lobehub/ui / Tailwind CSS |
| AI | OpenAI / Anthropic / DeepSeek / 智谱 GLM / Ollama / 多模型路由 |
| 协议 | MCP（Model Context Protocol）|
| 部署 | Docker / Docker Compose |

> **依赖说明**：`pyproject.toml` 声明 24 个核心运行时依赖（含 fastapi、redis、PyJWT、bcrypt、cryptography、httpx、sqlalchemy 等），并通过 `requirements.txt` 镜像供启动脚本使用。直接 `pip install -e .` 或 `pip install -r requirements.txt` 即可，无需手动补装。

---

## ✨ 核心亮点

### 1. 自由度——你定义，灵汐执行
几乎所有行为都可以通过 YAML / 配置文件定制，不需要改代码。Agent 身份、提示词、工具集、模型选择、硬约束/软约束、输入输出 Schema 全部可配置。

### 2. 工具的精细化工程设计
所有工具遵循统一接口契约（`name` / `when_to_use` / `when_not_to_use` / `input_schema` / `examples` / `caveats`），支持 ABORT / SKIP / FALLBACK / RETRY 四种错误策略。**当前实现 41 个内置工具**（含 MCP 外部工具接入）。

### 3. 智能会话——不只聊天，更是"会思考的对话"

流式响应 + 思考态实时展示 + 主动澄清 + 审批交互 + Schema 表单。

> **规划中（0.2.0+）**：投票面板、媒体时间线、思考模式开关等交互增强功能尚未在当前版本实现，详见 [ROADMAP.md](ROADMAP.md)。

### 4. 前端亮点——好看、好用、好定制
7 套预设主题（含深空指挥台、深色、浅色、海洋微风等）、全量配置可视化、YAML 字段自动映射表单控件。

### 5. 容器任务——复杂长期项目的引擎
对于"开发一个 App""写一部网络小说""做一个游戏"这类多阶段、有交付物的大任务，容器任务提供完整的方案规划→阶段执行→人类审查→完成验收闭环。

### 6. 触发器系统——无人值守
定时触发器（Cron）、事件触发器、间隔触发器让灵汐自己跑起来。

---

## 🚀 快速开始

### 前置要求

- Python 3.10+（启动脚本自动探测 3.11/3.12/3.13）
- Node.js 18+（前端构建，Vite 要求）
- Docker（前端容器 + Redis 容器；后端运行在宿主机）

> **架构说明**：`docker compose` 只负责前端（静态托管）和 Redis 容器，**后端 FastAPI 进程运行在宿主机**（通过 `python -m channels.websocket.app_factory` 启动）。下方脚本会自动编排这三部分。

### 方式一：Windows 一键启动（推荐）

```bat
:: 1. 配置环境变量
copy .env.example .env
::    编辑 .env，填入 LLM API Key（参考 config/models/llm.yaml）

:: 2. 首次配置 Docker 环境（WSL2 + docker-ce，替代 Docker Desktop）
::    若已装 Docker Desktop 可跳过，直接执行第 3 步
install_native_docker.bat

:: 3. 启动项目（自动装依赖 + 启动后端/前端/Redis）
start_web_cn.bat

:: 停止：关闭弹出的 "Agent OS Backend" 窗口，再执行
docker compose down
```

启动后：
- Web UI：http://localhost:5289
- 后端 API：http://localhost:8988 （API 文档：/docs）

### 方式二：Linux / macOS 一键启动

```bash
# 1. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 LLM API Key

# 2. 一键部署（装 Docker + Python 依赖 + 构建镜像 + 启动 + 健康检查）
chmod +x install.sh
./install.sh            # 完整部署（bootstrap + deploy）
# 或 ./install.sh --deploy   # 已装好 Docker，跳过 bootstrap 直接部署

# 3. 开发模式启动（后端 + 前端 dev server + Redis）
./start_web.sh

# 停止
./stop_web.sh
```

启动后：
- Web UI：http://localhost:5188
- 后端 API：http://localhost:8988

### 方式三：手动开发模式

适合不使用脚本、需要精细控制的开发者。

```bash
# 1. 安装依赖（任选其一）
pip install -e .              # 走 pyproject.toml（推荐）
pip install -r requirements.txt  # 走 requirements.txt

# 2. 启动 Redis（Docker 方式，端口对齐 .env）
docker run -d --name agent-os-redis -p 6480:6379 \
    redis:7-alpine redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru

# 3. 启动后端（FastAPI + WebSocket）
PYTHONPATH=src python -m channels.websocket.app_factory
# 后端运行在 http://localhost:8988

# 4. 启动前端（另一个终端）
cd frontend
npm install
npm run dev
# 前端开发服务器运行在 http://localhost:5188
```

> **关于 CLI 模式**：`python run.py demo`（echo 回显）或 `python run.py real`（真实 LLM）启动的是命令行交互，不启动 Web 服务。

---

## 📖 文档导航

| 文档 | 说明 |
|------|------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 系统架构详解 |
| [ROADMAP.md](ROADMAP.md) | 版本路线图 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献指南 |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | 行为准则 |
| [CHANGELOG.md](CHANGELOG.md) | 变更日志 |

---

## 🌍 镜像仓库

为方便国内用户访问，本项目同时在以下平台维护：

- **GitHub**（主仓库）：`https://github.com/AI-agent-system/Agent-os`
- **Gitee**（镜像）：`https://gitee.com/agentos/agent-os`

---

## 🤝 贡献

欢迎任何形式的贡献——提交 Issue、PR、完善文档、分享使用案例。详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 📄 开源协议

本项目采用 [Apache License 2.0](LICENSE)。

---

## 🌟 Star History

如果这个项目对你有帮助，欢迎点亮 Star ⭐️ 支持我们！

---

> **灵汐，取自"灵气如潮汐般生生不息"** —— 我们希望 AI Agent 也能像潮汐一样，具备自我调节、自我进化的生命力。

---

## 📊 项目状态（基于实际代码，2026-06-23）

- **Python 代码**：约 17.5 万行（含 `src/` 和 `tests/`）
- **前端代码**：约 9.2 万行（`frontend/src/`）
- **内置工具**：41 个（`src/tools/builtin/` 下含 `tool.py` 实现）
- **真实通道**：6 个（CLI / 钉钉 / 飞书 / QQ / 企微 / WebSocket）
- **模块数**：33 个（`src/` 下子目录）
- **测试文件**：335 个（`tests/` 下）
