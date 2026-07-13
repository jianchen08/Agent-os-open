/**
 * 主题系统类型定义
 *
 * 匹配 config/themes/*.yaml 配置文件结构
 */

/** 主题模式 */
export type ThemeMode = 'light' | 'dark' | 'system'

/** 主题类别 */
export type ThemeCategory = 'light' | 'dark' | 'special' | 'base'

/** 圆角大小 */
export type BorderRadiusSize = 'none' | 'sm' | 'md' | 'lg' | 'xl' | 'full'

/** 字体大小 */
export type FontSizeScale = 'xs' | 'sm' | 'md' | 'lg' | 'xl'

/** 阴影强度 */
export type ShadowIntensity = 'none' | 'light' | 'normal' | 'strong'

/** 按钮样式类型 */
export type ButtonStyle = 'rounded' | 'square' | 'pill'

/** 按钮悬停效果 */
export type ButtonHoverEffect = 'lift' | 'glow' | 'darken' | 'none'

/** 按钮纹理类型 */
export type ButtonTexture = 'none' | 'noise' | 'gradient' | 'glass'

/** 输入框样式类型 */
export type InputStyle = 'filled' | 'outlined' | 'underlined'

/** 卡片样式类型 */
export type CardStyle = 'flat' | 'elevated' | 'outlined' | 'glass' | 'solid'

/** 背景类型 */
export type BackgroundType = 'solid' | 'gradient' | 'image'

/** 纹理类型 */
export type TextureType = 'none' | 'dots' | 'grid' | 'noise' | 'lines'

/** 主题信息 */
export interface ThemeInfo {
  /** 主题 ID */
  id: string
  /** 显示名称 */
  name: string
  /** 描述 */
  description?: string
  /** 主题类别 */
  category: ThemeCategory
  /** 作者 */
  author?: string
  /** 版本 */
  version?: string
  /** 是否为无障碍主题 */
  accessibility?: boolean
  /** 预览色彩 */
  preview?: {
    primary: string
    background: string
    surface: string
    text: string
    accent: string
  }
}

/** 背景色配置 */
export interface BackgroundColors {
  main: string
  card: string
  sidebar: string
  input: string
  elevated: string
}

/** 文字色配置 */
export interface TextColors {
  primary: string
  secondary: string
  muted: string
  disabled: string
}

/** 边框色配置 */
export interface BorderColors {
  default: string
  hover: string
  active: string
}

/** 状态色配置 */
export interface StatusColors {
  success: string
  warning: string
  error: string
  info: string
  running: string
  pending: string
}

/** 消息气泡色配置 */
export interface BubbleColors {
  user_bg: string
  user_text: string
  user_radius?: string
  user_shadow?: string
  user_border?: string
  user_padding?: string
  ai_bg: string
  ai_text: string
  ai_radius?: string
  ai_shadow?: string
  ai_border?: string
  ai_padding?: string
}

/** 任务状态颜色 */
export interface TaskColors {
  pending: string
  running: string
  completed: string
  failed: string
  blocked: string
}

/** 阶段颜色 */
export interface PhaseColors {
  prepare: string
  execute: string
  evaluate: string
}

/** 验收标准状态颜色 */
export interface AcceptanceColors {
  pending: string
  evaluating: string
  passed: string
  failed: string
}

/** 任务类型颜色 */
export interface TaskTypeColors {
  planning: string
  execution: string
  final_evaluation: string
}

/** Agent 层级颜色 */
export interface AgentLevelColors {
  l1: string
  l2: string
  l3: string
}

/** 主题颜色配置 */
export interface ThemeColors {
  /** 基础色 */
  primary: string
  secondary: string
  accent: string

  /** 背景色 */
  background: BackgroundColors

  /** 文字色 */
  text: TextColors

  /** 边框色 */
  border: BorderColors

  /** 状态色 */
  status: StatusColors

  /** 消息气泡 */
  bubble: BubbleColors

  /** 任务状态颜色（可选） */
  task?: TaskColors

  /** 阶段颜色（可选） */
  phase?: PhaseColors

