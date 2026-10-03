/**
 * 呈现档案作用域（MessageItem 与消息卡组件共享）：
 * PresenterContext 单向投递（PresenterScope → 头像槽/徽标/工具卡叙事），
 * 附身档 → 呈现档案转换。
 */
import { createContext, type ReactNode, useContext } from 'react'
import { usePresenterProfile, type PresenterProfile } from '@/services/api/presenterProfiles'
import type { PersonaPossession } from '@/services/schema/modeOptions'

/** 呈现档案上下文（PresenterScope → 头像槽/徽标的单向投递） */
export const PresenterContext = createContext<PresenterProfile | null>(null)

/**
 * 附身档 → 呈现档案：附身是临时态（消息本体身份仍是主 agent），仅投递
 * {name, avatar} 供头像槽/徽标消费；origin 标 possess 以区分卡目录解析来源。
 */
export function possessionToPresenter(possessed: PersonaPossession): PresenterProfile {
  return { name: possessed.name, avatar: possessed.avatar, origin: 'possess' }
}

/**
 * 呈现档案作用域：assistant 消息带 agentId 或附身激活时挂载（挂载条件由
 * 调用方裁定）。呈现优先级：消息自带模式卡键 → presenter 解析优先（卡身份是
 * 消息的永久属性）；presenter 未命中且附身激活 → 附身档覆盖（解除即回退）。
 * agentId 缺席时 usePresenterProfile 恒 null（零请求）。
 */
export function PresenterScope({
  agentId,
  possessed,
  children,
}: {
  agentId: string | undefined
  possessed: PersonaPossession | null
  children: ReactNode
}) {
  const presenter = usePresenterProfile(agentId)
  const profile = presenter ?? (possessed ? possessionToPresenter(possessed) : null)
  return <PresenterContext.Provider value={profile}>{children}</PresenterContext.Provider>
}

/** 呈现态读取（卡内组件消费：未命中 = 非呈现态，走默认形态） */
export function usePresenter(): PresenterProfile | null {
  return useContext(PresenterContext)
}
