# 会话页面测试执行指南

## 快速开始

### 1. 确保服务运行

在运行测试之前，确保后端和前端服务都在运行：

```bash
# 方式 1: 使用启动脚本（推荐）
.\start.bat

# 方式 2: 手动启动
# 终端 1: 启动后端
python -m uvicorn src.api.main:app --host 0.0.0.0 --port 8888 --reload

# 终端 2: 启动前端
cd frontend
npm run dev -- --port 5188
```

验证服务：
- 后端: http://localhost:8888/docs
- 前端: http://localhost:5188

### 2. 运行简化测试（推荐首次运行）

```bash
cd frontend

# 运行简化测试套件（10个测试）
npx playwright test 04-session-page-simple.spec.ts --project=chromium

# 查看报告
npx playwright show-report
```

### 3. 运行完整测试

```bash
cd frontend

# 运行所有测试（20个测试 × 5个浏览器 = 100个测试）
npx playwright test 04-session-page.spec.ts

# 只运行 Chromium
npx playwright test 04-session-page.spec.ts --project=chromium

# 运行单个测试
npx playwright test 04-session-page.spec.ts --grep "01-应该正确显示会话页面"
```

## 测试文件说明

### 04-session-page.spec.ts (完整测试套件)

**文件位置**: `frontend/e2e/04-session-page.spec.ts`

**测试数量**: 20 个测试用例

**测试内容**:
1. ✅ 页面加载和基本元素
2. ✅ 会话列表显示
3. ✅ 创建新会话
4. ✅ 会话切换
5. ✅ 会话历史记录
6. ✅ 桌面端响应式布局
7. ✅ 平板端响应式布局
8. ✅ 移动端响应式布局
9. ✅ WebSocket 连接状态
10. ✅ 消息列表显示
11. ✅ 消息输入区域
12. ✅ 会话搜索
13. ✅ 会话编辑
14. ✅ 会话删除
15. ✅ 星标功能
16. ✅ 加载状态
17. ✅ 错误状态
18. ✅ 会话分组
19. ✅ 统计信息
20. ✅ 导航功能

**特点**:
- 使用多个选择器策略，提高鲁棒性
- 容错处理，不会因为找不到元素而失败
- 详细的控制台日志
- 每个测试都生成截图证据

### 04-session-page-simple.spec.ts (简化测试套件)

**文件位置**: `frontend/e2e/04-session-page-simple.spec.ts`

**测试数量**: 10 个测试用例

**测试内容**:
1. ✅ 页面基础加载
2. ✅ 会话列表元素探测
3. ✅ 新建会话按钮探测
4. ✅ 消息区域探测
5. ✅ 输入区域探测
6. ✅ 响应式布局检查
7. ✅ 页面错误监听
8. ✅ API 请求监听
9. ✅ 可访问性检查
10. ✅ 性能检查

**特点**:
- 快速验证核心功能
- 多选择器探测
- 控制台和网络监听
- 性能指标记录

## 查看测试结果

### HTML 报告（推荐）

```bash
cd frontend
npx playwright show-report
```

会在浏览器中打开详细的测试报告，包含：
- 测试结果概览
- 失败测试的详细信息
- 截图和视频
- 测试执行时间

### JSON 报告

```bash
# 查看测试结果 JSON
cat frontend/test-results.json

# 或用 PowerShell
Get-Content frontend/test-results.json | ConvertFrom-Json
```

### JUnit 报告

```bash
# 查看测试结果 XML
cat frontend/junit-results.xml
```

### 截图证据

测试会生成截图到 `frontend/test-results/screenshots/` 目录：

```bash
# 查看所有截图
cd frontend/test-results
dir screenshots /s /b | findstr ".png"
```

或用 PowerShell：
```powershell
Get-ChildItem frontend\test-results -Recurse -Filter *.png | Select-Object FullName
```

## 调试测试

### 交互式调试

```bash
cd frontend
npx playwright test 04-session-page-simple.spec.ts --debug
```

这会：
- 打开 Playwright Inspector
- 慢速执行测试
- 允许逐步检查元素

### 显示浏览器窗口

```bash
cd frontend
npx playwright test 04-session-page-simple.spec.ts --headed
```

### 单个文件测试

```bash
cd frontend
npx playwright test 04-session-page-simple.spec.ts --grep "01-页面应该可以加载"
```

### 增加超时时间

```bash
cd frontend
npx playwright test 04-session-page-simple.spec.ts --timeout=120000
```

## 常见问题

### 问题 1: 测试超时

**症状**: 测试运行超过 30 秒后超时

**解决方案**:
```bash
# 增加超时时间
npx playwright test 04-session-page-simple.spec.ts --timeout=60000
```

### 问题 2: 服务未运行

**症状**: `Error: connect ECONNREFUSED localhost:8888`

**解决方案**:
```bash
# 检查后端是否运行
curl http://localhost:8888/docs

# 或在浏览器打开
# http://localhost:8888/docs
```

### 问题 3: 测试找不到元素

**症状**: `Timeout 30000ms exceeded`

**解决方案**:
- 检查页面是否正确加载
- 使用 `--debug` 模式查看页面状态
- 查看截图确认元素是否存在

### 问题 4: 测试目录错误

**症状**: `Test file not found`

**解决方案**:
```bash
# 确认在正确的目录
cd frontend
pwd  # 应该显示 .../Agent/frontend

# 确认测试文件存在
ls e2e/04-session-page*.spec.ts
```

## CI/CD 集成

### GitHub Actions 示例

```yaml
name: E2E Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v3

      - name: Setup Node.js
        uses: actions/setup-node@v3
        with:
          node-version: 18

      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          cd frontend && npm install

      - name: Start services
        run: ./start.bat

      - name: Run Playwright tests
        run: |
          cd frontend
          npx playwright test 04-session-page-simple.spec.ts

      - name: Upload test results
        if: always()
        uses: actions/upload-artifact@v3
        with:
          name: test-results
          path: frontend/test-results/
```

## 测试报告模板

测试完成后，可以填写以下报告模板：

```
# 测试执行报告

**日期**: 2025-12-27
**测试文件**: 04-session-page-simple.spec.ts
**执行环境**: Windows 10, Chromium 120

## 测试结果概览

- 总测试数: 10
- 通过: X
- 失败: X
- 跳过: X
- 执行时间: X 秒

## 详细结果

| 测试名称 | 状态 | 执行时间 | 备注 |
|---------|------|---------|------|
| 01-页面应该可以加载 | ✅/❌ | Xs | |
| 02-应该查找会话列表相关元素 | ✅/❌ | Xs | |
| ... | | | |

## 发现的问题

1. [问题描述]
   - 重现步骤:
   - 预期结果:
   - 实际结果:
   - 截图: [文件名]

## 建议

[改进建议]

## 附件

- 测试报告: playwright-report/index.html
- 截图: test-results/screenshots/
- 视频: test-results/videos/
```

## 下一步

1. ✅ 创建测试文件
2. ✅ 创建测试报告
3. ⏳ 运行测试并收集结果
4. ⏳ 分析失败测试
5. ⏳ 修复发现的问题
6. ⏳ 更新测试用例
7. ⏳ 集成到 CI/CD

## 参考资源

- [Playwright 官方文档](https://playwright.dev/)
- [Playwright 测试最佳实践](https://playwright.dev/docs/best-practices)
- [项目测试文档](../docs/modules/testing.md)
