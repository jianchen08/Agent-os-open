# 灵汐 AgentOS

> **可进化的智能体操作系统** —— 一切皆插件：高度可配置、自进化闭环的 AI Agent 平台

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![CI](https://github.com/jianchen08/Agent-os-open/actions/workflows/ci.yml/badge.svg)](https://github.com/jianchen08/Agent-os-open/actions/workflows/ci.yml)
[![Rust](https://img.shields.io/badge/Rust-内核-orange.svg)](https://www.rust-lang.org)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org)
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

- 🔧 **高度可配置化** —— Agent 不是写死的代码，而是 YAML 数据 + 加载器。动态提示词（当前时间/会话规则）以独立尾部消息注入，系统提示词头部保持稳定、不破坏 prompt cache 命中；改提示词、工具白名单无需重启——配置 mtime 热生效，坏配置自动回退旧版。
- 🔄 **自进化闭环** —— 任务执行 → 复盘（LLM 深度复盘产出经验报告，沉淀到知识库）→ 经资源准备流程生成新插件/新配置**热装载**进系统，形成"越用越聪明"的闭环。一切皆插件 + 全链热生效（见核心亮点 13/14）是这套闭环的地基：系统改造自己 = 加插件，不动内核。
- 🔌 **插件化管道架构** —— 引擎只解释执行一份管道 YAML，维护共享 `state` 与循环体调度，**你可以自由编写插件，控制 Agent 执行过程中的每一个状态**（插件即拦截器、`state` 即总线）：插件能改写任意状态字段，出口转移（下一轮跑 LLM 还是工具、何时结束）由管道 YAML 里的声明式路由 DSL（`when`/`then`/`set`）按 state 条件裁决，全程无需改动内核代码；配合 Python 边车 / Rust 原生双宿主轨，让每一步决策都可观测、可干预、可回滚。详见下方 [核心亮点 14](#14-插件化管道架构agent-执行的每一处状态都可由你控制)。
- 🧠 **多层记忆系统** —— 情景记忆（EPISODE，对话压缩后的记忆）+ 语义记忆（SEMANTIC，沉淀用户偏好/项目决策/外部知识等），按需检索注入，由 hindsight 记忆插件承载、经 memory 工具读写；更丰富的检索与注入方式规划见 [ROADMAP.md](ROADMAP.md)。

### 技术栈

| 层级 | 技术 |
|------|------|
| 内核 | Rust（axum / tokio / SQLite），单进程微内核 :9100 |
| 插件 | Python 3.11 sidecar（MCP over stdio，uv venv 独立环境）+ Rust cdylib 原生轨 |
| 前端 | React 19 / TypeScript / Vite / Zustand / Antd / @lobehub/ui / Tailwind CSS |
| AI | OpenAI / Anthropic / DeepSeek / 智谱 GLM / Ollama / 多模型路由（LiteLLM） |
| 协议 | MCP（Model Context Protocol）|
| 部署 | Docker / Docker Compose（按需 Redis 容器） |

> **构建说明**：内核 `cd kernel && cargo build --release --bin agentos-kernel`；每个 Python 插件目录自带 `pyproject.toml`，用 `uv sync --project <插件目录>` 建独立 venv；前端 `npm install`。启动脚本 `start_web_02.*` 一键完成上述步骤。

### 📊 项目规模

- **Rust 内核**：约 8.1 万行（`kernel/`，其中约 1.8 万行为 Rust 测试）
- **Python 插件**：约 6.0 万行（`plugins/`，110 份 `plugin.json` 清单）
- **前端代码**：约 4.5 万行（`frontend/src/`）
- **Python 测试**：约 5.4 万行（`tests/` + 插件目录就地测试）
- **工具插件**：26 个（另可零代码接入任意 MCP 外部工具）
- **接入端**：Web 前端（直连内核）

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
所有工具遵循统一契约（`input_schema` + `output_schema` + `render` 渲染意图）：内核按 `output_schema` fail-closed 校验结果，前端按 `render` 路由渲染结果卡片；LLM 可见工具由 Agent 配置的 `tool_ids` 白名单精确控制。**内置 26 个工具插件**，另可零代码接入任意 MCP 外部工具。

### 3. 智能会话——不只聊天，更是"会思考的对话"

流式响应 + 思考态实时展示与模式切换 + 主动澄清 + （文档）审批交互。

> **规划中（0.2.0+）**：投票面板、媒体时间线等交互增强尚未实现，详见 [ROADMAP.md](ROADMAP.md)。

### 4. 前端亮点——好看、好用、好定制
7 套编译期预设主题（深色 / 浅色 / 深空指挥台 / 海洋微风 / 像素糖果 / 奶油甜心 / 高对比度）+ 3 套动态 JSON 主题 + 插件下发的主题/皮肤（`contributes.themes`）、全量配置可视化、YAML 字段自动映射表单控件。

### 5. 容器任务——复杂长期项目的引擎
对于"开发一个 App""写一部网络小说""做一个游戏"这类多阶段、有交付物的大任务，容器任务提供完整的方案规划→阶段执行→人类审查→完成验收闭环。

### 6. 触发器系统——无人值守
定时触发器（Cron）、事件触发器、间隔触发器让灵汐自己跑起来。

### 7. 工作区隔离与 worktree 机制
每个任务运行在**独立隔离的工作区**中：默认按文件夹隔离，高风险执行路径走 Docker 容器隔离；多任务场景通过 **git worktree** 为每个任务分叉出独立工作目录，互不抢占文件系统，副作用可在 worktree 边界审查与回滚。

### 8. 审批交互闭环——人机协同的质量闸
人工审批（choice / conversation 双模式，human-interaction 工具阻塞等待用户响应）+ 反馈注入 + 任务打回重做，构成"生成→审批→反馈→迭代"的质量闸闭环。

### 9. 强制评估系统——任务质量的硬约束
任务提交时必须同时提交评估指标（acceptance criteria），管道退出后强制门控转入评估、按指标审查；指标全过才标记完成，失败重试耗尽则失败。即使 Agent 不主动评估，系统也会强制重跑——质量不被跳过。

### 10. 26 个工具插件——开箱即用的工具箱
文件、Shell、代码搜索、浏览器、网络、记忆、媒体生成、IDE 集成（26 个工具插件），且任意第三方 MCP 服务零代码接入。

### 11. 多端接入——同一内核，处处可达
Web 前端直连 Rust 内核（HTTP / WebSocket）；插件与内核之间走 MCP 协议，第三方 MCP 服务可零代码接入。

### 12. Skill 能力集成——按需扩展领域能力
`skills/` 目录下的可复用技能包（SKILL.md）：Agent 按提示词引导懒加载（需要时 file_read 全文遵循执行），不改代码即可扩展领域能力；技能与规则/提示词三层解耦，可独立增删。

### 13. 配置热生效——不停机演进
Agent 配置（YAML）mtime 缓存热生效，改提示词/工具白名单无需重启；插件目录与 manifest 变更热发现自动注册/重注册（文件监听 + 轮询兜底，秒级），Python 插件进程空闲自动回收、代码改动热重载、崩溃自动拉起；管道配置每次执行前检测文件变化自动重编译，坏配置自动回退旧版本并告警。

### 14. 插件化管道架构——Agent 执行的每一处状态都可由你控制

**一切皆插件**：内核只是执行基座（管道解释执行、能力注册表、插件装载、存储），LLM 调用、记忆、评估、审批、触发器、通道、主题等一切能力都由插件承载——这也是"可进化的智能体操作系统"的落地机制：系统自我改造 = 生成新插件热装载生效，改任何业务行为都不动内核；插件崩溃互相隔离，Python 生态即插即用，第三方经 MCP 零代码接入。

内核引擎只做一件事：解释执行 `config/pipelines/autonomous.yaml`，维护一个共享的 `state` 字典与循环体调度。**每一轮循环里"做什么"全部交给插件决定**——你想在 Agent 执行的哪一步介入、改写什么、跳过什么、终止什么，都可以用插件实现，无需改动内核代码。

```
用户消息 → Web 前端 → Rust 内核引擎 ┌─ init 循环体（工作空间/环境解析）
                                    ├─ main 循环体（while 循环）
                                    │    ├─ prepare：Input 插件链（上下文构建/工具面/提示词/安全守卫…）
                                    │    ├─ core：LLM 调用 ↔ 工具执行（按路由动态切换）
                                    │    └─ post：Output 插件链（统计/评估闸门/卡死检测…）+ 路由仲裁
                                    └─ exit 循环体（工作空间收尾，出错也必经）
                                        ↑ 所有插件读写同一个共享 state ↑
```

**插件与路由的分工**：插件只读写 state（返回 `state_updates` 即时合并）；"下一轮做什么"由管道 YAML 的路由 DSL 按 state 条件裁决，加载期编译、运行时零解析。

| `then` 转移目标 | 含义 |
|------|------|
| `loop` | 继续循环（配合 `set:` 切换轮次，如 `core_type=tool_execute` 进入工具执行轮） |
| `end` | 结束管道 |
| step id / 循环体 id | 步骤间跳转 / 跨循环体转移 |

出口转移的判定条件与附带写入全部声明在管道配置里（如"有工具调用 → 回循环执行工具；工具刚执行完 → 回 LLM；其余 → 结束"），可观测、可干预、可回滚。

**插件即声明**：一个插件 = 一个目录 + 一份 `plugin.json` 清单（声明工具 / 服务 / 生命周期钩子 / HTTP 端点），内核统一发现、校验、注册。宿主双轨自选——Python 边车（独立进程，MCP over stdio，uv venv 隔离）或 Rust 原生（cdylib，进程内零 IPC）；还可零代码接入任意第三方 MCP 服务。插件错误由引擎统一兜底：sidecar 崩溃自动重启并重试一次，工具失败结果回喂 LLM 自我修正。

开发文档：[docs/plugin-protocol.md](docs/plugin-protocol.md)（插件协议权威）· [docs/guides/README.md](docs/guides/README.md)（分篇上手教程）。

---
## 🚀 快速开始

### 前置要求

- Python 3.11+（启动脚本自动探测 3.11/3.12/3.13）
- Node.js 18+（前端构建，Vite 要求）
- Docker（WSL2 + docker-ce；前端容器 + Redis 容器，后端运行在宿主机）

> **架构说明**：0.2 为 Rust 内核（`kernel/`）+ Vite 前端直连内核架构：`start_web_02.*` 编译并启动
> `agentos-kernel`（:9100，宿主机进程）与 Vite 前端 dev server（:6390，反代到内核）；
> `docker compose` 仅按需提供 Redis 容器（docker/0.2/docker-compose.yml）。

### 方式一：Windows 一键启动（推荐）

```bat
:: 1. 配置环境变量
copy .env.example .env
::    编辑 .env，填入 LLM API Key（参考 config/models/llm.yaml）

:: 2.（可选）配置 WSL2 + docker-ce 环境供 Redis 容器使用；已配置可跳过
install_native_docker.bat

:: 3. 启动项目（编译 Rust 内核 + 启动内核 :9100 / 前端 :6390 / Redis）
start_web_02.bat

:: 停止
stop_web_02.sh   （或按端口结束进程，见脚本末尾提示）
```

启动后：
- Web UI：http://localhost:6390
- 内核 API：http://localhost:9100

### 方式二：Linux / macOS 一键启动

```bash
# 1. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 LLM API Key

# 2. 启动（编译 Rust 内核 + 启动内核 :9100 / 前端 :6390 / Redis）
#    注意：0.1 的 install.sh / start_web.sh / stop_web.sh 已废弃，勿用
chmod +x start_web_02.sh
./start_web_02.sh            # 完整启动（编译 + 内核 + 前端）
# 或 ./start_web_02.sh --no-build   # 跳过编译直接启动

# 停止
./stop_web_02.sh
```

启动后：
- Web UI：http://localhost:6390
- 内核 API：http://localhost:9100

### 跨设备 / 多实例配置说明

默认配置开箱即用，以下情况需要按需调整：

**工作空间根目录**：任务的工作文件默认存放在 `config/isolation/isolation_config.yaml` 中 `workspace.root` 指定的目录下。如果你的项目不在该路径，或希望放到其他盘符/分区，编辑该文件把 `root` 改为你的实际路径（支持绝对路径，如 Linux 的 `/tmp/ai_workspaces` 或 Windows 的 `D:/workspaces`）。注意：容器隔离模式下 `root` 必须是绝对路径，相对路径会导致 Docker bind mount 失败。

**多实例运行（同时跑两个版本做对比测试）**：compose project 会自动按**所在目录名**隔离（不同目录 = 不同的容器名/网络/卷，互不冲突），无需手动设置 `COMPOSE_PROJECT_NAME`。唯一会冲突的是**宿主端口**（前端 6390 / 内核 9100 / Redis 6480）。

宿主端口已参数化（带默认值），单实例零配置。需要同时运行第二份实例时，给它设置不同的端口即可：

```bat
:: 实例一（默认端口 9100/6390）：直接双击 start_web_02.bat

:: 实例二（不同端口）：在另一个目录的命令行里
set FRONTEND_HOST_PORT=5290
set REDIS_HOST_PORT=6481
set AGENTOS_KERNEL_PORT=9101
set AGENTOS_FRONTEND_PORT=6391
start_web_02.bat
```

两个实例互不干扰：不同目录 → 不同 compose project（容器/网络/卷隔离）；不同端口 → 无冲突。启动提示会显示本实例实际使用的端口。停止时各自在对应目录执行 `docker compose down` 即可（按 project 隔离，不影响另一个）。

### 方式三：手动开发模式

适合不使用脚本、需要精细控制的开发者。

```bash
# 1. 编译并启动 Rust 内核（0.1 的 src/ + channels.websocket 入口已删除）
cd kernel && cargo build --release --bin agentos-kernel
export AGENTOS_PLUGINS_DIR=../plugins/shared AGENTOS_CONFIG_ROOT=../config
./target/release/agentos-kernel    # 内核运行在 http://localhost:9100

# 2. 启动前端（另一个终端）
cd frontend
npm install
npm run dev    # 前端开发服务器运行在 http://localhost:6390（反代到内核 :9100）
```

> **关于 CLI 模式**：0.1 的独立 CLI 入口（`cli_cn.bat` / `run.py` / `channels.cli.cli_main`）
> 已于 0.2 裁定砍掉（CLI 插件化盘点结论，2026-08-20）：交互式 REPL 依赖的 0.1 进程内
> Python 管道引擎（`pipeline.engine` / `infrastructure.*` 等）在 0.2 已由 Rust 内核引擎
> 取代，无等价物也无真实消费场景，交互体验统一走 Web 前端（`start_web_02.bat` /
> `start_web_02.sh`）。`plugins/shared/system/channel_cli` 保留为插件壳，经 sidecar
> 加载（唯一正途），提供 `cli.get_status` / `cli.sanitize_text` 服务与
> `CLIOutputAdapter`（终端文本清理，接口来自 `channel_common` 渠道共享包）。


---

## 📖 文档导航

| 文档 | 说明 |
|------|------|
| [README_EN.md](README_EN.md) | English README |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 系统架构详解 |
| [docs/plugin-protocol.md](docs/plugin-protocol.md) | 插件协议开发者文档（plugin.json 全字段权威） |
| [docs/guides/README.md](docs/guides/README.md) | 开发指南索引（插件 / 主题 / Agent / 管道配置 / 排障） |
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
