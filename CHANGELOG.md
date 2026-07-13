# 变更日志

所有对 **灵汐 AgentOS** 的重要变更都会记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/) 规范。

---

## [0.1.0] - 2026-06-22

### 🎉 首次发布

这是灵汐 AgentOS 的首个公开版本，标志着"高度可配置化 + 自进化闭环"核心理念的工程化落地。

**发布日期**：2026-06-22
**代码状态**：基于实际代码核对（pyproject.toml、package.json、src/ 目录）
**项目规模**：Python ~304K LOC（src ~162K + tests ~142K）/ 前端 ~95K LOC / 41 内置工具 / 2 个正式通道（Web / CLI，另有钉钉 / 飞书 / QQ / 企微 IM 适配器实验代码，未纳入正式支持） / 415 测试文件

### ✨ 新增功能

#### 核心架构
- **插件化管道引擎** —— 基于 4 种路由信号（`next_llm` / `next_tool` / `end` / `wait`）的统一执行框架
- **多层 Agent 协作** —— 主管 + 编排（方案规划 / 编程编排）+ 执行（写代码 / 调研 / 调试）
- **配置驱动的 Agent 加载** —— Agent 完全由 YAML 描述，支持 `hot_swap` 热替换
- **跨管道路由** —— `PipelineRegistry` 支持父子管道间精确消息回传

#### 工具系统
- **统一工具接口契约** —— `name` / `when_to_use` / `when_not_to_use` / `input_schema` / `examples` / `caveats`
- **40+ 内置工具**（实际 41 个 tool.py 实现） —— 文件、Shell、代码搜索、浏览器、网络、记忆、媒体生成、IDE 集成
- **四种错误策略** —— ABORT / SKIP / FALLBACK / RETRY
- **动态 Schema 增强** —— `image_generate` 等工具运行时注入可用 Provider 列表

#### 记忆系统
- **情景记忆（EPISODE）** —— 对话压缩后的记忆，保留要点而非逐轮原文
- **语义记忆（SEMANTIC）** —— 沉淀用户偏好 / 项目决策 / 外部知识库导入
- **基础检索 / 注入能力** —— 当前版本提供按需检索与按需注入；更丰富的多种检索方式 × 多种注入方式组合 **计划在下个版本正式上线**，详见 [ROADMAP.md](ROADMAP.md)

#### 复盘系统
- **触发机制** —— 阈值触发（500 条记录）/ 间隔触发（7 天）/ 手动触发（agent 或用户）
- **双路径降级** —— LLM 深度复盘管道优先，失败时降级到 ReviewEngine
- **实施位置** —— `src/memory/maintenance/{review_engine,service}.py` + `src/tools/builtin/trigger_review/tool.py` + `config/agents/system/review_agent.yaml`

> 注：本版本复盘系统聚焦"触发 + 复盘 + 沉淀"，记忆侧的容量治理归入记忆系统演进（见 [ROADMAP.md](ROADMAP.md)），不在复盘模块中描述。

#### 质量保障与执行隔离
- **审批交互闭环** —— choice / conversation 双模式 + 管道暂停/恢复 + 反馈注入 + 任务打回重做；diff 渲染组件与版本对比 API 已具备
- **强制评估系统** —— 任务提交时须同时提交评估指标（AC），管道退出后强制门控转入评估、按指标审查；全过才完成，失败重试耗尽则失败
- **工作区隔离** —— 文件夹隔离 + Docker 容器隔离（`src/isolation/providers/docker_provider.py`）+ git worktree 多任务分叉
- **Skill 能力集成** —— 可加载可复用的技能包，扫描 `skills/` 目录发现，按需注入 Agent

#### 触发器系统
- **Cron 定时触发** / **事件触发** / **间隔触发** —— 无人值守运行，`TriggerRegistry` 自动订阅 EventBus

