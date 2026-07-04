# 前端卡顿诊断指南

> 用途：定位"接收消息 / 流式输出期间，页面（聊天页、工作区、监控页等）卡顿延迟"的真实根因。
>
> 结论先行：**接收消息不会"查死"其他页面**——WebSocket 是单例、消息只写 `pipelineMessageStore`、非聊天页面零订阅消息 store、路由互斥。卡顿几乎肯定来自**流式渲染期间的主线程占用**，本指南帮你精确找到是哪一处。

---

## 0. 怀疑的 4 个热点（按可能性排序）

| # | 热点 | 文件:行 | 症状 |
|---|------|---------|------|
| 1 | 流式期间每帧全量重渲染 Markdown（memo 失效） | `components/chat/MessageContentRenderer.tsx:198` | 回复越长越卡，最后一个气泡在"刷" |
| 2 | 消息列表无虚拟化（上限 300 条全量渲染） | `components/chat/MessageList.tsx:53` | 历史长会话滚动/切换卡 |
| 3 | tool_result 的 LCS diff 是 O(m×n) 且无虚拟化 | `components/approval/TextDiffView.tsx:37` | 工具调用结果一到大文件就卡 |
| 4 | `bumpWorkspaceDataVersion` 高频无效写入 | `hooks/useRealtimeEvents.ts:148,193,257,269` | 工作区/FileTree 在流式期间莫名重渲染 |

诊断目的就是用数据把它们**确证或排除**。

---

## 1. Console 探针脚本（5 秒装好，立刻看到数据）

### 1.1 用法
1. 打开应用，登录，进到聊天页（HomePage）
2. F12 打开 DevTools → **Console** 标签
3. 粘贴下面 §1.2 的整段代码回车
4. 然后**正常发起一次会话**（让 AI 回复一段较长的内容，最好带代码块）
5. 观察 Console 实时输出，结束后输入 `stopJankProbe()` 停止并打印汇总

### 1.2 探针代码

