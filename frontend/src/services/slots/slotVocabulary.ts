/**
 * 槽 key 词表（C4 前端槽位扩展机制，设计真值 =
 * docs/working/DSH生态嫁接通用机制设计_20261104.md §4.4）
 *
 * 词表由核心布局声明（锚点预留自 FiveSpaceLayout 各区）；插件声明表外的
 * 槽 key = 装载期报错跳过（fail-closed，对齐 DSH 声明式授权制，AC-F2）。
 *
 * 三轴语义（2026-11-04 评审澄清）：
 * - channel = 渲染组件怎么来（component=宿主进程内/主题跟随/props 注入；
 *   webview=独立文档/自带世界/IPC 桥）
 * - slot = 渲染组件怎么组织（在哪/何时/排序/条件/键控）
 * - widgetRegistry = component 通道怎么发现（widget 名 → 渲染器目录）
 */

/** 槽 kind：list=加聚排序（条目聚合）；keyed=按键实例（key 寻址） */
export type SlotKind = 'list' | 'keyed'

/** 单个槽域规格：kind 语义 + 是否对插件声明开放 */
export interface SlotDomainSpec {
  kind: SlotKind
  /**
   * 是否开放：预留/悬置键在词表内登记但不开放——插件声明未开放键同样
   * 装载报错跳过（词表是契约边界，不是占位名册）。
   */
  open: boolean
}

/**
 * 槽 key 词表（按实际布局：主体三区 SideBar/Chat/Workspace + 浮层；
 * StatusBar 已移除——FiveSpaceLayout.tsx:954 实证）
 */
export const SLOT_VOCABULARY: Record<string, SlotDomainSpec> = {
  /** SideBar 会话列表下方区块（全宽纵向卡流带上限） */
  'sidebar.block': { kind: 'list', open: true },
  /** Workspace 标签（keyed：key=path 直接达语义；≈存量 contributes.pages 收敛） */
  'workspace.tab': { kind: 'keyed', open: true },
  /** 壳浮层（FloatingWindowManager/FullscreenOverlay 域；modal 收敛=模态呈现） */
  'shell.overlay': { kind: 'list', open: true },
  /** 消息流工具卡 keyed 槽（内建卡=词表白名单不变；插件自定义卡=W 动态注册出口） */
  'chat.toolview': { kind: 'keyed', open: true },
  /** Chat 输入区扩展位（P2 批次，先登记不开放） */
  'chat.composer.extension': { kind: 'list', open: false },
  /** 会话回合尾部动作（P2 批次，先登记不开放） */
  'chat.turnTail.action': { kind: 'keyed', open: false },
  /** TitleBar 右侧图标组（预留：先只内部插件，不开放） */
  'titlebar.action': { kind: 'list', open: false },
  /** 区已移除，词表预留不开放（恢复底栏再开） */
  'statusbar.item': { kind: 'list', open: false },
}

/** 表外/未开放判定（fail-closed 的唯一词表口径） */
export function isSlotKeyOpen(slotKey: string): boolean {
  return SLOT_VOCABULARY[slotKey]?.open === true
}

/** 槽 kind（表外返回 undefined） */
export function slotKindOf(slotKey: string): SlotKind | undefined {
  return SLOT_VOCABULARY[slotKey]?.kind
}
