# 功能验证 - 项目类型验证方式

根据项目类型选择对应的验证策略和工具集。

## 后端服务 / API

- **主验证工具**：fetch（结构化请求） + bash_execute（curl 补充）
- **验证内容**：HTTP 状态码、响应体内容、Schema 格式、错误码
- **fetch 示例**：`{"method": "POST", "url": "http://localhost:8000/api/users", "body": {"name": "test"}}`
- **curl 示例**：`curl -s -w "\n%{http_code}" http://localhost:8000/api/users`

## 前端应用 / UI 组件

- **主验证工具**：playwright_test（浏览器自动化） + fetch（HTML 结构检查）
- **验证策略分层**：
  1. **结构层**（fetch）：页面是否包含预期 DOM 元素、表单字段
  2. **行为层**（playwright_test）：点击/输入后是否有正确响应、页面跳转、状态变化
  3. **视觉层**（playwright_test 截图）：关键页面截图对比
  4. **console 层**（playwright_test）：捕获 console 错误和警告
- **playwright 操作链**：browser_launch → navigate → interact(click/type/select/drag/hover/upload) → capture_console → screenshot_compare → close

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

优先使用专用工具：fetch > curl，playwright_test > 手写脚本。
前端项目直接使用 playwright_test 工具，无需手写脚本。
