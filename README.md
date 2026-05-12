# Agent OS

**以你为中心的 AI 操作系统** — 能思考、能执行、能进化、能陪伴。

Agent OS 采用插件化管道架构，将 AI Agent 的处理流程抽象为管道循环，支持多通道接入、工具系统、记忆系统、评估引擎等完整能力。

## 快速开始

### 一键启动（推荐）

```powershell
# Windows
start_web.bat

# Linux / macOS
./start_web.sh
```

启动完成后：

- **后端 API**: http://localhost:8888
- **API 文档**: http://localhost:8888/docs
- **前端界面**: http://localhost:5188

### 停止服务

```powershell
# Windows
stop_web.bat

# Linux / macOS
./stop_web.sh

# 或 PowerShell
.\stop.ps1
```

### Docker 部署

```bash
docker compose up -d
```

## 项目结构

```
.
├── src/                        # Python 后端源代码
│   ├── agents/                 # Agent 配置加载、注册、上下文构建
│   ├── api/                    # FastAPI 路由、Schema、WebSocket
│   ├── auth/                   # 认证与 RBAC 权限
│   ├── cache/                  # 多级缓存系统
│   ├── channels/               # 多通道适配（CLI/WebSocket/API/飞书/钉钉/企微/QQ）
│   ├── config/                 # 配置热重载与 Schema 校验
│   ├── connectors/             # 外部连接器
│   ├── core/                   # 核心基础（事件总线、状态机、生命周期、DI）
│   ├── db/                     # 数据库模型与会话管理
│   ├── evaluation/             # 统一评估引擎
│   ├── human_interaction/      # 人机交互服务
│   ├── infrastructure/         # 基础设施（调度/并发/资源/统计）
│   ├── interfaces/             # 稳定公共 API 重导出层
│   ├── isolation/              # 隔离执行环境
│   ├── llm/                    # LLM 适配层（OpenAI/Anthropic/Ollama/智谱）
│   ├── lsp/                    # LSP 网关服务
│   ├── memory/                 # 记忆系统（情景/语义/知识/TagWave/压缩）
│   ├── monitoring/             # 监控（健康检查/指标/用量）
│   ├── multimodal/             # 多模态处理
│   ├── orchestration/          # 编排调度
│   ├── pipeline/               # 管道核心框架（引擎/路由/插件/热替换）
│   ├── plugins/                # 插件集合（Input/Core/Output）
│   ├── ranking/                # 排序与推荐
│   ├── review/                 # 审查服务
│   ├── rollback/               # 回滚管理
│   ├── scene/                  # 场景管理
│   ├── tasks/                  # 任务管理（状态机/存储/进度/看门狗）
│   ├── templates/              # 模板系统
│   ├── tools/                  # 工具系统（注册/执行/MCP 适配）
│   ├── triggers/               # 触发器系统
│   ├── ui_schema/              # UI Schema 解析与验证
│   └── utils/                  # 通用工具
├── frontend/                   # React 前端
│   ├── src/
│   │   ├── constants/          # 常量定义
│   │   ├── hooks/              # 自定义 Hooks
│   │   ├── stores/             # Zustand 状态管理
│   │   ├── styles/             # 样式与主题
│   │   ├── types/              # TypeScript 类型定义
│   │   ├── utils/              # 工具函数
│   │   ├── App.tsx
│   │   ├── main.tsx
│   │   └── router.tsx
│   └── e2e/                    # Playwright E2E 测试
├── config/                     # YAML 配置文件
│   ├── agents/                 # Agent 配置
│   ├── pipelines/              # 管道配置
│   ├── models/                 # LLM/嵌入模型配置
│   ├── tools/                  # 工具配置
│   ├── isolation/              # 隔离策略
│   ├── rules/                  # Agent 规则
│   └── ui/                     # UI Schema 配置
├── docs/                       # 项目文档
│   ├── project/                # 项目级文档（愿景/章程/结构/逻辑/待办）
│   └── project/design/         # 设计文档
├── tests/                      # 测试文件
├── skills/                     # Skill 定义
├── vscode_extension/           # VSCode 扩展
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
└── requirements.txt
```

## 架构概览

### 分层架构

```
┌──────────────────────────────────────────────────────────────┐
│                    Channels（通道层）                          │
│         CLI / WebSocket / API / 飞书 / 钉钉 / 企微 / QQ       │
├──────────────────────────────────────────────────────────────┤
│                    Interfaces（接口层）                        │
│         IInputPlugin / ICorePlugin / IOutputPlugin            │
├──────────────────────────────────────────────────────────────┤
│                    Plugins（插件层）                           │
│         Input 插件 → Core 插件 → Output 插件                  │
├──────────────────────────────────────────────────────────────┤
│                    Pipeline（管道层）                          │
│         Engine / Route / Chain / Config / Registry            │
├──────────────────────────────────────────────────────────────┤
│                    Services（服务层）                          │
│         Tasks / Memory / Evaluation / Human Interaction       │
├──────────────────────────────────────────────────────────────┤
│                 Infrastructure（基础设施层）                    │
│         Scheduler / Concurrency / Resource / Error Policy     │
└──────────────────────────────────────────────────────────────┘
```

