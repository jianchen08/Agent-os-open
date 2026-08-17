/**
 * 交互模式布局声明（widget 化 T9）：interaction mode → features 词表。
 *
 * 数据归属：human_interaction_tool 插件（谁的数据谁出表单）。声明走
 * manifest capabilities.tools[].ui.interaction_modes（与 chat_card 同通道，
 * /api/v1/schema tools[] 原样透传），GrowthLoop 装载进本注册表。
 *
 * 双路由（对齐槽位架构）：插件声明覆盖内置默认件；未声明的未知模式用
 * 通用兜底（message + text_input），数据形状增强（带 options/progress
 * 载荷的模式自动补对应特性）——插件新增模式前端零改动可渲染。
 */
import type { PendingInteraction } from '@/stores/interactionStore'

/** 模式布局特性词表（渲染词表——前端消费，插件声明 */
export type InteractionFeature =
  | 'options' // 选项按钮组（点选即回调 onRespondChoice）
  | 'options_detail' // 长描述选项走详情弹窗（≥20 字符）
  | 'text_input' // 自由文本输入 + 发送
  | 'suggestions' // 无 options 时的快捷回复芯片
  | 'navigate' // 「进入对话」跳转按钮
  | 'message' // initialMessage markdown 展示（无则展示 description）
  | 'progress' // 进度条

export interface InteractionModeDecl {
  mode: string
  features: InteractionFeature[]
  /** 文本输入占位文案（声明可覆盖；缺省「输入回复...」） */
  textInputPlaceholder?: string
}

/**
 * 内置默认件（三模式兼容层）：插件未声明时兜底，声明后可覆盖。
 * 与 human_interaction_tool 的 ui.interaction_modes 声明保持同构。
 */
const DEFAULT_MODE_FEATURES: Record<string, InteractionModeDecl> = {
  choice: { mode: 'choice', features: ['options', 'options_detail', 'text_input'], textInputPlaceholder: '输入回复后发送...' },
  conversation: { mode: 'conversation', features: ['options', 'suggestions', 'navigate', 'text_input'] },
  notification: { mode: 'notification', features: ['message', 'progress'] },
}

/** 未知模式兜底：纯展示 + 文本输入（数据形状增强见 resolveLayout） */
const GENERIC_FALLBACK: InteractionModeDecl = { mode: '', features: ['message', 'text_input'] }

const modeDeclarations = new Map<string, InteractionModeDecl>()

/** 从 schema.tools[].ui.interaction_modes 装载声明（幂等：先清空再装） */
export function loadInteractionModes(
  tools: Array<{ ui?: { interaction_modes?: unknown } }>,
): void {
  modeDeclarations.clear()
  for (const t of tools) {
    const decls = t.ui?.interaction_modes
    if (!Array.isArray(decls)) continue
    for (const raw of decls) {
      if (!raw || typeof raw !== 'object') continue
      const decl = raw as { mode?: unknown; features?: unknown; textInputPlaceholder?: unknown }
      if (typeof decl.mode !== 'string' || decl.mode === '') continue
      if (!Array.isArray(decl.features)) continue
      const features = decl.features.filter(
        (f): f is InteractionFeature =>
          typeof f === 'string' &&
          [
            'options',
            'options_detail',
            'text_input',
            'suggestions',
            'navigate',
            'message',
            'progress',
          ].includes(f),
      )
      modeDeclarations.set(decl.mode, {
        mode: decl.mode,
        features,
        textInputPlaceholder:
          typeof decl.textInputPlaceholder === 'string' ? decl.textInputPlaceholder : undefined,
      })
    }
  }
}

/** 按 mode 查声明（内置默认件兜底） */
export function getInteractionModeDecl(mode: string): InteractionModeDecl | undefined {
  return modeDeclarations.get(mode) ?? DEFAULT_MODE_FEATURES[mode]
}

/** 清空声明注册表（测试用） */
export function clearInteractionModes(): void {
  modeDeclarations.clear()
}

export interface InteractionLayout {
  features: Set<InteractionFeature>
  textInputPlaceholder: string
}

/**
 * 解析一次交互的布局（特性集 + 输入占位文案）：声明/内置默认 → 通用兜底；
 * 数据形状增强（载荷里有 options/progress 而特性未含 → 补上）。
 */
export function resolveInteractionLayout(interaction: PendingInteraction): InteractionLayout {
  const decl = modeDeclarations.get(interaction.mode) ?? DEFAULT_MODE_FEATURES[interaction.mode] ?? GENERIC_FALLBACK
  const features = new Set<InteractionFeature>(decl.features)
  if (interaction.options && interaction.options.length > 0) features.add('options')
  if (interaction.progress != null) features.add('progress')
  return { features, textInputPlaceholder: decl.textInputPlaceholder ?? '输入回复...' }
}

/** 兼容旧签名：仅取特性集 */
export function resolveInteractionFeatures(interaction: PendingInteraction): Set<InteractionFeature> {
  return resolveInteractionLayout(interaction).features
}
