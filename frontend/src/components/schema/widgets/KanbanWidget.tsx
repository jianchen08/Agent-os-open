/**
 * KanbanWidget —— 看板组件「插件可接入」骨架
 *
 * 设计原则（见任务说明）：本组件只负责把 columns + data 渲染成横向列 + 卡片堆叠，
 * 不引入 dnd-kit / sortablejs（拖拽属完整功能，后续 Phase 再做）。
 * 真实数据可由插件通过 props.data 提供；将来 dataSource 可走 apiClient 拉取
 * （本骨架仅接收静态 props.data，不做远程拉取）。
 *
 * 数据结构：
 *   columns: [{ id: string, title?: string }]
 *   data   : [{ id: string, columnId: string, title?: string, ... }]
 *   卡片按 data[i].columnId 分组归入对应列；无对应列的卡片归入「其他」列。
 *
 * Props 契约（flat，与 TableWidget 一致；由 registerWidgets 以
 * `WidgetComponent = ComponentType<Record<string, unknown>>` 注册）：
 *   { columns?, data?, dataSource?, pluginId?, title?, ...rest }
 */

/** 看板列定义 */
export interface KanbanColumn {
  /** 列唯一标识 */
  id: string
  /** 列标题 */
  title?: string
}

/** 看板卡片（宽松结构，允许插件附加任意字段） */
export interface KanbanCard {
  /** 卡片唯一标识 */
  id: string
  /** 所属列 id（用于分组） */
  columnId: string
  /** 卡片标题 */
  title?: string
  /** 卡片描述（可选） */
  description?: string
  /** 其他扩展字段 */
  [key: string]: unknown
}

/** KanbanWidget props（flat，与 WidgetRegistry 的 WidgetProps 契约一致）。 */
export interface KanbanWidgetProps {
  /** 列定义 */
  columns?: KanbanColumn[]
  /** 卡片数据（按 columnId 分组） */
  data?: KanbanCard[]
  /** 数据源 URI（骨架暂不拉取，预留接入） */
  dataSource?: string
  /** 兼容 composer 传入的 snake_case */
  data_source?: string
  /** 提供数据的插件 id（标记「由插件提供」） */
  pluginId?: string
  /** 看板标题（可选） */
  title?: string
}

/** 「其他」列：兜底分组，容纳找不到对应列的卡片 */
const OTHER_COLUMN_ID = '__kanban_other__'

/**
 * 从原始输入提取列定义（类型守卫，容错非数组 / 非对象输入）。
 */
function extractColumns(raw: unknown): KanbanColumn[] {
  if (!Array.isArray(raw)) return []
  return raw.filter(
    (c): c is KanbanColumn =>
      typeof c === 'object' && c !== null && typeof (c as KanbanColumn).id === 'string',
  )
}

/**
 * 从原始输入提取卡片数据（类型守卫，要求 id 与 columnId 为 string）。
 */
function extractCards(raw: unknown): KanbanCard[] {
  if (!Array.isArray(raw)) return []
  return raw.filter(
    (c): c is KanbanCard =>
      typeof c === 'object' &&
      c !== null &&
      typeof (c as KanbanCard).id === 'string' &&
      typeof (c as KanbanCard).columnId === 'string',
  )
}

/**
 * 看板组件骨架（静态列 + 卡片渲染，无拖拽）
 *
 * @param props - flat 组件属性
 * @returns 横向列布局的看板，或空状态占位
 */
export function KanbanWidget(props: KanbanWidgetProps) {
  const { columns, data, pluginId, title } = props
  const cols = extractColumns(columns)
  const cards = extractCards(data)

  // 空状态：无列或无卡片
  if (cols.length === 0 || cards.length === 0) {
    return (
      <div
        data-testid="kanban-empty"
        className="text-muted-foreground flex h-full flex-col items-center justify-center gap-1 rounded-lg border border-dashed p-8 text-center text-sm"
      >
        <span className="text-base font-medium">看板暂无数据</span>
        <span className="text-xs opacity-70">
          提供 <code className="font-mono">columns</code> 与{' '}
          <code className="font-mono">data</code>，或接入插件以提供数据
        </span>
      </div>
    )
  }

  // 已知列 id 集合
  const knownColIds = new Set(cols.map((c) => c.id))

  // 按 columnId 分组：已知列归入对应列，未知列归入「其他」
  const grouped = new Map<string, KanbanCard[]>()
  for (const card of cards) {
    const key = knownColIds.has(card.columnId) ? card.columnId : OTHER_COLUMN_ID
    const list = grouped.get(key)
    if (list) list.push(card)
    else grouped.set(key, [card])
  }

  // 渲染列：已知列（保持顺序）+ 可能的「其他」列
  const renderCols: KanbanColumn[] = [...cols]
  if (grouped.has(OTHER_COLUMN_ID)) {
    renderCols.push({ id: OTHER_COLUMN_ID, title: '其他' })
  }

  return (
    <div
      data-testid="kanban-board"
      data-plugin-id={pluginId}
      className="bg-kanban-board flex h-full w-full flex-col rounded-lg border"
    >
      {(title || pluginId) && (
        <div className="border-b bg-muted/40 flex items-center justify-between px-3 py-1.5">
          {title && (
            <span className="text-foreground text-xs font-semibold">{title}</span>
          )}
          {pluginId && (
            <span className="text-muted-foreground text-[10px]">
              由插件「{pluginId}」提供
            </span>
          )}
        </div>
      )}
      <div className="kanban-columns flex h-full gap-2 overflow-x-auto p-2">
        {renderCols.map((col) => {
          const colCards = grouped.get(col.id) ?? []
          return (
            <div
              key={col.id}
              data-testid={`kanban-column-${col.id}`}
              data-column-id={col.id}
              className="bg-muted/30 flex w-60 shrink-0 flex-col rounded-md border"
            >
              <div className="flex items-center justify-between border-b px-2.5 py-1.5">
                <span className="text-foreground text-xs font-medium">
                  {col.title ?? col.id}
                </span>
                <span className="text-muted-foreground rounded-full bg-muted px-1.5 text-[10px]">
                  {colCards.length}
                </span>
              </div>
              <div className="kanban-cards flex flex-1 flex-col gap-1.5 overflow-y-auto p-1.5">
                {colCards.length === 0 ? (
                  <div className="text-muted-foreground/60 px-1.5 py-2 text-center text-[10px]">
                    暂无卡片
                  </div>
                ) : (
                  colCards.map((card) => (
                    <div
                      key={card.id}
                      data-testid={`kanban-card-${card.id}`}
                      data-card-id={card.id}
                      data-column-id={col.id}
                      className="bg-card text-card-foreground rounded-md border px-2.5 py-2 text-xs shadow-sm"
                    >
                      <div className="font-medium">
                        {card.title ?? card.id}
                      </div>
                      {card.description && (
                        <div className="text-muted-foreground mt-0.5 text-[11px]">
                          {card.description}
                        </div>
                      )}
                    </div>
                  ))
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

export default KanbanWidget