```javascript
// ===== 前端卡顿诊断探针 v1 =====
// 不会修改任何业务逻辑，只读。结束时调 stopJankProbe() 还原。
(function () {
  if (window.__jankProbeActive) {
    console.warn('[probe] 已在运行，先执行 stopJankProbe()');
    return;
  }
  window.__jankProbeActive = true;

  const startedAt = performance.now();
  const stats = {
    wsMessages: 0,            // WS 消息总数
    wsByType: {},             // 按 type 分组
    storeWrites: {},          // 各 store 的 setState 次数
    longTasks: [],            // 长任务 (>50ms)
    frames: 0,                // rAF 帧数
    jankyFrames: 0,           // 间隔 >25ms 的"丢帧"
    wsMessageSizes: [],       // WS payload 字节数样本（前 200 条）
  };
  const _backups = {};

  // ---------- 1) WebSocket onmessage 计数 ----------
  // 包装原生 WebSocket.prototype.send/构造，统计进入的消息频率与体积
  const OrigWS = window.WebSocket;
  const wsCounts = stats.wsByType;
  function WrappedWS(url, protocols) {
    const ws = protocols ? new OrigWS(url, protocols) : new OrigWS(url);
    const origOnMsg = Object.getOwnPropertyDescriptor(
      WebSocket.prototype, 'onmessage'
    );
    let userHandler = null;
    Object.defineProperty(ws, 'onmessage', {
      configurable: true,
      get() { return userHandler; },
      set(fn) {
        userHandler = function (ev) {
          try {
            const raw = ev.data;
            const size = typeof raw === 'string' ? raw.length : (raw?.byteLength || 0);
            if (stats.wsMessages < 200) stats.wsMessageSizes.push(size);
            stats.wsMessages++;
            try {
              const parsed = JSON.parse(raw);
              if (parsed && parsed.type) {
                wsCounts[parsed.type] = (wsCounts[parsed.type] || 0) + 1;
              }
            } catch (_) {}
          } catch (_) {}
          if (fn) return fn.call(this, ev);
        };
      },
    });
    return ws;
  }
  WrappedWS.prototype = OrigWS.prototype;
  WrappedWS.CONNECTING = OrigWS.CONNECTING;
  WrappedWS.OPEN = OrigWS.OPEN;
  WrappedWS.CLOSING = OrigWS.CLOSING;
  WrappedWS.CLOSED = OrigWS.CLOSED;
  // 注意：只对“未来”新建的 WS 生效；已有连接（globalWS）不会被拦截。
  // 因此 WS 计数主要反映“连接建立后”的流量。要测当前连接，见 §1.3。
  window.WebSocket = WrappedWS;

  // ---------- 2) 长任务监控 ----------
  if ('PerformanceObserver' in window && window.PerformanceObserver) {
    try {
      const po = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          if (entry.duration > 50) stats.longTasks.push({
            t: Math.round(entry.duration),
            name: entry.name,
            at: Math.round(entry.startTime),
          });
        }
      });
      po.observe({ entryTypes: ['longtask'] });
      _backups.po = po;
    } catch (_) {}
  }

  // ---------- 3) 帧率 & 丢帧 ----------
  let lastFrame = performance.now();
  function frameLoop() {
    if (!window.__jankProbeActive) return;
    stats.frames++;
    const now = performance.now();
    const gap = now - lastFrame;
    if (gap > 25) {
      stats.jankyFrames++;
      if (gap > 100 && stats.longTasks.length < 500) {
        // 没有 longtask 支持时，用 rAF gap 兜底记录
        stats.longTasks.push({ t: Math.round(gap), name: 'raf-gap', at: Math.round(now - startedAt) });
      }
    }
    lastFrame = now;
    requestAnimationFrame(frameLoop);
  }
  requestAnimationFrame(frameLoop);

  // ---------- 4) 每 2 秒打印一次实时摘要 ----------
  const summaryTimer = setInterval(() => {
    const dur = ((performance.now() - startedAt) / 1000).toFixed(1);
    const fps = stats.frames / Math.max(0.1, (performance.now() - startedAt) / 1000);
    const topTypes = Object.entries(stats.wsByType)
      .sort((a, b) => b[1] - a[1]).slice(0, 6).map(([k, v]) => `${k}=${v}`).join(' ');
    console.log(
      `[probe ${dur}s] WS msgs=${stats.wsMessages} | fps≈${fps.toFixed(0)} jankyFrames=${stats.jankyFrames} longTasks(>50ms)=${stats.longTasks.length}\n   WS by type: ${topTypes || '—'}`
    );
  }, 2000);
  _backups.summaryTimer = summaryTimer;

  // ---------- 5) 停止 & 汇总 ----------
  window.stopJankProbe = function () {
    window.__jankProbeActive = false;
    clearInterval(_backups.summaryTimer);
    if (_backups.po) _backups.po.disconnect();
    window.WebSocket = OrigWS;

    const dur = ((performance.now() - startedAt) / 1000).toFixed(1);
    const sizes = stats.wsMessageSizes;
    const avgSize = sizes.length ? Math.round(sizes.reduce((a, b) => a + b, 0) / sizes.length) : 0;
    const maxSize = sizes.length ? Math.max(...sizes) : 0;
    const longBuckets = {};
    stats.longTasks.forEach((t) => {
      const bucket = t.t >= 500 ? '500ms+' : t.t >= 200 ? '200-500ms' : '100-200ms' : '50-100ms';
      longBuckets[bucket] = (longBuckets[bucket] || 0) + 1;
    });

    console.log('%c========== 卡顿诊断汇总 ==========', 'color:#0a0;font-weight:bold');
    console.log(`运行时长: ${dur}s`);
    console.log(`WS 消息总数: ${stats.wsMessages}`);
    console.log(`  平均 payload: ${avgSize}B, 最大: ${maxSize}B`);
    console.log('  按 type 分布:', stats.wsByType);
    console.log(`帧率统计: 总帧=${stats.frames}, 丢帧(>25ms)=${stats.jankyFrames}`);
    console.log(`长任务(>50ms): ${stats.longTasks.length} 次`);
    console.log('  按耗时分布:', longBuckets);
    console.log('  Top 10 最长任务:');
    stats.longTasks.slice().sort((a, b) => b.t - a.t).slice(0, 10)
      .forEach((t, i) => console.log(`    ${i + 1}. ${t.t}ms  ${t.name}  @${t.at}ms`));
    console.log('%c下一步: 录制 Performance（见诊断指南 §2）定位具体函数', 'color:#06c');
    console.log('%c判断矩阵见诊断指南 §3', 'color:#06c');
  };

  console.log('%c[probe] 已启动。现在去聊天页发消息触发流式回复。结束后执行 stopJankProbe()。', 'color:#0a0');
})();
```

### 1.3 如果 WS 计数为 0（探针没拦到现有连接）

`globalWS` 在登录时就建好了连接，探针拦不到。两种办法：

**办法 A（推荐）**：贴完探针后，**断开网络再连上**（或刷新页面）触发重连，探针就能拦到新连接。

**办法 B**：直接订阅 globalWS 的通配事件（如果项目暴露了）。在 Console 跑：
```javascript
// 备份并包装 globalWS 的内部 _emit（如果可访问）
// 这个方法依赖实现细节，可能不可用，优先用办法 A
```

---

## 2. Performance 面板录制（精确定位函数）

Console 探针能告诉你"**有没有**卡、卡多狠、是不是 WS 高频"，但**不知道是哪个函数**在吃 CPU。这要靠 Performance 录制。

### 2.1 录制步骤
1. DevTools → **Performance** 标签（Edge/Chrome）或 **录制性能**（Firefox）
2. 点左上角 **● Record**（实心圆）
3. **立刻**在聊天页发一条消息，让 AI 开始流式回复一段较长内容（带代码块最好）
4. 等 AI 回复 10~15 秒，**点 Stop**
5. 录制时间控制在 **15~20 秒**，太长会很难读

### 2.2 看哪里（按优先级）

