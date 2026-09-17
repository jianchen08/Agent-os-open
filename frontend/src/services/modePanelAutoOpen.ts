/**
 * 会话对话标签激活 → 模式面板自动弹出（模式体系 §5.3 会话态规则的自动化落点）
 *
 * 数据源 = 共享 pipelineStates query 缓存（GET /api/v1/pipelines/state 的前端
 * 镜像，30s 轮询 + WS 事件失效化，由 ContextUsageWidget 等常驻消费方保持新鲜），
 * 只读缓存不发新请求。管道 state.mode 非空 → 经 modePanel 聚合解析 workspace/tab
 * 面板页声明 → openPluginPage 打开/激活面板页签（openWorkspacePanel 按 id 幂等：
 * 已开 = 激活，未开 = 新开）。无缓存 / 无 mode 键 / 无匹配声明 → 不动作
 * （与模式徽标「查不到映射 = 不渲染」同源语义）。
 */

import { mapStateInfoToViewModel, type PipelineStateInfo } from '@/services/api/pipelines'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'
import { getModePanelTarget } from '@/services/schema/modePanel'
import { openPluginPage } from '@/services/workspacePanelOpener'

/** 对话标签激活时按管道 mode 自动弹出模式面板（无 mode/无声明时零动作） */
export function autoOpenModePanel(pipelineId: string | undefined): void {
  if (!pipelineId) return
  const info = queryClient.getQueryData<Record<string, PipelineStateInfo>>(
    queryKeys.pipelineStates,
  )?.[pipelineId]
  if (!info) return
  const { mode } = mapStateInfoToViewModel(info)
  if (!mode) return
  const target = getModePanelTarget(mode)
  if (!target) return
  openPluginPage(target)
}
