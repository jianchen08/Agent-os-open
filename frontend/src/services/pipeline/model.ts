/**
 * 0.2 多循环体管道模型（config/pipelines/autonomous.yaml）+
 * 可视化编辑器的纯函数辅助层。
 *
 * 设计要点：
 * - GET /api/v1/config/pipelines/{name} 返回 YAML→JSON 的原始对象，编辑器以
 *   「raw data 为唯一真相 + path 不可变更新」方式工作——模型之外的未知字段
 *   在保存（PUT 整个 data）时原样保留，不会因为经过类型化模型而丢失。
 * - 类型对齐 G10 文件 DSL（kernel/crates/api/src/pipeline_loader.rs 的 `*File`
 *   结构，deny_unknown_fields）：转移写在 `next:` 列表（`then` 为目标字符串，
 *   `set` 平级）、循环体循环条件为 `while`。旧内部形态（`routes`/`exit_routes`/
 *   体级 `loop_config`/`then:{next,set}` 对象/`then: wait`）内核加载即报错，
 *   编辑器不得写出。
 *
 * @module services/pipeline/model
 */

/** 编辑器定位 raw data 内节点的路径段（对象 key 或数组下标） */
export type Path = Array<string | number>

/** 循环配置（step 级合法字段；循环体级循环用 `while`，无 loop_config） */
export interface LoopConfigV2 {
  enabled?: boolean
  /** -1=无限循环；>0=安全阀 */
  max_iterations?: number
}

/**
 * G10 转移分支（文件 DSL `next:` 列表项；内核 TransitionFile）。
 *
 * `then` 目标合法集（内核加载期校验，非法目标启动报错）：
 * - step 级：`'end'` / `'loop'` / 本循环体内 step id / 循环体 id（跨体转移）；
 * - 循环体级：`'end'` / 循环体 id（`loop` 非法——出口转移在体循环结束后求值）。
 * `wait` 已退役（挂起由 state.suspended 表达）。
 */
export interface TransitionRule {
  /** 条件表达式；缺省 / 'True' = 恒真（兜底路由） */
  when?: string
  /** 跳转目标字符串（合法集见接口注释） */
  then: string
  /** 命中时写入 state 的字段（可省） */
  set?: Record<string, unknown>
}

/** 管道 step 节点（组合节点；原子执行单元是 steps 里的引用） */
export interface PipelineStepV2 {
  id: string
  /**
   * 引用列表：管道 step id / 公共 step 库 id / 插件名 / "{{...}}" 动态模板；
   * 条目形态两态——字符串直引，或 G9 项级 when 门对象（{name, when, inputs?}）。
   */
  steps?: (string | Record<string, unknown>)[]
  /** step 级钩子（管道步骤服务化提案 §3.6：{on, run}，P1 只读展示） */
  hooks?: PipeHookEntry[]
  /** 自由 key-value（模板字符串），merge 进 state 供插件读取 */
  context?: Record<string, unknown>
  /** G10 出口转移（DSL `next:` 列表；缺省顺序执行下一步） */
  next?: TransitionRule[]
  /** step 自带循环（组合节点可自带循环，如批量处理） */
  loop_config?: LoopConfigV2
}

/** 钩子声明条目（on = 事件名，run = 插件id[.方法]） */
export interface PipeHookEntry {
  on: string
  run: string
}

/** steps 引用条目归一化视图 */
export interface NormalizedStepRef {
  name: string
  /** G9 项级 when 门（对象条目专属；字符串直引无门） */
  when?: string
  /** 原始条目是否为对象形态（when 门） */
  gated: boolean
}

/**
 * steps 引用条目归一化：字符串直引 → {name}；G9 项级 when 门对象
 * （{name, when}）→ {name, when, gated}；其余畸形条目 → undefined
 * （渲染层降级展示，不崩溃）。
 */