  /** 验收标准状态颜色（可选） */
  acceptance?: AcceptanceColors

  /** 任务类型颜色（可选） */
  task_type?: TaskTypeColors

  /** Agent 层级颜色（可选） */
  agent_level?: AgentLevelColors
}

/** 圆角配置 */
export interface BorderRadiusConfig {
  none: string
  sm: string
  md: string
  lg: string
  xl: string
  full: string
  defaultRadius: BorderRadiusSize
}

/** 字体配置 */
export interface FontConfig {
  ui: string
  code: string
}

/** 字体大小配置 */
export interface FontSizeConfig {
  xs: string
  sm: string
  md: string
  lg: string
  xl: string
  defaultFontSize: FontSizeScale
}

/** 阴影级别配置 */
export interface ShadowLevel {
  sm: string
  md: string
  lg: string
}

/** 阴影配置 */
export interface ShadowsConfig {
  none: ShadowLevel
  light: ShadowLevel
  normal: ShadowLevel
  strong: ShadowLevel
  defaultShadow: ShadowIntensity
}

/** 发光效果配置 */
export interface GlowConfig {
  running: string
  waiting: string
  success: string
  error: string
  defaultGlowIntensity: number
}

/** 按钮变体配置 */
export interface ButtonVariant {
  bg: string
  text: string
  border: string
  hoverBg: string
}

/** 按钮变体集合 */
export interface ButtonVariants {
  primary: ButtonVariant
  secondary: ButtonVariant
  ghost: ButtonVariant
  destructive?: ButtonVariant
}

/** 按钮配置 */
export interface ButtonConfig {
  style: ButtonStyle
  shadow: boolean
  borderWidth: string
  hoverEffect: ButtonHoverEffect
  texture: ButtonTexture
  textureOpacity: number
  variants: ButtonVariants
}

/** 输入框配置 */
export interface InputConfig {
  style: InputStyle
  focusBorder: string
  focusGlow: string
}

/** 卡片配置 */
export interface CardConfig {
  style: CardStyle
  blur: string
  border: string
}

/** 徽章变体配置 */
export interface BadgeVariant {
  bg: string
  text: string
  border: string
}

/** 徽章变体集合 */
export interface BadgeVariants {
  default: BadgeVariant
  secondary: BadgeVariant
  success: BadgeVariant
  warning: BadgeVariant
  error: BadgeVariant
  info: BadgeVariant
}

/** 徽章配置 */
export interface BadgeConfig {
  borderRadius: string
  variants: BadgeVariants
}

/** 对话框配置 */
export interface DialogConfig {
  borderRadius: string
  overlayBg: string
  overlayOpacity: number
  shadow: string
  border: string
}

/** 标签页配置 */
export interface TabsConfig {
  borderRadius: string
  listBg: string
  activeBg: string
  activeText: string
  inactiveText: string
}

/** Toast 配置 */
export interface ToastVariant {
  bg: string
  text: string
  border: string
}

/** Toast 变体集合 */
export interface ToastVariants {
  default: ToastVariant
  success: ToastVariant
  error: ToastVariant
  warning: ToastVariant
  info: ToastVariant
}

/** Toast 配置 */
export interface ToastConfig {
  borderRadius: string
  shadow: string
  variants: ToastVariants
}

/** 进度条配置 */
export interface ProgressConfig {
  borderRadius: string
  trackBg: string
  variants: {
    default: string
    success: string
    warning: string
    error: string
  }
}

/** 下拉菜单配置 */
export interface DropdownMenuConfig {
  borderRadius: string
  shadow: string
  border: string
  itemHoverBg: string
  itemHoverText: string
}

/** 组件样式配置 */
export interface ComponentsConfig {
  /** 圆角 */
  borderRadius: BorderRadiusConfig

  /** 字体 */
  fonts: FontConfig

  /** 字体大小 */
  fontSize: FontSizeConfig

  /** 阴影 */
  shadows: ShadowsConfig

  /** 发光效果（可选） */
  glow?: GlowConfig

  /** 按钮样式 */
  button: ButtonConfig

  /** 输入框样式 */
  input: InputConfig

