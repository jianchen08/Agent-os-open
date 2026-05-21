# 消息向上滚动加载功能修复报告

## 问题描述

前端消息向上滚动加载更早消息功能完全失效，用户滑动到顶部时无法触发加载历史消息。

## 根因分析

三个 Bug 联合导致功能失效：

| Bug | 文件 | 严重性 | 描述 |
|-----|------|--------|------|
| Bug 1 | MessageList.tsx | 🔴 致命 | onScroll 回调中 `isScrolling` 硬编码为 true，但触发条件要求 `!isScrolling`，导致 onLoadMore 永远不触发 |
| Bug 2 | router.tsx | 🔴 致命 | 分页状态用 `getState()` 非响应式读取，且使用 activeSessionId 而非 pipelineId 作为 key |
| Bug 3 | pipelineMessageStore.ts | 🟡 严重 | fetchMessages 未在 before_sequence 请求前设置 loading 状态 |

## 修复方案

### Bug 1 - MessageList.tsx
- **文件**: `frontend/src/components/chat/MessageList.tsx`
- **修改**: 移除 `!isScrolling` 条件，触发条件从 `if (atTop && hasMore && !isLoadingMore && onLoadMore && !isScrolling)` 改为 `if (atTop && hasMore && !isLoadingMore && onLoadMore)`

### Bug 2 - router.tsx
- **文件**: `frontend/src/router.tsx`
- **修改**: 分页状态从 `getState()` 直接读取改为 Zustand hook selector 响应式读取，并以 `(activePipelineId || activeSessionId)` 为 key

### Bug 3 - pipelineMessageStore.ts
- **文件**: `frontend/src/stores/pipelineMessageStore.ts`
- **修改**: fetchMessages 中发起 before_sequence 请求前设置 `isLoadingOlderByPipeline[pipelineId] = true`

## 执行流程

| 阶段 | Agent | 任务ID | 结果 |
|------|-------|--------|------|
| A1 编码修复 | code_writer_agent | e89bbd0aab5b | ✅ 通过（6/6 指标） |
| A3 测试 | test_debug_agent | 04b9dcdb6a83 | ✅ 通过 |
| A4 功能验证 | function_verifier_agent | 52b9e73b0003 | ✅ 通过 |

## 产出物清单

- `frontend/src/components/chat/MessageList.tsx`（修改）
- `frontend/src/router.tsx`（修改）
- `frontend/src/stores/pipelineMessageStore.ts`（修改）
- `tests/test_message_scroll_loading.tsx`（测试文件）
- `function_verify_report.md`（功能验证报告）

## 知识沉淀

- **Zustand getState() 陷阱**：在 React 组件中使用 `getState()` 读取的值是一次性快照，不会触发重新渲染。必须使用 hook selector `useStore((s) => s.value)` 实现响应式更新。
