# 灵汐 AgentOS

> **可进化的智能体操作系统** —— 高度可配置化、自进化闭环的 AI Agent 平台

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688.svg)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-19-61dafb.svg)](https://react.dev)
[![MCP](https://img.shields.io/badge/MCP-Compatible-purple.svg)](https://modelcontextprotocol.io)
[![Gitee](https://img.shields.io/badge/Gitee-镜像-red.svg)](https://gitee.com)
[![GitHub](https://img.shields.io/badge/GitHub-主仓库-black.svg)](https://github.com)

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

### 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | Python 3.10 / FastAPI 0.110+ / aiohttp / Redis / Pydantic / LiteLLM |
| 前端 | React 19 / TypeScript / Vite / Zustand / Antd / @lobehub/ui / Tailwind CSS |
| AI | OpenAI / Anthropic / DeepSeek / 智谱 GLM / Ollama / 多模型路由 |
| 协议 | MCP（Model Context Protocol）|
| 部署 | Docker / Docker Compose |

> **注意**：`pyproject.toml` 当前仅声明 9 个核心依赖（pyyaml / rich / aiohttp / watchdog / litellm / pydantic / jsonschema / simpleeval / python-lsp-server），FastAPI 和 Redis 在 25+ 个文件中实际 import 但未在 `pyproject.toml` 声明。安装时需手动 `pip install fastapi>=0.110 redis>=5.0`，或使用 Docker 镜像（推荐）。

---

## ✨ 核心亮点

### 1. 自由度——你定义，灵汐执行
几乎所有行为都可以通过 YAML / 配置文件定制，不需要改代码。Agent 身份、提示词、工具集、模型选择、硬约束/软约束、输入输出 Schema 全部可配置。

### 2. 工具的精细化工程设计
所有工具遵循统一接口契约（`name` / `when_to_use` / `when_not_to_use` / `input_schema` / `examples` / `caveats`），支持 ABORT / SKIP / FALLBACK / RETRY 四种错误策略。**当前实现 41 个内置工具**（含 MCP 外部工具接入）。

### 3. 智能会话——不只聊天，更是"会思考的对话"

流式响应 + 思考态实时展示 + 主动澄清 + 审批交互 + Schema 表单 + 投票面板 + 媒体时间线 + 思考模式开关。

### 4. 前端亮点——好看、好用、好定制
7 套预设主题（含深空指挥台、深色、浅色、海洋微风等）、全量配置可视化、YAML 字段自动映射表单控件。

### 5. 容器任务——复杂长期项目的引擎
对于"开发一个 App""写一部网络小说""做一个游戏"这类多阶段、有交付物的大任务，容器任务提供完整的方案规划→阶段执行→人类审查→完成验收闭环。

### 6. 触发器系统——无人值守
定时触发器（Cron）、事件触发器、间隔触发器让灵汐自己跑起来。

---

## 🚀 快速开始

### 前置要求

- Python 3.10（项目 `pyproject.toml` 要求 `requires-python = ">=3.10"`）
- Node.js 18+（前端开发，Vite 8 要求）
- Redis 7+（token 撤销 + 事件总线）
- Docker & Docker Compose（推荐）

### 方式一：Docker 一键启动（推荐）

```bash
# 克隆仓库
git clone https://github.com/lingxi-agentos/lingxi-agentos.git
# 或 Gitee 镜像
# git clone https://gitee.com/lingxi-agentos/lingxi-agentos.git

cd lingxi-agentos

# 复制环境变量模板
cp .env.example .env
# 编辑 .env，填入你的 LLM API Key（参考 config/models/llm.yaml）

# 启动
docker compose up -d

# 访问 Web UI
open http://localhost:8000
```

### 方式二：本地开发模式

```bash
# 后端
cd src
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
# 补充 pyproject.toml 未声明的运行时依赖
pip install fastapi>=0.110 redis>=5.0

# 启动 Redis（如未运行）
redis-server

# 启动后端
python run.py

# 前端（另一个终端）
cd frontend
npm install
npm run dev
```

---

## 📖 文档导航

| 文档 | 说明 |
|------|------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 系统架构详解 |
| [docs/ROADMAP.md](docs/ROADMAP.md) | 版本路线图 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献指南 |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | 行为准则 |
| [CHANGELOG.md](CHANGELOG.md) | 变更日志 |
| [gitee_sync_guide.md](gitee_sync_guide.md) | GitHub/Gitee 双开同步指南 |

---

## 🌍 镜像仓库

为方便国内用户访问，本项目同时在以下平台维护：

- **GitHub**（主仓库）：`https://github.com/lingxi-agentos/lingxi-agentos`
- **Gitee**（镜像）：`https://gitee.com/lingxi-agentos/lingxi-agentos`

同步指南参见 [gitee_sync_guide.md](gitee_sync_guide.md)。

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
