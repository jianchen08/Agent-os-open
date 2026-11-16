/**
 * 会话对话标签激活 → 模式面板自动弹出（模式体系 §5.3 会话态规则的自动化落点）
 *
 * 数据源 = 共享 pipelineStates query 缓存（GET /api/v1/pipelines/state 的前端
 * 镜像，30s 轮询 + WS 事件失效化，由 ContextUsageWidget 等常驻消费方保持新鲜），
 * 只读缓存不发新请求。管道 state.mode 非空 → 经 modePanel 聚合解析 workspace/tab
 * 面板页声明 → openPluginPage 打开/激活面板页签（openWorkspacePanel 按 id 幂等：
 * 已开 = 激活，未开 = 新开）。无缓存 / 无 mode 键 / 无匹配声明 → 不动作
 * （与模式徽标「查不到映射 = 不渲染」同源语义）。
 *
 * 弹出策略随插件声明（页面扩展字段 `autoOpen`，与 `mode` 同走声明透传）：
 * 声明 autoOpen:false = 该页不参与「对话标签激活自动弹出」；候选制解析在
 * 愿弹页里取 order 最小者，全员选退 = 该模式自动弹出整体关闭（解析规则见
 * modePanel.getModeAutoOpenTarget）。显式入口（openPluginPage / 模式选择器
 * 点名出生）不受选退约束。缺省 = 弹（既有六模式声明零变化）。宿主零模式
 * 知识，弹不弹由各插件声明自持。
 */

import { mapStateInfoToViewModel, type PipelineStateInfo } from '@/services/api/pipelines'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'
import { getModeAutoOpenTarget } from '@/services/schema/modePanel'
import { openPluginPage } from '@/services/workspacePanelOpener'

/** 对话标签激活时按管道 mode 自动弹出模式面板（无 mode/无声明时零动作；候选制——只在愿弹页里取 order 最小者） */
export function autoOpenModePanel(pipelineId: string | undefined): void {
  if (!pipelineId) return
  const info = queryClient.getQueryData<Record<string, PipelineStateInfo>>(
    queryKeys.pipelineStates,
  )?.[pipelineId]
  if (!info) return
  const modeTags = mapStateInfoToViewModel(info).modeTags ?? []
  if (!modeTags.length) return
  // 标签集解析：主标签（末位=最新激活）优先，向前找第一个命中愿弹页的标签；
  // 单标签集与旧单键行为逐字节一致
  for (let i = modeTags.length - 1; i >= 0; i--) {
    const target = getModeAutoOpenTarget(modeTags[i])
    if (target) {
      openPluginPage(target)
      return
    }
  }
}
