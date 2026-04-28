# Frontend Test Skill - 前端测试技能

## 概述

前端 E2E 测试技能，支持 Agent 自主执行前端交互测试。通过集成 playwright_test 封装工具，实现浏览器自动化、页面交互验证、截图对比等功能。

## 适用场景

当需要验证以下场景时激活此技能：

- **UI 渲染验证**：页面元素是否正确渲染、样式是否符合预期
- **用户流程测试**：登录、注册、表单提交、购物车等用户操作流程
- **交互功能测试**：点击、输入、选择、拖拽、悬停等交互行为
- **前后端集成验证**：API 响应与页面展示的一致性
- **回归测试**：功能修改后验证现有功能是否正常

## 引用工具

| 工具名称 | 路径 | 用途 |
|---------|------|------|
| playwright_test | src/tools/builtin/playwright_test/ | 浏览器自动化测试主工具 |

### playwright_test 支持的 action

| action | 功能 | 关键参数 |
|--------|------|----------|
| launch_browser | 启动浏览器 | browser_type(cromium/firefox/webkit), headless, args |
| navigate | 页面导航 | url, wait_until(networkidle/load/domcontentloaded) |
| interact | 页面交互 | selector, action(click/input/select/drag/hover/upload), value |
| capture_console | 捕获 Console | level(log/warn/error), message_pattern |
| compare_screenshot | 截图对比 | selector/full_page, expected_path, threshold |
| close_browser | 关闭浏览器 | - |

## 工具调用流程

### 步骤 1：启动浏览器

```
playwright_test(
    action="launch_browser",
    browser_type="chromium",
    headless=true,
    args=["--disable-dev-shm-usage"]
)
```

**参数说明**：
- `browser_type`: 浏览器类型，支持 chromium/firefox/webkit
- `headless`: 是否无头模式，生产环境建议 true
- `args`: 浏览器启动参数

### 步骤 2：导航到目标页面

```
playwright_test(
    action="navigate",
    url="https://example.com/login",
    wait_until="networkidle"
)
```

**参数说明**：
- `url`: 目标页面 URL
- `wait_until`: 等待策略
  - `networkidle`: 等待网络空闲（推荐）
  - `load`: 等待页面加载完成
  - `domcontentloaded`: 等待 DOM 解析完成

### 步骤 3：执行页面交互

#### 点击操作

```
playwright_test(
    action="interact",
    selector="[data-testid='submit-btn']",
    action="click"
)
```

#### 文本输入

```
playwright_test(
    action="interact",
    selector="[data-testid='username-input']",
    action="input",
    value="testuser"
)
```

#### 下拉选择

```
playwright_test(
    action="interact",
    selector="[data-testid='country-select']",
    action="select",
    value="CN"
)
```

#### 拖拽操作

```
playwright_test(
    action="interact",
    selector="[data-testid='draggable-element']",
    action="drag",
    target_selector="[data-testid='drop-zone']"
)
```

#### 悬停操作

```
playwright_test(
    action="interact",
    selector="[data-testid='dropdown-menu']",
    action="hover"
)
```

#### 文件上传

```
playwright_test(
    action="interact",
    selector="[data-testid='file-upload']",
    action="upload",
    value="/path/to/file.pdf"
)
```

### 步骤 4：捕获 Console 日志

```
playwright_test(
    action="capture_console",
    level="error",
    message_pattern=".*Failed to load.*"
)
```

**参数说明**：
- `level`: 日志级别，log/warn/error
- `message_pattern`: 消息匹配正则表达式（可选）

**返回格式**：
```json
{
  "logs": [
    {"level": "error", "message": "Failed to load resource: https://..."}
  ],
  "assertions": [
    {"passed": true, "expected": "error", "actual": "error"}
  ]
}
```

### 步骤 5：截图对比验证

#### 全页截图

```
playwright_test(
    action="compare_screenshot",
    full_page=true,
    expected_path="/baseline/homepage_full.png",
    threshold=0.1
)
```

#### 元素截图

```
playwright_test(
    action="compare_screenshot",
    selector="[data-testid='main-content']",
    expected_path="/baseline/content_area.png",
    threshold=0.1
)
```

**参数说明**：
- `selector`: 元素选择器（可选，不填则为全页）
- `full_page`: 是否截取整页
- `expected_path`: 基准截图路径
- `threshold`: 像素差异阈值（0.1 = 10%）

**返回格式**：
```json
{
  "passed": false,
  "diff_path": "/output/homepage_diff_20260428.png",
  "diff_ratio": 0.152,
  "message": "Screenshot differs by 15.2% (threshold: 10%)"
}
```

### 步骤 6：关闭浏览器

```
playwright_test(
    action="close_browser"
)
```

## 结果解析

### 测试结果状态

| 状态 | 含义 | 后续动作 |
|------|------|----------|
| pass | 测试通过，所有断言成功 | 输出成功日志，结束测试 |
| fail | 测试失败，存在断言错误 | 提取错误信息，进入错误反馈流程 |
| error | 执行错误，工具调用异常 | 记录错误日志，输出诊断信息 |

### 结果提取方法