#### 🔍 看火焰图（Main 线程）里这些"宽条"
- **`markdown` / `parse` / `tokenize`** → 命中热点 1（markdown 全量重渲染）
- **`appendPart` / `updateMessage` / `[...pipelineMessages]`**（带 spread 的 setState）→ store 数组浅拷贝开销
- **`LCS` / `computeDiff` / `TextDiffView`** → 命中热点 3
- **`react-syntax-highlighter` / `Prism` / `highlight`** → 代码高亮
- **`mermaid` / `render` / `getBBox`** → mermaid 图渲染

#### 🔍 看 "Bottom-Up"（自底向上）标签
按 **Self Time** 排序，排在前面的函数就是真正的 CPU 消耗者。如果看到大量时间花在：
- React 的 `commitWork` / `reconcileChildren` → 是**渲染**开销（热点 1 或 2）
- 业务函数（如 `computeDiff`、`preprocessSvgCodeBlocks`）→ 是**计算**开销（热点 1 的预处理 / 热点 3）

#### 🔍 看 "Interactions" / "FPS" 轨道
- 黄色长条 = 主线程被占用，期间用户点击/输入都会延迟
- 如果黄条和 `stream_chunk` / `appendToPart` 的时间点对齐，就是流式渲染在阻塞

### 2.3 录制小技巧
- 录制前**关掉 React DevTools**（它会让渲染慢 2~3 倍）
- 录制前**关掉 Console 的 "Log XMLHTTPRequest"** 之类，减少噪音
- 多录几次取稳定的，单次偶发毛刺不算数

---

## 3. 判断矩阵（拿到数据后怎么对应热点）

### 3.1 根据 Console 探针输出

| 观察到的现象 | 最可能的热点 | 验证方式 |
|--------------|-------------|----------|
| `stream_chunk` 数量 > 500 次/10秒，但 fps 正常 | 正常（RAF 批处理在工作） | 无需处理 |
| `stream_chunk` 高频 **且** jankyFrames 同步飙升 | 热点 1（markdown 重渲染） | 看 Performance 里 `markdown parse` 宽条 |
| WS 消息不多，但 longTask 还是很多 | 热点 2（列表 reconcile）或热点 3（diff） | 看 Performance 里 `commitWork` 宽条 |
| WS payload 平均 > 10KB | 后端单条太大，parse 有压力 | 看后端是否在塞大 tool_result |
| 完全没有流式、只是切换页面就卡 | 与 WS 无关，是路由/页面本身重 | 单独录那个页面的 Performance |

### 3.2 根据 Performance 火焰图

| 火焰图里看到的宽函数 | 对应热点 | 修复方向 |
|---------------------|---------|---------|
| `markdown parse / Streamdown / @lobehub` 每帧都出现 | 热点 1 | 改 memo 让流式期间只 diff 末段 |
| `commitWork / reconcileChildren` 在大消息树上 | 热点 2 | 虚拟化（react-window） |
| `computeDiff` 单次几十~几百 ms | 热点 3 | diff 虚拟化 / Web Worker / 懒算 |
| `setState → bumpWorkspaceDataVersion` 高频 | 热点 4 | 删除无效写入 |

---

## 4. 复现"其他页面卡顿"的对照实验

如果你坚持认为"接收消息会让其他页面卡"，用这个对照实验排除/确证：

1. **实验组**：在聊天页发起一个会话，AI 流式回复**进行中**时，**快速切到监控页/设置页**，感受卡顿程度。
2. **对照组**：**不**发任何消息（WS 空闲），切同样的页面，感受卡顿程度。

**预期结果**（基于架构分析）：
- 因为切到非聊天页时 **HomePage 会卸载**，`useRealtimeEvents` 和 `initStreamingEvents` 都会被销毁，WS 消息**不再触发任何 store 写入**，所以"实验组"应该和"对照组"一样流畅。
- 如果实验组明显更卡 → 说明 HomePage 卸载不彻底（可能有全局订阅泄漏），这是需要查的新问题，请把实验组时录的 Performance 发我。
- 如果两组一样 → 证实卡顿**只发生在你停留在聊天页、且正在流式输出时**，根因是热点 1~4，与"影响其他页面"无关。

---

## 5. 常见误判澄清

- **"消息多=卡"** 不一定。流式 token 多但走了 RAF 批处理就还好；真正的卡来自**渲染层**对每次 token 的响应。
- **"WebSocket 慢=卡"** 错。WS 只是数据源，卡的是拿到数据后**主线程上的渲染/计算**。WS 本身从不阻塞渲染。
- **"切到别的页面还卡=别的页面有问题"** 不一定。很可能是切页**之前**聊天页累积的渲染任务还在主线程队列里没消化完。用 Performance 录"切页那一刻"能看到。

---

## 附：探针安全说明

- 探针**只读**，不改业务状态，刷新页面即彻底还原。
- 唯一改动是临时包装 `window.WebSocket` 构造函数，`stopJankProbe()` 会还原。
- 长任务/帧率监控用的是浏览器原生 API，无副作用。
