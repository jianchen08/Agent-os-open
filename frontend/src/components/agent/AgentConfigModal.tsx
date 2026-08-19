/**
 * AgentConfigModal 组件
 *
 * Agent 管理页「编辑」入口：FormWidget modal 壳 + datasource 模式
 * （widget 化 T12，原 SchemaFormEmbed 已并入 FormWidget）——
 * fieldsUri 拉 /ext/agent_manager/agents/schema 字段声明，dataUri 拉/写回
 * /ext/agent_manager/agents/{id}/config yaml，保存成功自动关闭并回调刷新。
 * （2026-08-20 插件化：URI 经 API_ENDPOINTS.AGENTS 切 agent_manager，壳零改动）
 */

import { FormWidget } from '@/components/schema/widgets/FormWidget'
import { API_ENDPOINTS } from '@/constants/api'

/** AgentConfigModal 属性 */
export interface AgentConfigModalProps {
  /** 当前编辑的 Agent（null 时不渲染内容） */
  agent: { id: string; name?: string } | null
  /** 是否打开 */
  isOpen: boolean
  /** 关闭回调 */
  onClose: () => void
  /** 保存成功回调（列表页刷新） */
  onSaved?: () => void
}

/**
 * Agent 配置编辑模态框
 *
 * @param props - agent/isOpen/onClose/onSaved
 * @returns FormWidget modal 壳模态框
 */
export function AgentConfigModal({ agent, isOpen, onClose, onSaved }: AgentConfigModalProps) {
  if (!agent) return null
  return (
    <FormWidget
      key={agent.id}
      modal={{ title: `编辑配置 — ${agent.name ?? agent.id}` }}
      open={isOpen}
      onClose={onClose}
      fieldsUri={API_ENDPOINTS.AGENTS.SCHEMA}
      dataUri={API_ENDPOINTS.AGENTS.CONFIG(agent.id)}
      dataFormat="yaml"
      submitLabel="保存配置"
      onSaved={() => onSaved?.()}
    />
  )
}

export default AgentConfigModal
