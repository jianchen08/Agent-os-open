# 贡献指南

感谢你考虑为 **灵汐 AgentOS** 做出贡献！正是有了像你这样的人，这个开源项目才能变得更好。

## 📜 行为准则

参与本项目即代表你同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。请在所有互动中保持尊重与专业。

---

## 🐛 报告 Bug

发现 Bug？请通过 [Bug 报告模板](.github/ISSUE_TEMPLATE/bug_report.md) 提交 Issue。提交前请：

1. 搜索现有 Issue，确认问题未被重复报告
2. 使用最新版本复现问题
3. 提供详细的复现步骤、环境信息、错误日志

---

## 💡 提出新功能

有新功能想法？请通过 [功能请求模板](.github/ISSUE_TEMPLATE/feature_request.md) 提交 Issue。请说明：

- **痛点**：当前缺失什么？要解决什么问题？
- **方案**：你设想的功能如何工作？
- **替代方案**：是否考虑过其他实现方式？

大型功能建议先在 Discussion 中讨论，达成共识后再提交 Issue。

---

## 🔧 提交 Pull Request

### 准备工作

1. **Fork 仓库** 并克隆到本地
2. 创建特性分支：`git checkout -b feature/your-feature-name`
3. 安装开发依赖：`pip install -e ".[dev]"`（运行时依赖已在 `pyproject.toml` 中全部声明，无需手动补装 FastAPI/Redis）
4. 阅读 [开发规范](#开发规范) 和 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

### 编码流程

```bash
# 1. 编写代码
# 2. 添加/更新测试
pytest tests/

# 3. 格式化代码
ruff format .
ruff check --fix .

# 4. 类型检查
mypy src/

# 5. 提交
git add .
git commit -m "feat: add amazing feature"

# 6. 推送并创建 PR
git push origin feature/your-feature-name
```

### PR 要求

- ✅ **遵循 PR 模板**（见 [PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md)）
- ✅ **通过所有 CI 检查**（Lint / Type Check / Unit Test）
- ✅ **新代码覆盖率 ≥ 80%**
- ✅ **无新增 Lint warning**（`--max-warnings 0`）
- ✅ **更新相关文档**（如修改了 API、配置、架构）
- ✅ **commit message 遵循 [Conventional Commits](https://www.conventionalcommits.org/)**

### Commit 规范

格式：`<type>[scope]: description`

| type | 用途 |
|------|------|
| feat | 新功能 |
| fix | Bug 修复 |
| docs | 文档变更 |
| style | 代码格式（不影响功能） |
| refactor | 重构（不新增功能、不修复 Bug） |
| perf | 性能优化 |
| test | 测试相关 |
| build | 构建系统/依赖变更 |
| ci | CI 配置变更 |
| chore | 杂项 |

示例：
```
feat(pipeline): add cross-pipeline routing via PipelineRegistry
fix(tools): correct schema for image_generate when no provider
docs(readme): update quick start for Docker
```

### 审查流程

1. 提交 PR 后，CI 会自动运行
2. 维护者会进行 Code Review
3. 根据反馈修改并 force-push
4. 审查通过后由维护者 merge

---

## 🛠️ 开发规范

### 代码风格

- **Python**：遵循 PEP 8，使用 `ruff` 强制格式化（项目 `pyproject.toml` 已配置 ruff + mypy）
  - Python 版本：3.11+（`requires-python = ">=3.11"`）
- **TypeScript**：遵循项目 ESLint + Prettier 配置
  - React 版本：19.2+（`frontend/package.json`）
- **命名**：
  - 变量/函数：`snake_case`（Python）/ `camelCase`（TS）
  - 类/接口：`PascalCase`
  - 常量：`UPPER_SNAKE_CASE`
  - 私有成员：`_leading_underscore`

### 架构原则

- **单一职责**：每个模块只有一个变更原因
- **开闭原则**：对扩展开放、对修改关闭
- **依赖倒置**：依赖抽象而非具体实现
- **配置优于代码**：能用 YAML 表达的不写 Python

详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 的「架构设计四问」。

### 测试要求

- **新功能必须附带测试**（单元 + 集成）
- **Bug 修复必须先写失败用例**（TDD 修复流程）
- **覆盖率 ≥ 80%**（行覆盖 + 分支覆盖）
- **测试名描述意图**：`should_reject_expired_token` 而非 `test_1`

---

## 📁 项目结构

```
Agent-os/
├── src/                  # 后端源码
│   ├── pipeline/        # 管道引擎（路由信号、插件链、热替换）
│   ├── agents/          # Agent 系统（注册表、YAML 加载、协作）
│   ├── tools/           # 工具系统（内置工具、MCP 适配、注册表）
│   ├── memory/          # 记忆系统（EPISODE/SEMANTIC、检索注入、复盘维护）
│   ├── channels/        # 通道层（WebSocket/CLI/API/钉钉/飞书/企微/QQ + 网关）
│   ├── skills/          # Skill 注册与发现
│   ├── isolation/       # 工作区隔离（Docker/Host Provider、worktree）
│   ├── triggers/        # 触发器系统（Cron/事件/间隔）
│   ├── evaluation/      # 强制评估系统
│   ├── plugins/         # 管道插件实现（input/output/core）
│   ├── connectors/      # 外部工具连接器（VSCode/游戏引擎/ComfyUI）
│   ├── infrastructure/  # 基础设施（任务执行、通知、恢复）
│   └── ...              # auth / monitoring / llm / ui_schema 等
├── skills/              # 可复用技能包（Skill 根目录）
├── frontend/            # 前端源码（React 19 + Vite）
├── config/              # 配置文件（agents/tools/pipelines/triggers/...）
├── tests/               # 测试
├── docs/                # 文档
└── .github/             # Issue / PR 模板
```

---

## 🌐 国际化（i18n）

我们欢迎多语言翻译贡献：

- 文档翻译：在 `docs/i18n/<lang>/` 下创建对应翻译
- UI 翻译：在 `frontend/src/locales/<lang>/` 下提交 PR

目前已支持：简体中文、英文。

---

## 💬 交流渠道

- **GitHub Issues**：Bug 报告、功能请求
- **GitHub Discussions**：技术讨论、问题求助
- **Gitee Issues**：国内用户反馈通道
- 邮件：`chenjian1306792950@foxmail.com`

---

## 🙏 致谢

每一位贡献者都会被记录在 [AUTHORS.md](AUTHORS.md) 中。你的名字将永远留在项目的历史里。

---

**再次感谢你的贡献！** 🌟