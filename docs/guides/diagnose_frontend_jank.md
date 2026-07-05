# 前端卡顿诊断报告（实测）

> 方法：用 Playwright 驱动真实 Chromium，登录后触发一次完整流式对话（1000+ 个 stream_chunk），
> 通过 CDP 拦截所有 WebSocket 流量 + 页面探针采集长任务/帧率 + Chrome trace 火焰图归因。
>
> 测试脚本：`frontend/scripts/probe_jank.mjs`（带 CDP Tracing）、`frontend/scripts/probe_clean.mjs`（无干扰轻量版）。
> 数据文件：`frontend/probe-out/jank-report.json`、`clean-report.json`、`jank-trace.json`（34MB，14.8 万事件）。

---

## 结论先行

**接收消息 / 流式渲染不是性能问题，实测数据排除了最初的怀疑。**

你感受到的"卡顿"如果真实存在，**不在消息接收链路**。实测在 1000+ 个流式 token、22 帧/秒的推送强度下，流式期间的长任务数为 **0**，帧率稳定。

但要注意：本次测试是**单次**回复、**中等长度**历史。如果你在**超长会话（接近 300 条消息上限）**或**多 tab 并发**下感到卡，那是另一个故事（见文末"未覆盖场景"）。

---

## 一、实测数据（决定性证据）

### 1.1 干净测量（无 tracing 干扰，最接近真实体验）

| 场景 | stream_chunk 数 | 流式期间新增长任务 | 全程长任务 | 真实 fps |
|------|----------------|-------------------|-----------|---------|
| headless | 1002 | **0** | 3（全在前 5 秒） | 59.1 |
| headed | 1028 | **0** | 2（全在前 0.4 秒） | —（显示器无 vsync） |

**流式回复持续 30+ 秒、推送 1000+ 个 token，期间产生 0 个 >50ms 的长任务。**

仅有的几个长任务都在流式**开始前**的页面初始化阶段：
- `@106ms / @346ms`：进入会话、加载历史消息、首次渲染
- `@4580ms / @4789ms`：`stream_start` 首个 chunk 触发的首次 markdown 渲染

### 1.2 Chrome trace 火焰图归因（14.8 万事件）

| 函数 | 累计耗时 | 调用次数 | 平均 | 结论 |
|------|---------|---------|------|------|
| `ws.onmessage` | **199.6ms** | 947 | **0.21ms** | WS 消息处理极轻量 ✅ |
| `_flushChunks`（RAF 批处理） | 23.1ms | 20 | 1.15ms | **947 个 chunk 合并成 20 次写入（47:1）** ✅ |
| `MessageList.tsx` 渲染 | 1.8ms | 105 | 0.017ms | 列表渲染开销可忽略 ✅ |
| `Layout`（布局计算） | 43.4ms | 38 | — | 渲染管线不是瓶颈 ✅ |
| `Paint` | 22.5ms | 222 | — | 绘制开销极小 ✅ |
| `Commit` | 5.8ms | 39 | — | DOM 提交开销可忽略 ✅ |

**WebSocket 消息处理（947 次）总共只用了 0.2 秒主线程时间。这是最初怀疑被彻底推翻的铁证。**

### 1.3 WS 流量画像

- 总帧：949~1125 / 224KB
- 平均 payload：242B/帧，峰值 5330B（无大消息问题）
- 推送速率：22~27 帧/秒（适中，不是高频轰炸）
- 类型分布：`stream_chunk` 占 88%，`thinking_chunk` 占 12%

---

## 二、最初怀疑的 4 个热点，实测结论

| # | 怀疑点（代码审查时的判断） | 实测结论 | 证据 |
|---|--------------------------|---------|------|
| 1 | 流式期间每帧全量重渲染 Markdown | **❌ 不成立**（在工作负载下） | `MessageList.tsx` 渲染全程 1.8ms，`Layout`+`Paint`+`Commit` < 80ms |
| 2 | 消息列表无虚拟化 | **⚠️ 本次未触发** | 测试会话历史短；接近 300 条上限时才会暴露 |
| 3 | tool_result 的 LCS diff | **⚠️ 本次未触发** | 本次回复无大文件 diff；有 diff 时才会暴露 |
| 4 | `bumpWorkspaceDataVersion` 高频无效写入 | **⚠️ 本次未观测到明显影响** | 流式期间 0 长任务 |

**诚实说明**：热点 1 我在代码审查时判断错了——`MessageContentRenderer` 在 `isStreaming` 时 memo 失效确实存在，但 `@lobehub/ui` 的 Markdown 和 RAF 批处理配合下，**单次回复的实际渲染开销远低于预期**。代码审查是静态推断，实测才能定论。

---

## 三、为什么你"感觉"卡？可能的真实原因（实测之外）

既然消息链路被排除了，你感受到的卡顿可能来自**本次测试没覆盖**的场景：

