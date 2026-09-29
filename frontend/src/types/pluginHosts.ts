/**
 * 插件宿主盒子类型定义
 *
 * 内核只读端点 GET /api/v1/plugins/hosts（admin 鉴权）的响应契约面。
 * 字段名与后端契约逐字一致；字段可缺省/为 null（后端只返回实际存在的宿主），
 * 消费方需对 null/缺字段健壮。
 *
 * 语义：
 * - host_key = 宿主标识（如 group:light:1 / solo:metrics_admin）
 * - kind = group（合宿）/ solo（独占）
 * - members = 装箱分配表（含 pending_rejoin = 软卸载待重加入标记：分配在但
 *   进程里没有，等下次 respawn 回归）
 * - spawned_members = 宿主进程实际成员快照
 * - starting = spawn 窗口中；starting_members = 窗口内的成员 id
 * - pending_spawn = 已启用但未装载的插件（异常嫌疑）
 */

/** 宿主分配表条目 */
export interface HostMember {
  /** 插件 id */
  plugin_id: string
  /** 软卸载待重加入标记（分配在但进程里没有，等下次 respawn 回归） */
  pending_rejoin?: boolean | null
}

/** 单个插件宿主（盒子）快照 */
export interface PluginHost {
  /** 宿主标识（group:light:1 / solo:metrics_admin） */
  host_key: string
  /** 宿主形态：group（合宿）/ solo（独占） */
  kind?: string | null
  /** 宿主进程 PID（未 spawn 为 null） */
  pid?: number | null
  /** 宿主进程是否存活 */
  alive?: boolean | null
  /** 宿主进程常驻内存（MB） */
  rss_mb?: number | null
  /** 宿主进程运行时长（秒） */
  uptime_secs?: number | null
  /** 宿主进程实际成员快照 */
  spawned_members?: string[] | null
  /** 装箱分配表 */
  members?: HostMember[] | null
  /** 宿主级在途调用数 */
  in_flight?: number | null
  /** 成员级在途调用数（plugin_id → 计数） */
  member_in_flight?: Record<string, number> | null
  /** 最后一次调用时间（unix 秒；从未调用为 null） */
  last_call_at?: number | null
  /** 是否处于 spawn 窗口 */
  starting?: boolean | null
  /** spawn 窗口内的成员 id */
  starting_members?: string[] | null
}

/** 已启用但未装载的插件条目（异常嫌疑） */
export interface PendingSpawnEntry {
  /** 插件 id */
  plugin_id: string
  /** 是否已启用 */
  enabled?: boolean | null
  /** 最后一次调用时间（unix 秒；从未调用为 null） */
  last_call_at?: number | null
}

/**
 * 插件运行态表行（GET /ext/monitoring/plugins rows；进程观测卡片的 join 源）
 *
 * 行字段与后端 kernel_reads.plugin_runtime 组装逐字一致；该端点按内核周期
 * 轮询代采组行，lazy 未运行的插件不占行。卡片唯一消费 last_crash_ts（hosts
 * 端点没有的列），其余列由卡片自身数据承载。
 */
export interface PluginRuntimeRow {
  /** 插件 id */
  plugin_id: string
  /** 进程是否存活（0/1） */
  alive?: number | null
  /** 派生状态：running / dead */
  status?: string | null
  /** 进程 PID */
  pid?: number | null
  /** 常驻内存（MB） */
  memory_rss_mb?: number | null
  /** 运行时长（秒） */
  uptime_seconds?: number | null
  /** 上次崩溃时间戳（unix 秒；0 = 未崩过或留存窗外） */
  last_crash_ts?: number | null
}

/** GET /api/v1/plugins/hosts 响应 */
export interface PluginHostsResponse {
  /** 宿主盒子清单（后端只返回实际存在的宿主） */
  hosts?: PluginHost[] | null
  /** 已启用但未装载的插件差集 */
  pending_spawn?: PendingSpawnEntry[] | null
}