export function normalizeStepRef(entry: unknown): NormalizedStepRef | undefined {
  if (typeof entry === 'string') {
    return entry ? { name: entry, gated: false } : undefined
  }
  if (entry !== null && typeof entry === 'object') {
    const obj = entry as Record<string, unknown>
    if (typeof obj.name === 'string' && obj.name) {
      return {
        name: obj.name,
        ...(typeof obj.when === 'string' && obj.when ? { when: obj.when } : {}),
        gated: true,
      }
    }
  }
  return undefined
}

/** 循环体（内核 LoopBody） */
export interface LoopBodyV2 {
  id: string
  steps: PipelineStepV2[]
  /** 体级钩子（管道步骤服务化提案 §3.6，P1 只读展示） */
  hooks?: PipeHookEntry[]
  /** G10 循环继续条件（DSL `while: "expr"`；缺省 = 单次执行） */
  while?: string
  /** G10 循环体结束转移（DSL `next:` 列表；缺省顺序进下一个体） */
  next?: TransitionRule[]
  /** 提前终止（ended/出错）时仍执行（收尾语义） */
  run_on_error?: boolean
}

/** 0.2 管道配置（raw data 的类型视图） */
export interface PipelineConfigV2 {
  name: string
  loop_bodies: LoopBodyV2[]
  [key: string]: unknown
}

// ── 格式判断与读取（防御性：字段可能缺省/为空） ─────────────────────

/** data 是否为 0.2 多循环体格式（有非空 loop_bodies 数组） */
export function isPipelineV2Data(data: unknown): data is PipelineConfigV2 {
  if (data === null || typeof data !== 'object') return false
  const bodies = (data as Record<string, unknown>).loop_bodies
  return Array.isArray(bodies) && bodies.length > 0
}

/** 读取循环体列表（防御缺省） */
export function getLoopBodies(data: unknown): LoopBodyV2[] {
  return isPipelineV2Data(data) ? (data.loop_bodies as LoopBodyV2[]) : []
}

/** 收集全部 step id（路由 step 目标下拉的数据源；跳过非法条目） */
export function collectStepIds(data: unknown): string[] {
  const ids: string[] = []
  for (const body of getLoopBodies(data)) {
    if (!Array.isArray(body?.steps)) continue
    for (const step of body.steps) {
      if (typeof step?.id === 'string' && step.id) ids.push(step.id)
    }
  }
  return ids
}

/** 收集全部循环体 id（路由 phase 目标下拉的数据源） */
export function collectBodyIds(data: unknown): string[] {
  return getLoopBodies(data)
    .map((body) => body?.id)
    .filter((id): id is string => typeof id === 'string' && id.length > 0)
}

// ── steps 引用分类（三级命中 + 动态模板） ─────────────────────────

/** steps 引用的解析类别 */
export type RefKind = 'plugin' | 'step' | 'template' | 'unknown'

/** 解析结果：类别 + 目录条目（仅 plugin 类有） */
export interface RefResolution {
  kind: RefKind
  /** plugin 类且目录命中时的插件目录条目 */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- 目录条目类型由服务层定义，模型层只做存在性判断
  catalogEntry?: any
}

/** 是否为 "{{...}}" 动态模板引用（如 core step 的 {{state.core_plugin}}） */
export function isTemplateRef(ref: string): boolean {
  return /\{\{.*\}\}/.test(ref)
}

/**
 * 解析一条 steps 引用：
 * ① 命中插件目录 → plugin；② 命中管道内 step id → step（组合节点递归引用）；
 * ③ "{{...}}" → template（动态插件名，运行期渲染）；④ 其余（可能是公共
 * step 库 id，前端无清单接口）→ unknown。
 */
export function resolveRef(
  ref: string,
  catalog: Iterable<{ id: string }>,
  knownStepIds: ReadonlySet<string>,
): RefResolution {
  if (isTemplateRef(ref)) return { kind: 'template' }
  if (knownStepIds.has(ref)) return { kind: 'step' }
  for (const entry of catalog) {
    if (entry.id === ref) return { kind: 'plugin', catalogEntry: entry }
  }
  return { kind: 'unknown' }
}

// ── raw data 路径不可变更新（编辑器的 ops 实现） ─────────────────────

