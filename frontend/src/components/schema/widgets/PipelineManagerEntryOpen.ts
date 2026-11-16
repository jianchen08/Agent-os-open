/** 任务条目「打开对话」统一跳转逻辑（自 PipelineManagerWidget 提取，行为零变化）
 *
 * 全部状态经 getState()/readSessions() 在点击时刻读取（无组件闭包依赖），
 * 可作模块级函数由 widget 直接引用。
 */

import { readSessions, ensureSessionsLoaded } from '@/hooks/queries/useSessionsQuery'
import { navigateToPipeline } from '@/services/pipelineNavigator'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { useSessionListStore } from '@/stores/sessionListStore'
import { useSessionStore } from '@/stores/sessionStore'
import { pipelineStatusToTabStatus } from './pipelineStatusVisuals'
import type { PipelineViewEntry } from '@/types/pipeline'

/** 打开对话（统一跳转逻辑）：先确认管道归属会话（会话列表未加载时先拉取，
 *  避免误判无归属而在当前会话新建标签），有归属 → 定位/跳转到对应会话标签
 *  （主管道=主标签、子管道=已有子标签跳转/无则新建），无归属 → 创建独立标签。 */
export async function openEntryConversation(entry: PipelineViewEntry): Promise<void> {
  // 项目 = 文件夹 + 登记行，无管道无会话（ADR 2026-08-27）——没有可打开的对话
  if (entry.kind === 'project') return
  const pipelineId = entry.pipelineId || entry.key
  // 会话列表未加载时先拉取；拉取失败即阻断跳转——继续执行会把有归属管道
  // 误判为孤儿而在当前会话新建重复标签（L540 注释要避免的正是这个后果）。
  if (readSessions().length === 0) {
    try {
      await ensureSessionsLoaded()
    } catch (e) {
      console.error('[PipelineManager] 会话列表拉取失败，中止定位', e)
      useNotificationStore.getState().addNotification({
        title: '无法定位对话',
        message: `会话列表加载失败，请稍后重试（管道 ${pipelineId.slice(0, 12)}）`,
        priority: 'normal',
        category: 'alert',
        isBlocking: false,
        autoDismissMs: 5000,
        sourceLabel: '系统',
      })
      return
    }
  }
  // 归属会话解析：threadId 命中会话列表为第一真值。自环子任务管道
  // （thread_id=自身 id）不在任何会话成员列表里——runs 读面联结出的
  // thread_id 不会命中会话列表，真实归属会话经血缘 origin_session_id
  // 兜底（出生写面真值，指向根用户会话）。
  // 待办（解耦方案 P2-6）：内核 runs/state 响应直接携带归属会话字段后，
  // 删除本兜底分支。
  const sessionsNow = readSessions()
  let owningSessionId: string | undefined
  let ownsViaLineage = false
  if (entry.threadId && sessionsNow.some((s) => s.id === entry.threadId)) {
    owningSessionId = entry.threadId
  } else if (
    entry.originSessionId
    && sessionsNow.some((s) => s.id === entry.originSessionId)
  ) {
    owningSessionId = entry.originSessionId
    ownsViaLineage = true
  }
  if (owningSessionId) {
    const ok = await navigateToPipeline(pipelineId, {
      agentName: entry.name,
      agentLevel: 2,
      taskId: entry.taskId,
      status: entry.status,
      // 自环子任务管道在任何会话 pipelineIds 里都查不到——血缘会话作定位
      // 提示传入，导航器切到归属会话开子标签（不在当前会话落孤儿标签）
      fallbackSessionId: ownsViaLineage ? owningSessionId : undefined,
    }).catch((e) => {
      // 记日志并返回 false：导航失败走下方 setActiveSession 归属会话兜底链
      // （OBS-R258-1 吞错误规则登记）
      console.error('[PipelineManager] 管道导航失败', e)
      return false
    })
    if (!ok) {
      // navigateToPipeline 在无活跃会话等场景会拒绝——直接切换到管道所属会话
      // （setActiveSession 会激活主管道标签并加载消息，等价于"打开对应会话标签"）
      await useSessionListStore
        .getState()
        .setActiveSession(owningSessionId)
        .catch((e) => {
          console.error('[PipelineManager] 切换会话失败', e)
          useNotificationStore.getState().addNotification({
            title: '无法打开对话',
            message: `管道 ${pipelineId.slice(0, 12)} 无归属会话或会话不存在`,
            priority: 'normal',
            category: 'alert',
            isBlocking: false,
            autoDismissMs: 5000,
            sourceLabel: '系统',
          })
        })
    }
    return
  }
  // 无会话归属：Tab 条随 ChatContainer 挂载（router 按 activeSessionId 渲染
  // 聊天区），无活跃会话时子标签无处渲染、loadTabMessages 也无从发起——
  // 点击自此静默无反应（2026-10-13 装机版实证），禁止静默：
  // - 带 thread 线索（threadId / 血缘 originSessionId）→ 会话记录缺失（如
  //   清空过会话数据）但消息内核侧仍按 thread_id 可读：从 thread 重建会话
  //   条目并切换打开（主管道绑本条目管道，消息即任务对话）；
  // - 真孤儿（无任何会话线索）→ 显式反馈，不伪装成功。
  if (!useSessionStore.getState().activeSessionId) {
    const rebuildId = entry.threadId ?? entry.originSessionId
    if (rebuildId) {
      useSessionListStore.getState().rebuildSessionFromThread(rebuildId, {
        title: entry.name,
        pipelineId,
      })
      await useSessionListStore
        .getState()
        .setActiveSession(rebuildId)
        .catch((e) => {
          console.error('[PipelineManager] 重建会话打开失败', e)
          useNotificationStore.getState().addNotification({
            title: '无法打开对话',
            message: `会话 ${rebuildId.slice(0, 12)} 重建打开失败，请稍后重试（管道 ${pipelineId.slice(0, 12)}）`,
            priority: 'normal',
            category: 'alert',
            isBlocking: false,
            autoDismissMs: 5000,
            sourceLabel: '系统',
          })
        })
      return
    }
    useNotificationStore.getState().addNotification({
      title: '无法打开对话',
      message: `管道 ${pipelineId.slice(0, 12)} 无归属会话线索，且当前无活跃会话——请先在侧栏选择或新建会话后再试`,
      priority: 'normal',
      category: 'alert',
      isBlocking: false,
      autoDismissMs: 6000,
      sourceLabel: '系统',
    })
    return
  }
  // 无会话归属（有活跃会话承载子标签）：不定位，直接跳转打开对应标签页
  const tabStore = useAgentTabStore.getState()
  const existingTab = tabStore.tabs.find((t) => t.pipelineRunId === pipelineId)
  if (existingTab) {
    tabStore.switchToTab(existingTab.id)
    return
  }
  const tabId = `sub-${pipelineId}`
  tabStore.openSubAgentTab({
    agentId: entry.taskId || pipelineId,
    agentName: entry.name,
    parentRecordId: pipelineId,
    agentLevel: 2,
    taskId: entry.taskId,
    status: pipelineStatusToTabStatus(entry.status ?? 'unknown'),
    setActive: true,
    pipelineId,
  })
  tabStore.loadTabMessages(tabId, pipelineId)
}
