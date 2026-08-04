/**
 * 通用配置注册表
 *
 * 定义所有通用配置页面的元数据（标题、描述、图标、后端路径）。
 * GenericConfigPage 和 SettingsPage 共享此注册表。
 */

/** 配置页元数据 */
export interface GenericConfigEntry {
  /** 后端配置路径（白名单 key） */
  configPath: string
  /** 页面标题 */
  title: string
  /** 页面描述 */
  description: string
  /** 图标（emoji） */
  icon: string
  /** 字段中文标签映射（点号分隔路径，如 "database.pool_size"） */
  labelMap?: Record<string, string>
}

/** 分组定义 */
export interface ConfigGroup {
  /** 分组名称 */
  name: string
  /** 分组下的配置项 */
  items: GenericConfigEntry[]
}

/**
 * 配置分组列表
 *
 * 分组展示在 SettingsPage 卡片中，每项对应一个 GenericConfigPage。
 */
export const CONFIG_GROUPS: ConfigGroup[] = [
  {
    name: '系统配置',
    items: [
      {
        configPath: 'system/memory_storage',
        title: '记忆存储',
        description: '记忆存储后端、向量检索、维护配置',
        icon: '🧠',
        labelMap: {
          // 顶层字段
          episode_backend: '情景记忆后端',
          semantic_backend: '语义记忆后端',
          // 数据库配置
          database: '数据库配置',
          'database.pool_size': '连接池大小',
          'database.max_overflow': '连接池最大溢出',
          'database.pool_timeout': '连接超时（秒）',
          'database.pool_recycle': '连接回收时间（秒）',
          // Redis 配置
          redis: 'Redis 配置',
          'redis.host': '主机地址',
          'redis.port': '端口',
          'redis.db': '数据库索引',
          'redis.password': '密码',
          'redis.pool_size': '连接池大小',
          'redis.ttl': '数据过期时间（秒）',
          'redis.socket_timeout': '连接超时（秒）',
          'redis.ssl': 'SSL/TLS',
          // 文件系统配置
          file: '文件系统配置',
          'file.base_path': '基础存储路径',
          'file.compression': '启用压缩',
          'file.compression_level': '压缩级别（1-9）',
          'file.enable_index': '启用索引',
          'file.index_path': '索引文件路径',
          'file.file_mode': '文件权限',
          'file.dir_mode': '目录权限',
          // 混合存储配置
          hybrid: '混合存储配置',
          'hybrid.hot_data_backend': '热数据后端',
          'hybrid.cold_data_backend': '冷数据后端',
          'hybrid.archive_backend': '归档数据后端',
          'hybrid.hot_threshold': '热数据阈值',
          'hybrid.hot_threshold.min_access_count': '最小访问次数',
          'hybrid.hot_threshold.max_age_hours': '最大保留时间（小时）',
          'hybrid.cold_threshold': '冷数据阈值',
          'hybrid.cold_threshold.max_age_hours': '最大保留时间（小时）',
          'hybrid.archive_threshold': '归档阈值',
          'hybrid.archive_threshold.max_age_hours': '最大保留时间（小时）',
          // 性能优化配置
          performance: '性能优化配置',
          'performance.batch_size': '批量操作大小',
          'performance.max_concurrent': '并发操作数量',
          'performance.cache_size': '缓存大小（条目数）',
          'performance.preload': '预加载配置',
          'performance.preload.enabled': '启用预加载',
          'performance.preload.max_items': '最大预加载条目数',
          // 向量检索配置
          vector_search: '向量检索配置',
          'vector_search.enabled': '启用向量检索',
          'vector_search.fallback_to_keyword': '不可用时回退到关键词检索',
          'vector_search.default_method': '默认检索方法',
          'vector_search.hybrid': '混合检索配置',
          'vector_search.hybrid.enabled': '启用混合检索',
          'vector_search.hybrid.vector_weight': '向量检索权重',
          'vector_search.hybrid.keyword_weight': '关键词检索权重',
          'vector_search.embedding_dim': '嵌入向量维度',
          // 健康检查配置
          health_check: '健康检查配置',
          'health_check.enabled': '启用健康检查',
          'health_check.check_interval': '检查间隔（秒）',
          // 记忆维护配置
          maintenance: '记忆维护配置',
          'maintenance.enabled': '启用自动维护',
          'maintenance.review': '复盘配置',
          'maintenance.review.trigger': '复盘触发条件',
          'maintenance.review.trigger.min_records': '最小记录数',
          'maintenance.review.trigger.max_interval': '最大间隔（秒）',
          'maintenance.review.skeleton_budget_percent': '骨架预算百分比',
          'maintenance.review.records_per_skeleton_token': '每 Token 记录数',
          'maintenance.review.max_records_per_review': '单次复盘最大记录数',
          'maintenance.cleanup': '清理配置',
          'maintenance.cleanup.check_interval': '巡检间隔（秒）',
          'maintenance.cleanup.min_age_days': '最小保留天数',
          'maintenance.cleanup.capacity_pressure_threshold': '容量压力阈值',
          'maintenance.cleanup.early_cleanup_age_days': '提前清理天数',
          // 监控配置
          monitoring: '监控配置',
          'monitoring.enabled': '启用性能监控',
          'monitoring.slow_query_threshold': '慢查询阈值（毫秒）',
          'monitoring.log_operations': '记录操作日志',
          'monitoring.log_level': '日志级别',
        },
      },
      {
        configPath: 'system/api_config',
        title: 'API 配置',
        description: '管理外部 API 密钥、端点、限流、CORS',
        icon: '🔑',
        labelMap: {
          endpoint: '端点配置',
          'endpoint.base_url': 'Base URL',
          'endpoint.version': 'API 版本',
          'endpoint.timeout': '超时时间（秒）',
          rate_limit: '限流配置',
          'rate_limit.global_limit': '全局限流',
          'rate_limit.auth': '认证限流',
          'rate_limit.tasks': '任务限流',
          'rate_limit.websocket': 'WebSocket 限流',
          cors_origins: 'CORS 允许的源',
        },
      },
      {
        configPath: 'system/concurrency_config',
        title: '并发控制',
        description: '任务并发数、Agent 层级并发、LLM 并发、工作流并发',
        icon: '⚡',
        labelMap: {
          task: '任务并发',
          'task.max_concurrent_tasks': '最大并发任务数',
          'task.task_max_workers': '线程池大小',
          'task.task_timeout': '任务超时（秒）',
          agent: 'Agent 层级并发',
          'agent.l1_max_concurrent': 'L1 Agent（项目经理）',
          'agent.l2_max_concurrent': 'L2 Agent（团队负责人）',
          'agent.l3_max_concurrent': 'L3 Agent（执行者）',
          workflow: '工作流并发',
          'workflow.max_concurrent': '最大工作流并发',
          llm: 'LLM 并发',
          'llm.zhipu_max_concurrent': '智谱 AI',
          'llm.openai_max_concurrent': 'OpenAI',
          'llm.anthropic_max_concurrent': 'Anthropic',
          'llm.default_max_concurrent': '默认提供商',
        },
      },
      {
        configPath: 'system/context_window_config',
        title: '上下文窗口',
        description: 'Token 预算分配、记忆层级、压缩设置',
        icon: '📏',
        labelMap: {
          version: '版本',
          compress_trigger_ratio: '全局压缩触发比例',
          budgets: 'Token 预算分配',
          'budgets.system_prompt': '系统提示词',
          'budgets.tools_description': '工具描述',
          'budgets.static_vars': '静态资源',
          'budgets.dynamic_variables': '动态上下文',
          'budgets.l3': 'L3 记忆（关键词索引）',
          'budgets.l2': 'L2 记忆（摘要）',
          'budgets.l1': 'L1 记忆（详细历史）',
          'budgets.recent': '最近消息',
          'budgets.retrieval': '检索结果',
          'budgets.response_reserve': '响应预留',
          include_tools_description_in_prompt: '在提示词中包含工具描述',
          stability: '稳定性配置',
          layer_order: '层级顺序',
          compression: '压缩配置',
          'compression.enabled': '启用压缩',
          'compression.model': '压缩模型',
          'compression.layer_trigger_ratio': '单层触发比例',
          'compression.max_turn_ratio': '最大轮次比例',
          custom_layers: '自定义层级',
        },
      },
      {
        configPath: 'system/cost_control',
        title: '成本控制',
        description: 'Token 用量限制、预算管理、费用告警、保护策略',
        icon: '💰',
        labelMap: {
          enabled: '启用成本控制',
          global_config: '预算限制',
          'global_config.daily_token_limit': '每日 Token 限制',
          'global_config.monthly_token_limit': '每月 Token 限制',
          'global_config.per_task_token_limit': '单任务 Token 限制',
          'global_config.per_session_token_limit': '单会话 Token 限制',
          alerts: '告警设置',
          'alerts.warning_threshold': '警告阈值（%）',
          'alerts.critical_threshold': '严重阈值（%）',
          'alerts.exhausted_threshold': '耗尽阈值（%）',
          protection: '保护策略',
          'protection.auto_save_at_warning': '警告时自动保存',
          'protection.auto_pause_at_critical': '严重时自动暂停',
          'protection.auto_stop_at_exhausted': '耗尽时自动停止',
        },
      },
      {
        configPath: 'system/editor_config',
        title: '编辑器配置',
        description: '默认编辑器和文件类型映射',
        icon: '📝',
      },
      {
        configPath: 'system/long_term_task',
        title: '长期任务',
        description: '长期任务超时、重试、心跳、恢复配置',
        icon: '⏱️',
      },
    ],
  },
  {
    name: '模型与媒体',
    items: [
      {
        configPath: 'models/media_providers',
        title: '多媒体 Provider',
        description: 'TTS、图像、视频、音乐生成的 Provider 配置',
        icon: '🎬',
      },
    ],
  },
  {
    name: '隔离与安全',
    items: [
      {
        configPath: 'isolation/isolation_config',
        title: '隔离配置',
        description: '工作空间、容器、权限策略配置',
        icon: '🔒',
      },
      {
        configPath: 'isolation/isolation_policy',
        title: '隔离策略',
        description: '工具隔离策略、分类规则配置',
        icon: '🛡️',
      },
      {
        configPath: 'isolation/security_rules',
        title: '安全规则',
        description: '命令黑名单、路径保护、SSRF 防护规则',
        icon: '⚠️',
      },
      {
        configPath: 'isolation/approval',
        title: '审批配置',
        description: '高风险操作审批策略和用户信任配置',
        icon: '✅',
      },
    ],
  },
  {
    name: '评估',
    items: [
      {
        configPath: 'evaluation/evaluation_metrics',
        title: '评估指标',
        description: '内置评估指标定义（文件检查、语义检查等）',
        icon: '📊',
      },
    ],
  },
  {
    name: '能力适配器',
    items: [
      {
        configPath: 'capability_adapters',
        title: '能力适配器',
        description: '外部连接器注册（VSCode、ComfyUI、Playwright 等）',
        icon: '🔌',
      },
    ],
  },
  {
    name: '外部工具',
    items: [
      {
        configPath: 'external_tools/default',
        title: '外部工具 · 默认',
        description: '默认外部工具配置',
        icon: '🔧',
      },
      {
        configPath: 'external_tools/godot',
        title: '外部工具 · Godot',
        description: 'Godot 引擎外部工具配置',
        icon: '🎮',
      },
      {
        configPath: 'external_tools/vscode',
        title: '外部工具 · VSCode',
        description: 'VSCode 外部工具配置',
        icon: '💻',
      },
    ],
  },
  {
    name: '管道配置',
    items: [
      {
        configPath: 'pipelines/default',
        title: '管道 · 默认',
        description: '默认管道插件链配置',
        icon: '🔄',
      },
      {
        configPath: 'pipelines/l1-main',
        title: '管道 · L1 主 Agent',
        description: 'L1 主 Agent 管道配置',
        icon: '👤',
      },
      {
        configPath: 'pipelines/l2-evaluator',
        title: '管道 · L2 评估',
        description: 'L2 评估 Agent 管道配置',
        icon: '🔍',
      },
      {
        configPath: 'pipelines/l2-subtask',
        title: '管道 · L2 子任务',
        description: 'L2 子任务 Agent 管道配置',
        icon: '📋',
      },
    ],
  },
]

/**
 * 所有配置项的平面列表
 */
export const ALL_GENERIC_CONFIGS: GenericConfigEntry[] = CONFIG_GROUPS.flatMap((g) => g.items)

/**
 * 根据 configPath 查找配置项元数据
 */
export function findConfigEntry(configPath: string): GenericConfigEntry | undefined {
  return ALL_GENERIC_CONFIGS.find((c) => c.configPath === configPath)
}