/** 编辑器对 raw data 的操作集（页面实现，组件消费） */
export interface PipelineEditorOps {
  /** 设置 path 处的值（缺失的中间对象自动创建） */
  set: (path: Path, value: unknown) => void
  /** 删除 path 处的 key / 数组元素 */
  remove: (path: Path) => void
  /** 向 arrayPath 数组插入元素（index 可超界，钳制到末尾） */
  insert: (arrayPath: Path, index: number, value: unknown) => void
  /** 移动 arrayPath 数组的第 index 个元素（delta ±1） */
  move: (arrayPath: Path, index: number, delta: -1 | 1) => void
}

/** 不可变 set：缺失的字符串段中间层创建为空对象；不写 undefined */
export function setAtPath<T>(root: T, path: Path, value: unknown): T {
  if (path.length === 0) return structuredClone(root)
  const next = structuredClone(root) as Record<string | number, unknown>
  let cursor: Record<string | number, unknown> = next
  for (let i = 0; i < path.length - 1; i++) {
    const seg = path[i]
    const child = cursor[seg]
    if (child === null || child === undefined || typeof child !== 'object') {
      cursor[seg] = {}
    }
    cursor = cursor[seg] as Record<string | number, unknown>
  }
  cursor[path[path.length - 1]] = value
  return next as unknown as T
}

/** 不可变删除：对象删 key，数组按下标 splice */
export function deleteAtPath<T>(root: T, path: Path): T {
  if (path.length === 0) return structuredClone(root)
  const next = structuredClone(root) as Record<string | number, unknown>
  let cursor: Record<string | number, unknown> = next
  for (let i = 0; i < path.length - 1; i++) {
    const child = cursor[path[i]]
    if (child === null || typeof child !== 'object') return next as unknown as T
    cursor = child as Record<string | number, unknown>
  }
  const last = path[path.length - 1]
  if (Array.isArray(cursor)) {
    const index = Number(last)
    if (Number.isInteger(index) && index >= 0 && index < cursor.length) cursor.splice(index, 1)
  } else {
    delete cursor[last]
  }
  return next as unknown as T
}

/** 数组内不可变插入（index 钳制到 [0, length]） */
export function insertAtPath<T>(root: T, arrayPath: Path, index: number, value: unknown): T {
  const array = getAtPath(root, arrayPath)
  if (!Array.isArray(array)) return setAtPath(root, arrayPath, [value])
  const clamped = Math.max(0, Math.min(index, array.length))
  const next = structuredClone(root) as Record<string | number, unknown>
  const target = getMutableAtPath(next, arrayPath)
  if (Array.isArray(target)) target.splice(clamped, 0, structuredClone(value))
  return next as unknown as T
}

/** 数组元素不可变移动 */
export function moveArrayItem<T>(root: T, arrayPath: Path, index: number, delta: -1 | 1): T {
  const array = getAtPath(root, arrayPath)
  if (!Array.isArray(array)) return root
  const to = index + delta
  if (index < 0 || index >= array.length || to < 0 || to >= array.length) return root
  const next = structuredClone(root) as Record<string | number, unknown>
  const target = getMutableAtPath(next, arrayPath)
  if (!Array.isArray(target)) return next as unknown as T
  const [item] = target.splice(index, 1)
  target.splice(to, 0, item)
  return next as unknown as T
}

/** 读取 path 处的值（任一层缺失返回 undefined） */
function getAtPath(root: unknown, path: Path): unknown {
  let cursor: unknown = root
  for (const seg of path) {
    if (cursor === null || typeof cursor !== 'object') return undefined
    cursor = (cursor as Record<string | number, unknown>)[seg]
  }
  return cursor
}

/** 拿 path 处的可变引用（配合 structuredClone 后的 next 使用） */
function getMutableAtPath(
  next: Record<string | number, unknown>,
  path: Path,
): Record<string | number, unknown> | unknown[] | undefined {
  let cursor: unknown = next
  for (const seg of path) {
    if (cursor === null || typeof cursor !== 'object') return undefined
    cursor = (cursor as Record<string | number, unknown>)[seg]
  }
  return cursor as Record<string | number, unknown> | unknown[] | undefined
}