#### 多通道接入
- **Web UI** —— React 19 + @lobehub/ui
- **CLI** —— 命令行交互入口
- **HTTP API** —— RESTful 接口（21 个 `routes_*.py` 路由模块，位于 `src/channels/api/`）
- **IM 适配器框架** —— 钉钉、飞书、QQ、企微共用网关层

#### 容器任务系统
- **方案规划 Agent** —— `solution_planning_agent` 自动拆解复杂任务
- **人类审查节点** —— `human_review` 强制人工确认关键决策
- **完成验证 Agent** —— `container_verification_agent` 逐条核验 AC
- **触发器系统** —— Cron 定时 / 事件触发 / 间隔轮询

#### 前端亮点
- **8 套主题** —— 5 套编译期预设（深色 / 浅色 / 深空指挥台 / 海洋微风 / 高对比度）+ 3 套动态主题（林间薄雾 / 薰衣草田 / 日落晚霞，由后端无状态清单 `frontend/public/themes/*.json` 发现）
- **全量配置可视化** —— 后端 YAML 字段自动映射表单
- **实时消息系统** —— WebSocket 流式响应 + 思考态展示
- **结构化交互** —— 审批弹窗（投票面板 / 媒体时间线 / 思考模式开关属 0.2.0+ 规划，尚未实现）

#### MCP 协议
- **完整 MCP 兼容** —— 支持 Model Context Protocol 标准
- **MCP 服务发现** —— 自动识别可用 MCP 工具
- **多 MCP 服务并行接入**

#### 部署
- **Docker Compose 一键启动** —— 前端（静态托管）+ Redis；后端 FastAPI 运行在宿主机（`python -m channels.websocket.app_factory`）
- **多环境配置** —— dev / staging / prod 通过 `.env` 切换
- **健康检查** —— `/health` 端点

### 🛠️ 技术栈

- **后端**：Python 3.10（`pyproject.toml` `requires-python = ">=3.10"`）/ FastAPI 0.110+（已声明于 `pyproject.toml`）/ Redis 5+（已声明于 `pyproject.toml`）/ Pydantic v2
- **前端**：React 19.2（`frontend/package.json` `"react": "^19.2.0"`）/ TypeScript 5.9 / Vite 8 / @lobehub/ui / Tailwind CSS 4
- **AI 接入**：OpenAI / Anthropic / DeepSeek V4（实际配置）/ 智谱 GLM（实际配置）/ Ollama
- **协议**：MCP（Model Context Protocol）
- **代码质量**：ruff / mypy / pytest / ESLint / Prettier

### 📚 文档

- 📖 [README.md](README.md) / [README_EN.md](README_EN.md)
- 🏗️ [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 🗺️ [ROADMAP.md](ROADMAP.md)
- 🤝 [CONTRIBUTING.md](CONTRIBUTING.md)
- 📜 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)

### ⚠️ 已知限制

- **依赖已收敛**：`pyproject.toml` 已声明全部 24 个核心运行时依赖（含 fastapi、redis、PyJWT、bcrypt、cryptography、httpx、sqlalchemy 等），`requirements.txt` 镜像同步。`pip install -e .` 或 `pip install -r requirements.txt` 即可直接运行，无需手动补装。（早期版本的「FastAPI/Redis 未声明」问题已修复）
- 单实例部署（Redis 作为共享状态层，水平扩展需额外配置）
- 暂未提供官方 Helm Chart（计划在 0.3.0 加入）
- 部分 IM 适配器（飞书、企微）需要用户自行申请应用凭证
- 文档的英文翻译覆盖率约 70%

详见 [ROADMAP.md](ROADMAP.md)。

### 🙏 致谢

感谢所有为这个版本付出努力的贡献者、测试用户和早期采用者。

---

## 版本说明

- **主版本号**：不兼容的 API 变更
- **次版本号**：向下兼容的功能新增
- **修订号**：向下兼容的 Bug 修复

[未发布]: https://github.com/jianchen08/Agent-os-open/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jianchen08/Agent-os-open/releases/tag/v0.1.0