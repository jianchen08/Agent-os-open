# 功能验证 - 项目类型验证方式

根据项目类型选择对应的验证策略和工具集。

## 后端服务 / API

- **主验证工具**：fetch（结构化请求） + bash_execute（curl 补充）
- **验证内容**：HTTP 状态码、响应体内容、Schema 格式、错误码
- **fetch 示例**：`{"method": "POST", "url": "http://localhost:8000/api/users", "body": {"name": "test"}}`
- **curl 示例**：`curl -s -w "\n%{http_code}" http://localhost:8000/api/users`

## 前端应用 / UI 组件

- **主验证方式（优先）**：通过命令行运行 playwright 测试脚本，覆盖完整的浏览器自动化验证（点击/输入/导航/截图/console/DOM 断言），比命令式逐步驱动浏览器更接近"用户真的去用"。
- **第一步：探测项目 playwright 栈**（用 bash_execute + file_read）：
  - **Node 栈**：项目根有 `package.json`，依赖里含 `@playwright/test` 或 `playwright` → 用 `npx playwright test <spec>` 跑测试
  - **Python 栈**：项目根有 `pyproject.toml` 或 `requirements.txt`，依赖里含 `playwright` → 用 `pytest <测试文件>` 跑测试
  - 同时搜索项目已有 spec 文件（`e2e/**/*.spec.ts`、`tests/**/test_*.py`）
- **第二步：选择 spec 来源（优先复用，无则生成）**：
  1. **优先复用项目已有 spec**：找到能覆盖当前需求的 spec，直接用对应 CLI 命令运行它
  2. **无现成 spec 覆盖当前需求时，现场生成一个 spec 文件再跑**：
     - Node：写到项目 `e2e/`（或现有 playwright 配置的 testDir）下，命名 `*.spec.ts`，遵循项目 `playwright.config.ts` 的约定（baseURL、testDir、testMatch），写完用 `npx playwright test e2e/xxx.spec.ts` 跑
     - Python：写到对应 `tests/` 目录，命名 `test_*.py`，用 `pytest tests/test_xxx.py` 跑
- **命令示例**：
  - Node：`npx playwright test e2e/feature.spec.ts --reporter=list`
  - Python：`pytest tests/test_feature.py -v`
- **第三步：回退策略**：仅当项目既无 Node 也无 Python playwright 栈，或 CLI 执行失败时，才回退到内置 `playwright_test` 工具做命令式浏览器验证（browser_launch → navigate → interact → capture_console → screenshot_compare → close）
- **验证策略分层**（CLI spec 与内置工具都应覆盖）：
  1. **行为层**：点击/输入后是否有正确响应、页面跳转、状态变化
  2. **视觉层**（截图）：关键页面截图对比
  3. **console 层**：捕获 console 错误和警告
  4. **结构层**：页面是否包含预期 DOM 元素、表单字段
- **playwright 全部不可用时的处理**（CLI 与内置工具都不可用）：
  1. **禁止降级到 fetch**：fetch 只能做 HTTP 请求，无法替代浏览器自动化验证前端交互行为，用 fetch 替代 playwright 做前端验证是不充分的验证
  2. **必须如实报告**：在验证报告的 tool_capability_assessment 中明确说明：
     - 哪些前端/UI内容**无法验证**（如按钮点击响应、页面渲染、交互行为）
     - 需要什么工具才能完成验证（如需要安装 playwright 及其浏览器：`npx playwright install` / `playwright install`）
     - 请求上级 Agent 或人类协助提供所需工具/环境
  3. **可验证部分正常执行**：如果前端项目有后端 API，后端部分使用 fetch 正常验证

## CLI 工具 / 脚本

- **主验证工具**：bash_execute
- **验证内容**：退出码（$?）、标准输出、标准错误
- **示例**：`python cli.py --input test.txt; echo "exit: $?"`

## 配置文件 / 数据格式

- **主验证工具**：file_read（读取内容） + bash_execute（解析验证） + evaluate（格式校验）
- **验证内容**：字段完整性、格式正确性、默认值、必填项
- **示例**：`python -c "import yaml; d=yaml.safe_load(open('config.yaml')); assert 'host' in d"`

## Agent / 智能体系统

- **主验证工具**：bash_execute（模拟用户输入） + file_read（日志/输出分析）
- **验证内容**：Agent 响应是否符合预期、工具调用是否正确、多轮对话状态保持

## 游戏 / 交互程序

- **主验证工具**：bash_execute（管道输入）
- **验证内容**：游戏状态变化、得分、胜负判定
- **示例**：`echo -e "input1\ninput2\n" | python game.py`

## 工具选择原则

- **后端/API**：优先使用专用工具（fetch > curl）
- **前端/UI**：优先用 playwright 命令行跑测试脚本（npx playwright test / pytest），先探测栈、优先复用已有 spec、无则现场生成；CLI 栈不可用时回退内置 playwright_test 工具；两者都不可用时禁止降级到 fetch，必须如实报告工具缺口
- **前端验证通过条件**：可验证部分全部实际验证通过 + 不可验证部分有清晰说明和资源请求
