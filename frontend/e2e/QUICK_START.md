# 🚀 快速开始 - 测试使用指南

## 立即运行测试

### 方式 1：运行所有测试
```bash
cd frontend
npm run test:e2e
```

### 方式 2：运行特定测试
```bash
# 交互功能测试
npx playwright test interactions.spec.ts

# 认证流程测试
npx playwright test auth-flow.spec.ts

# 冒烟测试
npx playwright test smoke.spec.ts
```

### 方式 3：调试模式（显示浏览器）
```bash
# 显示浏览器窗口
npx playwright test --headed

# 逐步调试
npx playwright test --debug

# UI 模式
npm run test:e2e:ui
```

### 方式 4：查看测试报告
```bash
# 打开 HTML 报告
npm run test:e2e:report

# 或直接打开文件
start playwright-report/index.html
```

## 📊 测试文件说明

| 文件 | 测试数 | 说明 |
|------|--------|------|
| smoke.spec.ts | 30 | 冒烟测试，验证基本功能 |
| interactions.spec.ts | 16 | 真实交互功能测试 |
| auth-flow.spec.ts | 12 | 认证流程测试 |

## 🎯 测试覆盖的功能

✅ **已测试**
- 页面加载和显示
- 响应式布局（桌面、平板、移动）
- 按钮点击和悬停
- 表单输入和验证
- 键盘导航（Tab、Enter）
- 性能测试
- 认证流程

⚠️ **需要后端支持**
- 用户登录
- 会话管理
- 数据持久化
- WebSocket 连接

## 📸 查看测试截图

测试截图保存在 `frontend/test-results/` 目录：

```bash
# 查看所有截图
ls test-results/*.png

# 在 Windows 中查看
dir test-results\*.png
```

## 🔧 常用命令

```bash
# 安装 Playwright 浏览器
npx playwright install

# 运行特定浏览器的测试
npx playwright test --project=chromium
npx playwright test --project=firefox
npx playwright test --project=webkit

# 运行特定文件的测试
npx playwright test interactions.spec.ts

# 显示浏览器运行
npx playwright test --headed

# 调试模式
npx playwright test --debug

# 查看报告
npm run test:e2e:report
```

## 📝 测试结果位置

- **HTML 报告**: `frontend/playwright-report/index.html`
- **测试截图**: `frontend/test-results/`
- **JSON 结果**: `frontend/test-results.json`
- **JUnit 结果**: `frontend/junit-results.xml`

## 🎓 学习资源

- **Playwright 文档**: https://playwright.dev
- **测试指南**: `frontend/e2e/README.md`
- **完整报告**: `frontend/e2e/FINAL_TEST_REPORT.md`
- **工作总结**: `frontend/e2e/TESTING_SUMMARY.md`

## ❓ 常见问题

### Q: 测试失败怎么办？
A:
1. 查看测试报告了解详情
2. 检查 `test-results/` 目录中的截图
3. 使用 `--debug` 模式逐步执行

### Q: 如何添加新测试？
A:
1. 在 `e2e/` 目录创建新的 `.spec.ts` 文件
2. 使用 `test()` 和 `expect()` 编写测试
3. 运行 `npx playwright test <新文件>`

### Q: 如何运行特定测试？
A:
```bash
# 运行特定文件
npx playwright test interactions.spec.ts

# 运行特定测试（使用 grep）
npx playwright test -g "应该显示欢迎"
```

## 🎉 快速验证

想要快速验证测试是否工作？

```bash
# 运行冒烟测试（最快）
npx playwright test smoke.spec.ts --project=chromium

# 这将运行 30 个快速测试，约 30 秒完成
```

---

**准备好了吗？运行 `npm run test:e2e` 开始测试！**
