# Streamdown 快速使用指南

## 🎯 一分钟启用

### 方法 1: 环境变量（推荐）

在 `frontend/.env.local` 文件中添加：
```bash
VITE_USE_STREAMDOWN=true
```

重启开发服务器即可生效。

### 方法 2: 演示页面测试

1. 启动开发服务器：
   ```bash
   cd frontend
   npm run dev
   ```

2. 访问演示页面：
   ```
   http://localhost:5188/demos/streamdown
   ```

3. 勾选"使用 Streamdown"复选框，点击"开始流式输出"观察效果

## ✅ 验证功能

访问 `/demos/streamdown` 演示页面，测试以下功能：

- ✅ GFM 表格渲染
- ✅ 数学公式显示（行内和块级）
- ✅ 代码高亮
- ✅ Mermaid 图表
- ✅ 流式输出平滑度

## 🔙 切换回 react-markdown

如果需要回退，只需设置：
```bash
VITE_USE_STREAMDOWN=false
```

或删除该环境变量（默认为 false）。

## 📝 在代码中使用

无需修改代码，`MarkdownRenderer` 组件会自动根据环境变量选择渲染器：

```typescript
import { MarkdownRenderer } from '@/components/chat/markdown/MarkdownRenderer'

// 自动使用 Streamdown 或 react-markdown
<MarkdownRenderer content={content} isStreaming={isStreaming} />
```

## 🐛 问题排查

### Streamdown 无法导入

检查包是否正确安装：
```bash
cd frontend
npm list streamdown
```

应该显示 `streamdown@1.6.10`

### 演示页面 404

检查路由配置是否正确更新：
```bash
grep StreamdownDemo src/router.tsx
```

### 环境变量不生效

确保：
1. 在 `frontend/.env.local` 中设置（不是 `.env.example`）
2. 重启了开发服务器
3. 拼写正确：`VITE_USE_STREAMDOWN`

## 📚 更多信息

详细文档：[docs/reports/streamdown-migration-complete-20251231.md](../docs/reports/streamdown-migration-complete-20251231.md)

设计文档：[docs/design/messaging-system-refactor-design.md](../docs/design/messaging-system-refactor-design.md) 第 4.2 节
