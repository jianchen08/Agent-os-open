# AI Agent 系统

元思考 Agent 系统 - 基于 LangGraph 和 FastAPI 构建的智能 Agent 框架。

## 快速开始

### 一键启动（推荐）

```powershell
# Windows PowerShell
.\start.ps1

# 或双击运行
start.bat
```

启动完成后：

- **后端 API**: http://localhost:8888
- **API 文档**: http://localhost:8888/docs
- **前端界面**: http://localhost:5188

### 停止服务

```powershell
# 停止所有服务
.\stop.ps1

# 或双击运行
stop.bat
```

### 重启服务

```powershell
# 重启所有服务
.\restart.ps1

# 或双击运行
restart.bat
```

## 项目结构

```
.
├── src/              # Python 后端
│   ├── agents/       # Agent 实现
│   ├── api/          # FastAPI 路由
│   ├── db/           # 数据库模型
│   ├── memory/       # 记忆管理
│   └── tools/        # 工具集成
├── frontend/         # React 前端
│   ├── src/
│   │   ├── components/
│   │   ├── pages/
│   │   └── stores/
│   └── package.json
├── tests/            # 测试文件
├── docs/             # 项目文档
├── config/           # 配置文件
├── scripts/          # 工具脚本
├── start.ps1         # 一键启动脚本
├── stop.ps1          # 停止服务脚本
└── restart.ps1       # 重启服务脚本
```

## 技术栈

### 后端

- **Web 框架**: FastAPI + Uvicorn
- **Agent 框架**: LangGraph
- **数据库**: PostgreSQL + SQLAlchemy
- **缓存**: Redis
- **向量数据库**: pgvector

### 前端

- **框架**: React 19 + TypeScript
- **构建工具**: Vite
- **状态管理**: Zustand
- **UI 组件**: Radix UI + TailwindCSS
- **路由**: React Router v7

## 详细文档

- 📖 [启动脚本使用说明](docs/START_GUIDE.md)
- 📋 [开发规范](CLAUDE.md)
- 🏗️ [架构文档](docs/02_Architecture.md)
- 📊 [进度追踪](docs/05_Progress.md)

## 开发指南

### 环境要求

- Python 3.14+
- Node.js 18+
- PostgreSQL 16+
- Redis 7+

### 手动启动（不使用脚本）

#### 后端

```powershell
# 1. 创建虚拟环境
python -m venv .venv

# 2. 激活虚拟环境
.venv\Scripts\activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 设置开发工具（首次运行）
python scripts/setup-dev-tools.py

# 5. 启动后端
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

## 常用命令

### 后端

```bash
# 运行测试
pytest tests/

# 代码格式化
black src/
isort src/

# 类型检查
mypy src/

# 运行 lint
flake8 src/
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

# Lint
npm run lint

# 格式化
npm run format
```

## 配置

### 环境变量

创建 `.env` 文件（可从 `.env.example` 复制）：

```env
# 数据库
DATABASE_URL=postgresql+asyncpg://user:password@localhost/agent_db

# Redis
REDIS_URL=redis://localhost:6379/0

# API Keys
OPENAI_API_KEY=your_openai_key
ANTHROPIC_API_KEY=your_anthropic_key

# 其他配置
LOG_LEVEL=INFO
DEBUG=False
```

## 故障排查

### 端口被占用

```powershell
# 查看占用端口的进程
netstat -ano | findstr :8888

# 停止进程
taskkill /PID <进程ID> /F
```

### 依赖安装失败

```powershell
# 更新 pip
python -m pip install --upgrade pip

# 清理缓存
pip cache purge
npm cache clean --force
```

更多问题请参考 [启动脚本使用说明](docs/START_GUIDE.md)。

## 贡献指南

请参考 [开发规范](CLAUDE.md) 了解项目的开发流程和规范。

## 许可证

MIT License
