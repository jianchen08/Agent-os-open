/**
 * agent 图标解析（单一真值）：`config/agents/**\/*.yaml` 的 `icon` 字段 →
 * 可渲染的图标规格。任务管理面板行内用图标替代 agent 名字文字（名字转
 * tooltip / 详情），图标与名字同源解析，不在多处各自维护。
 *
 * 优先级（用户定的机制，从高到低）：
 * 1. emoji（`icon: "🧠"`）：原样作为文本渲染，零资源文件，换主题不失效；
 * 2. 内置图标名（`icon: "brain"`）：解析到本文件 AGENT_BUILTIN_ICONS 映射的
 *    前端图标集组件（小写键，大小写不敏感）；
 * 3. 未配置 / 空值 / 未知名：缺省图标兜底，**不留空**。
 *
 * 文件路径形态（svg/png）暂不支持：要处理打包资源与相对路径，易踩加载失败，
 * 收益低于 emoji。
 */
import {
  BookOpen,
  Bot,
  Brain,
  Code,
  Database,
  Globe,
  Plug,
  Search,
  Settings,
  ShieldCheck,
  Sparkles,
  Target,
  Terminal,
  Users,
  Wrench,
  Zap,
  type LucideIcon,
} from '@/assets/icons'

/** 缺省图标（未配置 / 未知值兜底）——单一真值，调用方不各写一个 */
export const DEFAULT_AGENT_ICON: LucideIcon = Bot

/** 内置图标名 → 图标组件（小写键；新增可用名只改这张表） */
export const AGENT_BUILTIN_ICONS: Record<string, LucideIcon> = {
  book: BookOpen,
  bot: Bot,
  brain: Brain,
  code: Code,
  database: Database,
  globe: Globe,
  plug: Plug,
  search: Search,
  settings: Settings,
  shield: ShieldCheck,
  sparkles: Sparkles,
  target: Target,
  terminal: Terminal,
  users: Users,
  wrench: Wrench,
  zap: Zap,
}

/** 图标渲染规格：emoji 走文本，其余走组件 */
export type AgentIconSpec =
  | { kind: 'emoji'; value: string }
  | { kind: 'builtin'; Icon: LucideIcon }
  | { kind: 'default'; Icon: LucideIcon }

/** emoji 判定（扩展象形字符集；覆盖 🧠🔍🛠 等常见图标 emoji） */
const EMOJI_PATTERN = /\p{Extended_Pictographic}/u

/**
 * `icon` 配置值 → 图标规格。
 * @param raw 配置文件里的原始值（可缺失）
 * @returns emoji / 内置组件 / 缺省组件三选一，永不为空
 */
export function resolveAgentIcon(raw: string | undefined | null): AgentIconSpec {
  const trimmed = (raw ?? '').trim()
  if (!trimmed) return { kind: 'default', Icon: DEFAULT_AGENT_ICON }
  if (EMOJI_PATTERN.test(trimmed)) return { kind: 'emoji', value: trimmed }
  const key = trimmed.toLowerCase()
  // 显式属主判定：图标集映射的值是组件（函数），对函数做真值判断会被
  // TS 判为恒真，且能挡住 'toString' 这类原型链键误命中。
  if (Object.prototype.hasOwnProperty.call(AGENT_BUILTIN_ICONS, key)) {
    return { kind: 'builtin', Icon: AGENT_BUILTIN_ICONS[key] }
  }
  return { kind: 'default', Icon: DEFAULT_AGENT_ICON }
}

/** agents 注册表最小形状（id/configId 双键 + icon，够本模块解析） */
export interface AgentIconSource {
  id?: string
  configId?: string
  icon?: string
}

/**
 * agent 身份 → 配置里的原始 `icon` 值（未配置返回 undefined，交缺省兜底）。
 *
 * 与 `resolveAgentLabel` 同一查找口径（id/config_id 双键匹配）：名字与图标
 * 必须命中同一条注册表行，不得各查一套。
 * @param agentId 执行身份（state agent.id / 任务快照 / 会话归属）
 * @param agents agents 注册表
 */
export function resolveAgentIconValue(
  agentId: string | undefined,
  agents: ReadonlyArray<AgentIconSource>,
): string | undefined {
  const trimmed = agentId?.trim()
  if (!trimmed) return undefined
  const hit = agents.find((a) => a.id === trimmed || a.configId === trimmed)
  const icon = hit?.icon?.trim()
  return icon || undefined
}