```python
# 解析 playwright_test 返回结果
result = playwright_test(...)

if result["status"] == "fail":
    error_msg = result.get("message", "")
    screenshot_path = result.get("screenshot_path", "")
    diff_ratio = result.get("diff_ratio", 0)
    console_logs = result.get("console_logs", [])
```

## 错误反馈

### 修复建议流程

```
┌─────────────────────────────────────────────────────────┐
│  测试失败                                                │
│  - 截图差异                                              │
│  - 控制台错误                                            │
│  - 断言失败                                              │
└─────────────────────┬───────────────────────────────────┘
                      ▼
┌─────────────────────────────────────────────────────────┐
│  步骤 1：定位问题元素                                    │
│  - 检查 selector 是否正确                                │
│  - 确认元素是否存在于 DOM                                │
│  - 验证元素是否可见/可交互                               │
└─────────────────────┬───────────────────────────────────┘
                      ▼
┌─────────────────────────────────────────────────────────┐
│  步骤 2：检查相关代码                                    │
│  - 前端组件代码                                          │
│  - 样式定义                                              │
│  - API 响应数据                                          │
└─────────────────────┬───────────────────────────────────┘
                      ▼
┌─────────────────────────────────────────────────────────┐
│  步骤 3：修复问题                                        │
│  - 修复组件逻辑                                          │
│  - 调整样式                                              │
│  - 修正 API 返回                                         │
└─────────────────────┬───────────────────────────────────┘
                      ▼
┌─────────────────────────────────────────────────────────┐
│  步骤 4：重新测试                                        │
│  - 运行相同测试用例                                      │
│  - 验证修复是否生效                                      │
└─────────────────────────────────────────────────────────┘
```

### 常见错误修复建议

| 错误类型 | 原因 | 修复建议 |
|----------|------|----------|
| 元素未找到 | selector 错误或元素未渲染 | 检查 selector 语法，确认元素存在 |
| 元素不可交互 | 元素被遮挡或 disabled | 检查 z-index，调整样式或等待元素可用 |
| 截图差异过大 | UI 变化或渲染问题 | 确认是否为预期变化，更新基准截图 |
| 控制台错误 | JS 执行异常 | 检查错误堆栈，定位问题代码 |
| 超时错误 | 网络慢或页面加载慢 | 增加 wait_until 等待时间或添加额外等待 |

## 配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| 默认浏览器 | chromium | 支持 chromium/firefox/webkit |
| headless 模式 | true | 生产环境建议开启 |
| 默认超时 | 30000ms | 等待操作的超时时间 |
| 截图对比阈值 | 0.1 | 10% 像素差异容许度 |
| 等待策略 | networkidle | 页面加载等待策略 |
| 截图格式 | png | 支持 png/jpeg |

### 超时配置建议

| 操作类型 | 建议超时 |
|----------|----------|
| 页面导航 | 30000ms |
| 元素点击 | 10000ms |
| 文本输入 | 10000ms |
| 下拉选择 | 10000ms |
| 截图对比 | 15000ms |
| 文件上传 | 30000ms |

## 最佳实践

### 测试用例设计

1. **独立性原则**：每个测试用例独立启动/关闭浏览器，避免用例间依赖
   ```python
   # 正确做法
   async def test_login_flow():
       await launch_browser()
       try:
           # 测试步骤
       finally:
           await close_browser()
   ```

2. **使用 data-testid 定位**：避免使用 CSS 选择器，优先使用 `data-testid` 属性
   ```html
   <button data-testid="submit-btn">Submit</button>
   ```

3. **显式等待**：避免使用硬编码的 sleep，使用等待策略确保元素可用
   ```python
   # 等待元素可见
   await page.wait_for_selector("[data-testid='content']", state="visible")
   ```

### 元素定位优先级

| 优先级 | 定位方式 | 示例 |
|--------|----------|------|
| 1 | data-testid | `[data-testid='submit-btn']` |
| 2 | ID | `#main-form` |
| 3 | CSS 类 | `.btn-primary` |
| 4 | XPath | `//button[contains(text(), 'Submit')]` |

### 截图命名规范

```
{test_name}_{step}_{timestamp}.png

示例：
login_flow_01_navigate_20260428_175200.png
login_flow_02_input_20260428_175230.png
login_flow_03_submit_20260428_175300.png
```

### 截图目录结构

```
/screenshots
  /baseline          # 基准截图
    homepage_full.png
    login_form.png
  /diff              # 差异截图
    homepage_diff_20260428.png
  /actual            # 实际截图
    homepage_20260428.png
```

### 等待策略选择

| 场景 | 推荐策略 | 说明 |
|------|----------|------|
| 动态内容加载 | networkidle | 等待网络请求完成 |
| 静态页面 | load | 等待页面完全加载 |
| SPA 应用 | domcontentloaded | 等待 DOM 解析完成 |
| 动画/过渡 | 额外等待 | networkidle + 手动等待 |

### 调试技巧

1. **开启 headed 模式查看浏览器**：
   ```python
   playwright_test(action="launch_browser", headless=False)
   ```

2. **启用 slow_mo 慢速执行**：
   ```python
   playwright_test(action="launch_browser", slow_mo=100)
   ```

3. **录制视频**：
   ```python
   playwright_test(action="launch_browser", record_video_dir="/output")
   ```
