# 灵汐 AgentOS

> **可进化的智能体操作系统** —— 高度可配置化、自进化闭环的 AI Agent 平台

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![CI](https://github.com/jianchen08/Agent-os-open/actions/workflows/ci.yml/badge.svg)](https://github.com/jianchen08/Agent-os-open/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688.svg)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-19-61dafb.svg)](https://react.dev)
[![MCP](https://img.shields.io/badge/MCP-Compatible-purple.svg)](https://modelcontextprotocol.io)
[![Gitee](https://img.shields.io/badge/Gitee-镜像-red.svg)](https://gitee.com/jc27/Agent-os-open)
[![GitHub](https://img.shields.io/badge/GitHub-主仓库-black.svg)](https://github.com/jianchen08/Agent-os-open)

[English](./README_EN.md) | [中文](#)

## 📑 目录

- [项目简介](#-项目简介) · [演示视频](#-演示视频) · [核心亮点](#-核心亮点) · [项目规模](#-项目规模)
- [快速开始](#-快速开始)（[Windows](#方式一windows-一键启动推荐) / [Linux·macOS](#方式二linux--macos-一键启动) / [手动开发](#方式三手动开发模式)）
- [多实例配置](#跨设备--多实例配置说明)
- [文档导航](#-文档导航) · [镜像仓库](#-镜像仓库)
- [贡献](#-贡献) · [安全策略](#-安全策略) · [开源协议](#-开源协议)

---

## 🌊 项目简介

**灵汐 AgentOS** 不是一个孤立的聊天机器人，而是一个**可自由定制的智能体操作系统**。它把 LLM、工具、记忆、任务、配置这些原本割裂的环节，重新组织成一条条**可观测、可干预、可回滚**的管道（Pipeline），让 Agent 既能像人一样与你交流，也能像一支高效团队一样把复杂任务拆解、派发、验证、交付。

### 核心创新

- 🔧 **高度可配置化** —— Agent 不是写死的代码，而是 YAML 数据 + 加载器。支持加载动态提示词（如当前时间/会话规则）并以独立尾部消息注入，系统提示词头部保持稳定，不破坏 prompt cache 命中；注入片段可在配置阶段按使用频率排布，以最大化缓存命中。改一个提示词不用重启服务（`hot_swap` 支持热替换，失败可回滚）。
- 🔄 **自进化闭环** —— 任务执行 → 复盘（对已结束管道做 LLM 深度复盘，产出经验报告沉淀到知识库）→ 通过修改/添加配置、添加插件对系统进行改造与增强，形成闭环，系统越用越聪明。配套记忆清理机制按「复盘状态 × 年龄 × 容量」三维决策，确保复盘产出被沉淀后再回收原始记忆。
  > **现状说明（自进化闭环当前为半自动，需人在回路）**：
  > - **经验（experience）不会自动注入**——复盘把经验写进知识库后，**需要你自己在对应 Agent 的 `static_vars` / `dynamic_vars` 里加一个 `type: retrieval` 的占位符变量项（声明按哪些 `tags` 检索），经验才会在运行时被检索填充进上下文**。不加占位符，经验只会沉淀在库里、不会被复用。
  > - **改进建议（action_item）不会自动落盘**——需要你把建议告诉 Agent，由 Agent 复盘并明确"要改哪些配置 / 插件 / 提示词"，**改动落地前请你手动批阅确认**，避免误改。
  >
  > 换句话说，整个闭环目前都需要人在回路（human-in-the-loop）把关：经验要不要复用由配置决定，系统要不要改由你确认。
- 🔌 **插件化管道架构** —— 引擎只维护一个共享 `state` 与 `while not ended` 循环，**你可以自由编写或配置插件，控制 Agent 执行过程中的每一个状态**（插件即拦截器、`state` 即总线）：插件能终止管道、挂起等待、决定下一轮跑 LLM 还是工具、读写并改写任意状态字段，且无需改动引擎代码；配合 4 种路由信号（`next_llm` / `next_tool` / `end` / `wait`）与 `ABORT` / `SKIP` / `RETRY` / `FALLBACK` 四种错误策略，让每一步决策都可观测、可干预、可回滚。详见下方 [核心亮点 14](#14-插件化管道架构agent-执行的每一处状态都可由你控制)。
- 🧠 **多层记忆系统** —— 情景记忆（EPISODE，对话压缩后的记忆）+ 语义记忆（SEMANTIC，沉淀用户偏好/项目决策/外部知识库导入等），按需检索注入。当前已上线关键词检索、标签检索与全量注入；更丰富的检索方式（如向量语义检索）与注入方式（按需/摘要注入）规划在后续版本上线，详见 [ROADMAP.md](ROADMAP.md)。

### 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | Python 3.11 / FastAPI 0.110+ / aiohttp / Redis / Pydantic / LiteLLM |
| 前端 | React 19 / TypeScript / Vite / Zustand / Antd / @lobehub/ui / Tailwind CSS |
| AI | OpenAI / Anthropic / DeepSeek / 智谱 GLM / Ollama / 多模型路由 |
| 协议 | MCP（Model Context Protocol）|
| 部署 | Docker / Docker Compose |

> **依赖说明**：`pyproject.toml` 声明 24 个核心运行时依赖（含 fastapi、redis、PyJWT、bcrypt、cryptography、httpx、sqlalchemy 等），并通过 `requirements.txt` 镜像供启动脚本使用。直接 `pip install -e .` 或 `pip install -r requirements.txt` 即可，无需手动补装。

### 📊 项目规模

- **Python 代码**：约 30.8 万行（`src/` ~16.6 万 + `tests/` ~14.2 万）
- **前端代码**：约 9.6 万行（`frontend/src/`）
- **内置工具**：41 个（`src/tools/builtin/` 下含 `tool.py` 实现）
- **真实通道**：2 个（Web / CLI）
- **模块数**：35 个（`src/` 下子目录）

---

## 🎬 演示视频

<a href="https://www.bilibili.com/video/BV1d1NV62Efh">
  <img src="https://img.shields.io/badge/▶_观看演示视频-B站-FF69B4?style=for-the-badge&logo=bilibili&logoColor=white" alt="灵汐 AgentOS 演示视频" />
</a>

> 点击上方图片在 B站 观看简单演示视频。

---

## ✨ 核心亮点

### 1. 自由度——你定义，灵汐执行
几乎所有行为都可以通过 YAML / 配置文件定制，不需要改代码。Agent 身份、提示词、工具集、模型选择、硬约束/软约束、输入输出 Schema 全部可配置。

### 2. 工具的精细化工程设计
所有工具遵循统一接口契约（`name` / `when_to_use` / `when_not_to_use` / `input_schema` / `examples` / `caveats`），预留了 `ABORT` / `SKIP` / `RETRY` / `FALLBACK` 四种错误策略框架（当前 ToolCore 默认 `RETRY`，可按工具维度扩展）。**当前实现 41 个内置工具**（含 MCP 外部工具接入）。

### 3. 智能会话——不只聊天，更是"会思考的对话"

流式响应 + 思考态实时展示 + 主动澄清 + （文档）审批交互。

> **规划中（0.2.0+）**：投票面板、媒体时间线、思考模式开关等交互增强功能尚未在当前版本实现，详见 [ROADMAP.md](ROADMAP.md)。

### 4. 前端亮点——好看、好用、好定制
8 套主题（5 套编译期预设：深色 / 浅色 / 深空指挥台 / 海洋微风 / 高对比度；3 套动态主题：林间薄雾 / 薰衣草田 / 日落晚霞）、全量配置可视化、YAML 字段自动映射表单控件。

### 5. 容器任务——复杂长期项目的引擎
对于"开发一个 App""写一部网络小说""做一个游戏"这类多阶段、有交付物的大任务，容器任务提供完整的方案规划→阶段执行→人类审查→完成验收闭环。

### 6. 触发器系统——无人值守
定时触发器（Cron）、事件触发器、间隔触发器让灵汐自己跑起来。

### 7. 工作区隔离与 worktree 机制
每个任务运行在**独立隔离的工作区**中：默认按文件夹隔离，高风险执行路径走 Docker 容器隔离；多任务场景通过 **git worktree** 为每个任务分叉出独立工作目录，互不抢占文件系统，副作用可在 worktree 边界审查与回滚。

### 8. 审批交互闭环——人机协同的质量闸
人工审批（choice / conversation 双模式）+ 管道暂停/恢复 + 反馈注入 + 任务打回重做，构成"生成→审批→反馈→迭代"的闭环。文本审批已上线，diff 渲染组件与版本对比 API 已具备（详见 [ROADMAP.md](ROADMAP.md)）。

### 9. 强制评估系统——任务质量的硬约束
任务提交时必须同时提交评估指标（acceptance criteria），管道退出后强制门控转入评估、按指标审查；指标全过才标记完成，失败重试耗尽则失败。即使 Agent 不主动评估，系统也会强制重跑——质量不被跳过。

### 10. 40+ 内置工具——开箱即用的工具箱
文件、Shell、代码搜索、浏览器、网络、记忆、媒体生成、IDE 集成（实际 41 个 tool.py 实现），含 MCP 外部工具接入。

### 11. 双通道接入——同一内核，处处可达
Web、CLI 共享同一套内核；完整支持 MCP 协议，可接入任何 MCP 服务。

### 12. Skill 能力集成——按需扩展领域能力
可加载可复用的技能（skill）包，按需注入 Agent，无需改代码即可获得新的领域能力（如文档处理、PDF 生成等）。

### 13. 配置热替换——不停机演进
`hot_swap`（快照-替换-健康检查-失败回滚）支持运行时热替换插件/Agent，`hot_reload` 监听配置文件变更自动重载，调试与迭代无需重启服务。

### 14. 插件化管道架构——Agent 执行的每一处状态都可由你控制

引擎本身只做一件事：维护一个共享的 `state` 字典，并跑一个 `while not state["ended"]` 循环。**每一轮循环里"做什么"全部交给插件决定**——你想在 Agent 执行的哪一步介入、改写什么、跳过什么、终止什么，都可以用插件实现，无需改动引擎代码。

```
用户消息 → 通道层 → 管道引擎 ┌─ Input 插件链（预处理、安全检查、上下文装配…）
                             ├─ Core 插件（LLM 调用 / 工具执行）
                             └─ Output 插件链（结果加工、路由决策…）
                                 ↑ 所有插件读写同一个共享 state ↑
```

**插件如何控制 state 的四种手段**：

| 控制手段 | 插件做的事 | 效果 |
|---------|-----------|------|
| 读写状态字段 | 返回 `state_updates`，引擎即时合并进 `state` | 下游插件、路由表、Core 都能读到 |
| 终止 / 挂起 | 写 `state["ended"]=True` / 写字段让路由选 `wait` | 立即结束管道，或挂起等外部事件（如审批）后再 `wake()` 继续 |
| 路由表按 state 选插件 | Input 路由表每轮按 `condition` 重新匹配 | 同一管道不同轮次跑不同插件组合，分支无需写死 |
| Output 信号决定下一轮 | Output 插件返回路由信号 | 下一轮跑 LLM、跑工具、结束、还是挂起 |

**4 种路由信号**清晰定义了"下一轮做什么"：

| 信号 | 含义 |
|------|------|
| `next_llm` | 下一轮调用 LLM |
| `next_tool` | 执行工具 |
| `end` | 结束管道 |
| `wait` | 挂起，等外部输入/审批 |

**4 种错误策略**让你声明插件出错时怎么办：`ABORT`（终止）、`SKIP`（跳过继续）、`RETRY`（重试）、`FALLBACK`（用兜底结果）——安全检查用 `ABORT`（不确定不能继续），上下文构建用 `FALLBACK`（降级也能跑），统计类插件用 `SKIP`（失败不影响当轮）。

**配置驱动接入**：写一个继承 `IInputPlugin` / `ICorePlugin` / `IOutputPlugin` 的 Python 类，在 YAML 里用 `name:` 或 `class:` 声明，引擎启动时自动发现并实例化。新增插件不动核心引擎，已有插件支持热替换与回滚。

---
## 🚀 快速开始

### 前置要求

- Python 3.11+（启动脚本自动探测 3.11/3.12/3.13）
- Node.js 18+（前端构建，Vite 要求）
- Docker（WSL2 + docker-ce；前端容器 + Redis 容器，后端运行在宿主机）

> **架构说明**：`docker compose` 只负责前端（静态托管）和 Redis 容器，**后端 FastAPI 进程运行在宿主机**（通过 `python -m channels.websocket.app_factory` 启动）。下方脚本会自动编排这三部分。

### 方式一：Windows 一键启动（推荐）

```bat
:: 1. 配置环境变量
copy .env.example .env
::    编辑 .env，填入 LLM API Key（参考 config/models/llm.yaml）

:: 2. 首次配置 Docker 环境（WSL2 + docker-ce，不再支持 Docker Desktop）
::    本部署使用 WSL2 + docker-ce，不再支持 Docker Desktop
::    若尚未配置，请先运行下面的脚本；已配置可跳过直接执行第 3 步
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
- Web UI：http://localhost:5289
- 后端 API：http://localhost:8988

### 跨设备 / 多实例配置说明

默认配置开箱即用，以下情况需要按需调整：

**工作空间根目录**：任务的工作文件默认存放在 `config/isolation/isolation_config.yaml` 中 `workspace.root` 指定的目录下。如果你的项目不在该路径，或希望放到其他盘符/分区，编辑该文件把 `root` 改为你的实际路径（支持绝对路径，如 Linux 的 `/tmp/ai_workspaces` 或 Windows 的 `D:/workspaces`）。注意：容器隔离模式下 `root` 必须是绝对路径，相对路径会导致 Docker bind mount 失败。

**多实例运行（同时跑两个版本做对比测试）**：compose project 会自动按**所在目录名**隔离（不同目录 = 不同的容器名/网络/卷，互不冲突），无需手动设置 `COMPOSE_PROJECT_NAME`。唯一会冲突的是**宿主端口**（前端 5289 / Redis 6480 / 后端 8988）。

宿主端口已参数化（带默认值），单实例零配置。需要同时运行第二份实例时，给它设置不同的端口即可：

```bat
:: 实例一（默认端口）：直接双击 start_web_cn.bat

:: 实例二（不同端口）：在另一个目录的命令行里
set FRONTEND_HOST_PORT=5290
set REDIS_HOST_PORT=6481
set BACKEND_PORT=8989
start_web_cn.bat
```

两个实例互不干扰：不同目录 → 不同 compose project（容器/网络/卷隔离）；不同端口 → 无冲突。启动提示会显示本实例实际使用的端口。停止时各自在对应目录执行 `docker compose down` 即可（按 project 隔离，不影响另一个）。

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
# 前端开发服务器运行在 http://localhost:5289
```

> **关于 CLI 模式**：除 Web 模式外，还支持命令行交互（不启动 Web 服务）：
> - `python run.py demo`（echo 回显）/ `python run.py real`（真实 LLM）—— 基于 `run.py` 的快捷入口
> - `cli_cn.bat`（Windows）—— 清 `__pycache__` 后启动完整 CLI（`channels.cli.cli_main`），支持 `--mode {normal,auto,plan}`、`--message` 等参数
> - `PYTHONPATH=src python -m channels.cli.cli_main`（跨平台）或安装后用注册命令 `agent-os`


---

## 📖 文档导航

| 文档 | 说明 |
|------|------|
| [README_EN.md](README_EN.md) | English README |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 系统架构详解 |
| [ROADMAP.md](ROADMAP.md) | 版本路线图 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献指南 |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | 贡献者行为准则 |
| [CHANGELOG.md](CHANGELOG.md) | 变更日志 |
| [SECURITY.md](SECURITY.md) | 安全策略与漏洞上报 |
| [AUTHORS.md](AUTHORS.md) | 贡献者名单 |
| [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) | 第三方依赖许可证清单 |

---

## 🌍 镜像仓库

为方便国内用户访问，本项目同时在以下平台维护：

- **GitHub**（主仓库）：`https://github.com/jianchen08/Agent-os-open`
- **Gitee**（镜像）：`https://gitee.com/jc27/Agent-os-open`

---

## 🤝 贡献

欢迎任何形式的贡献——提交 Issue、PR、完善文档、分享使用案例。详见 [CONTRIBUTING.md](CONTRIBUTING.md)，参与前请阅读[贡献者行为准则](CODE_OF_CONDUCT.md)。

---

## 🔒 安全策略

发现安全漏洞请勿在公开 Issue 提交，按 [SECURITY.md](SECURITY.md) 的流程私下上报。

---

## 📄 开源协议

本项目采用 [Apache License 2.0](LICENSE)。

---

## 🌟 Star History

如果这个项目对你有帮助，欢迎点亮 Star ⭐️ 支持我们！

---

> **灵汐，取自"灵气如潮汐般生生不息"** —— 我们希望 AI Agent 也能像潮汐一样，具备自我调节、自我进化的生命力。
- **测试文件**：376 个（`tests/` 下 `test_*.py`）