### 管道循环

```
输入路由 → Input 插件链 → Core 插件 → Output 插件链 → 输出路由仲裁 → apply_route
```

循环持续直到管道结束（`ended=True`）或挂起（`wait`）。

### 路由信号

| 信号 | 含义 |
|------|------|
| `next_llm` | 下一轮调用 LLM |
| `next_tool` | 下一轮执行工具 |
| `end` | 管道结束 |
| `delegate` | 委派到子管道 |
| `wait` | 管道挂起 |

## 技术栈

### 后端

- **Web 框架**: FastAPI + Uvicorn
- **LLM 集成**: LiteLLM（OpenAI / Anthropic / Ollama / 智谱）
- **配置**: YAML + Pydantic + 热重载
- **缓存**: Redis（可选）
- **Python**: >= 3.10

### 前端

- **框架**: React 19 + TypeScript
- **构建工具**: Vite
- **状态管理**: Zustand
- **UI 组件**: Radix UI + TailwindCSS + LobeHub UI
- **路由**: React Router v7
- **虚拟列表**: react-virtuoso
- **Markdown**: react-markdown + remark-gfm
- **流式渲染**: streamdown
- **E2E 测试**: Playwright

## 详细文档

| 文档 | 说明 |
|------|------|
| [项目愿景](docs/project/vision.md) | Agent OS 的产品愿景、定位与三维目标 |
| [项目章程](docs/project/charter.md) | 架构定义：插件化管道、分层架构、插件接口 |
| [项目结构](docs/project/structure.md) | 目录布局、模块清单、依赖关系 |
| [业务逻辑](docs/project/logic.md) | 核心流程、状态机、数据流 |
| [待办事项](docs/project/backlog.md) | Bug 修复、功能补全、体验优化 |
| [部署指南](docs/deployment.md) | Docker 部署说明 |
| [开发规范](CLAUDE.md) | 开发流程和规范 |

## 开发指南

### 环境要求

- Python >= 3.10
- Node.js 18+
- Redis 7+（可选，不使用 Redis 也可运行）

### 手动启动

#### 后端

```powershell
# 1. 安装依赖
pip install -e .

# 或
pip install -r requirements.txt

# 2. 启动后端
python start_server.py

# 或直接使用 uvicorn
python -m uvicorn src.api.main:app --host 0.0.0.0 --port 8888 --reload
```

#### 前端

```powershell
# 1. 进入前端目录
cd frontend

# 2. 安装依赖
npm install

# 3. 启动前端
npm run dev -- --port 5188
```

### CLI 模式

```bash
# 命令行交互模式
python -m src.channels.cli.cli_main
```

## 常用命令

### 后端

```bash
# 运行测试
pytest tests/

# 运行集成测试
pytest tests/ --run-integration
```

### 前端

```bash
cd frontend

# 开发
npm run dev

# 构建
npm run build

# 测试
npm run test

# 测试覆盖率
npm run test:coverage

# Lint
npm run lint

# 格式化
npm run format
```

## 配置

### 环境变量

创建 `.env` 文件（可从 `.env.example` 复制）：

```env
# 基础配置
APP_ENV=production
LOG_LEVEL=INFO

# LLM 配置
LLM_PROVIDER=openai
OPENAI_API_KEY=your_openai_key
OPENAI_API_BASE=https://api.openai.com/v1
OPENAI_MODEL=gpt-4

# Redis（可选）
REDIS_HOST=localhost
REDIS_PORT=6379

# 端口配置
BACKEND_PORT=8888
FRONTEND_PORT=5188
```

完整配置项见 [.env.example](.env.example)。

### YAML 配置

- **管道配置**: `config/pipelines/default.yaml`
- **Agent 配置**: `config/agents/**/*.yaml`
- **工具配置**: `config/tools/**/*.yaml`
- **模型配置**: `config/models/*.yaml`

## 里程碑

| 阶段 | 目标 | 状态 |
|------|------|------|
| M1-M6 | 核心管道引擎、插件系统、工具系统、通道层 | ✅ 完成 |
| M7-M9 | 任务系统、评估系统、人机交互、记忆系统 | ✅ 完成 |
| M10 | 前端渲染系统（五空间布局、Schema 渲染、Widget 系统） | ✅ 完成 |
| M11 | 跨管道路由、记忆系统增强 | ✅ 完成 |
| M12 | 创意生产审批闭环（制品展示、批注交互、审批协议） | ✅ 完成 |
| 未来 | 人设系统、虚拟形象、千人千面、更多场景管道 | 📋 规划中 |

## 许可证

MIT License