  /** 卡片样式 */
  card: CardConfig

  /** 徽章样式 */
  badge: BadgeConfig

  /** 对话框样式 */
  dialog: DialogConfig

  /** 标签页样式 */
  tabs: TabsConfig

  /** Toast 样式 */
  toast: ToastConfig

  /** 进度条样式 */
  progress: ProgressConfig

  /** 下拉菜单样式 */
  dropdownMenu: DropdownMenuConfig
}

/** 视觉效果配置 */
export interface EffectsConfig {
  /** 毛玻璃效果 */
  glassmorphism: boolean

  /** 动画 */
  animations: boolean

  /** 过渡时长 (ms) */
  transitionDuration: number

  /** 过渡曲线 */
  transitionEasing: string
}

/** 纹理配置 */
export interface TextureConfig {
  type: TextureType
  color?: string
  size?: string
  opacity?: number
}

/** 背景配置 */
export interface BackgroundConfig {
  type: BackgroundType
  value: string
  texture?: TextureConfig
}

/** 背景图片配置 */
export interface BackgroundImageConfig {
  enabled: boolean
  url: string
  position: string
  size: string
  attachment: string
  overlay: string
  overlayOpacity: number
}

/** 区域背景配置 */
export interface AreaBackgroundConfig {
  type: BackgroundType
  value: string
  texture?: TextureConfig
}

/** 粒子效果配置 */
export interface ParticlesConfig {
  enabled: boolean
  count: number
  color: string
  size: string
  speed: string
}

/** 波浪效果配置 */
export interface WavesConfig {
  enabled: boolean
  color: string
  amplitude: string
  frequency: string
  speed: string
}

/** 星空效果配置 */
export interface StarsConfig {
  enabled: boolean
  count: number
  color: string
  size: string
  twinkle: boolean
}

/** 扫描线效果配置 */
export interface ScanlinesConfig {
  enabled: boolean
  color: string
  width: string
  speed: string
}

/** 背景系统配置 */
export interface BackgroundsConfig {
  /** 主背景 */
  main: BackgroundConfig

  /** 背景图片（可选） */
  image?: BackgroundImageConfig

  /** 纹理图案（可选） */
  texture?: TextureConfig

  /** 侧边栏背景（可选） */
  sidebar?: AreaBackgroundConfig

  /** 聊天区域背景（可选） */
  chat?: BackgroundConfig

  /** 粒子效果（可选） */
  particles?: ParticlesConfig

  /** 波浪效果（可选） */
  waves?: WavesConfig

  /** 星空效果（可选） */
  stars?: StarsConfig

  /** 扫描线效果（可选） */
  scanlines?: ScanlinesConfig
}

/** 无障碍配置 */
export interface AccessibilityConfig {
  /** 对比度比例 */
  contrastRatio: string

  /** 焦点指示器 */
  focusIndicator: {
    enabled: boolean
    color: string
    width: string
    style: string
  }

  /** 屏幕阅读器优化 */
  screenReader: {
    optimized: boolean
    announcements: boolean
  }

  /** 减少动画 */
  reducedMotion: boolean

  /** 大字体支持 */
  largeText: boolean
}

/** 完整主题配置 */
export interface ThemeConfig {
  /** 主题 ID */
  id: string

  /** 显示名称 */
  name: string

  /** 描述 */
  description?: string

  /** 主题类别 */
  category?: ThemeCategory

  /** 作者 */
  author?: string

  /** 版本 */
  version?: string

  /** 继承的主题 ID */
  extends?: string

  /** 是否为无障碍主题 */
  accessibility?: boolean

  /** 预览色彩 */
  preview?: {
    primary: string
    background: string
    surface: string
    text: string
    accent: string
  }

  /** 颜色配置 */
  colors: ThemeColors

  /** 组件样式配置 */
  components: ComponentsConfig

  /** 视觉效果配置 */
  effects: EffectsConfig

  /** 背景配置 */
  backgrounds: BackgroundsConfig

  /** 无障碍配置（可选） */
  accessibility_config?: AccessibilityConfig
}