### 3.1 长会话（消息接近 300 条上限）
- `pipelineMessageStore` 每次 `appendToPart`/`updatePart` 都 `[...pipelineMessages]` 整数组浅拷贝（`pipelineMessageStore.ts:966,993,1026`）
- 消息越多，单次拷贝越慢，且 `MessageList` 无虚拟化，全量 DOM
- **本次测试是刚进入会话，历史短。如果你在用了很久的会话里发消息，开销会显著放大**

### 3.2 多 Tab / Five-space 布局并发
- `bumpWorkspaceDataVersion` 在每个执行事件触发（`useRealtimeEvents.ts`）
- 如果开着 FileTree / 多个文件编辑器，3 秒轮询（`FiveSpaceLayout.tsx:113`）+ 流式渲染会争主线程
- **本次测试是单 tab 默认布局**

### 3.3 工具调用结果含大文件 diff
- `TextDiffView.tsx:37` 的 LCS 是 O(m×n)，无虚拟化
- **本次回复是纯文本+代码块，没触发 diff**

### 3.4 网络层
- 实测 WS 推送 22 帧/秒很平滑，但如果**后端偶尔卡顿**（某个 chunk 延迟几百毫秒然后突然涌来一批），RAF 批处理会一次性处理多个，可能产生瞬时压力
- 这种"脉冲式"卡顿需要**长时间录制**才能抓到

### 3.5 你访问的是生产构建还是 dev server？
- **本次测试是 vite dev server**（未压缩、含 HMR、esbuild 转译开销）
- 生产构建（`dist/`，已 minify + tree-shake）会更快
- 如果你平时用的是 dev server，切到生产构建可能就改善

---

## 四、如何复现我的测试

### 4.1 启动环境
```bash
# 1. 确保后端在 8989 跑着
# 2. 启动 vite dev server（用专门的探针 config，proxy 指向 8989，前端 base URL 留空走相对路径）
cd frontend
VITE_API_BASE_URL="" node_modules/.bin/vite --config vite.probe.config.ts &

# 3. 跑干净测量（推荐，无 tracing 干扰）
node scripts/probe_clean.mjs                    # headless
PROBE_HEADLESS=0 node scripts/probe_clean.mjs   # 有头，接近真实体验

# 4. 跑带 trace 的详细版（会慢，但能拿到火焰图）
node scripts/probe_jank.mjs

# 5. 分析 trace（找最耗时的函数）
python scripts/analyze_trace.py probe-out/jank-trace.json
python scripts/analyze_trace2.py probe-out/jank-trace.json   # 按 functionName 归类
```

### 4.2 环境说明
- 后端：`http://localhost:8989`（API + WS）
- 前端：`http://localhost:5290`（vite dev server，proxy 转发 /api /ws 到 8989）
- 账号：`admin` / `admin123`（来自 `.env` 的 `DEFAULT_ADMIN_PASSWORD`）
- 测试会话：复用已有的第一个会话（"+新会话"按钮在本环境点击无响应，需点已有会话）

### 4.3 关键约束
- **不能用 `frontend/.env` 里的 `VITE_API_BASE_URL=http://localhost:8988`**：端口是错的（应为 8989），且绝对 URL 会触发 CORS。用 `vite.probe.config.ts` 把它置空走代理。
- **"+新会话"按钮点击无效**（parent 0x0，可能是 sidebar 折叠态），测试脚本改为点击已有会话。

---

## 五、复测建议（如果你坚持要找到卡顿根因）

下次你觉得卡的时候，按这个顺序排查：

1. **确认是不是长会话**：新建一个会话发同样的消息，对比卡不卡。如果新会话不卡、旧会话卡 → 是热点 2（虚拟化/数组拷贝）。
2. **关掉 Five-space 布局和 FileTree**：用默认单 tab 布局，看是否还卡。如果不卡 → 是热点 4 / `FiveSpaceLayout` 轮询。
3. **看回复内容**：如果卡的那次回复带大段文件 diff / mermaid 图 → 是热点 3。
4. **用 dev 还是生产构建**：如果卡的是 dev server，试试 `npm run build && npm run preview` 用生产构建，看是否改善。
5. **如果以上都不是**：在卡的瞬间打开 DevTools Performance 录 10 秒，把火焰图里最宽的函数名告诉我。

---

## 附：本次测试产物

```
frontend/probe-out/
├── jank-report.json       # 完整运行报告（带 tracing，受干扰）
├── clean-report.json      # 干净测量报告（推荐参考）
├── jank-trace.json        # Chrome trace（34MB，可用 chrome://tracing 打开）
└── *.png                  # 调试截图

frontend/scripts/
├── probe_clean.mjs        # 轻量探针（推荐日常用）
├── probe_jank.mjs         # 带 CDP Tracing 的详细版
├── analyze_trace.py       # trace 函数耗时 Top 分析
└── analyze_trace2.py      # trace 按 functionName/url 归类
```
