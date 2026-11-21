/**
 * 对话标签状态语义单点（P1-7 移动端视觉优化：MuMu 实测 shots/09 等标题 chip
 * 「● 灵汐 0」「✕ 灵汐 0」「✓ hi 0」符号含义不明——●✓✕ 是什么、全靠猜）。
 *
 * 语义表（状态词 = 用户可读口径，符号 → 状态映射由本表唯一承载，渲染面与
 * 单测共用；台账同表归档）：
 * - running      「运行中」   主色点（呼吸）  = 管道正在执行
 * - completed    「已完成」   成功色点        = 最近一轮正常结束
 * - waiting_input「等待输入」 警告色点（呼吸）= 等用户输入/审批回来继续
 * - failed       「失败」     红色点          = 最近一轮失败/中断
 * - unknown      「待运行」   中性灰点        = 尚无运行数据（新开签未跑首行）
 *
 * 注：标签名本身 = 管道标题（数据面），「0」等尾缀是会话标题数据自带的
 * （现役前端会话标题只产「灵汐」/首条消息截断，无计数器），本表不处理标题
 * 数据，只承载「状态 → 可读语义」映射。
 */

import type { AgentTabStatus } from '@/types/task'

export interface TabStatusSemantic {
  /** 状态词（可读，移动端 chip 内随名显示；桌面仍用符号位） */
  label: string
  /** 状态点颜色类（Tailwind bg-*） */
  dotClass: string
  /** 状态点呼吸动画（进行中语义） */
  pulsing: boolean
}

export const TAB_STATUS_SEMANTICS: Record<AgentTabStatus, TabStatusSemantic> = {
  running: { label: '运行中', dotClass: 'bg-primary', pulsing: true },
  completed: { label: '已完成', dotClass: 'bg-success', pulsing: false },
  waiting_input: { label: '等待输入', dotClass: 'bg-warning', pulsing: true },
  failed: { label: '失败', dotClass: 'bg-destructive', pulsing: false },
  unknown: { label: '待运行', dotClass: 'bg-muted-foreground', pulsing: false },
}

/** 状态 → 语义（未知值回退 unknown 档，渲染面零分支） */
export function tabStatusSemantics(status: AgentTabStatus | undefined): TabStatusSemantic {
  return (status && TAB_STATUS_SEMANTICS[status]) || TAB_STATUS_SEMANTICS.unknown
}
