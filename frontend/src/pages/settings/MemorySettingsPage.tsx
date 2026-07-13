/**
 * 记忆配置页面
 *
 * 管理记忆存储后端、向量检索、维护配置。
 * 映射后端配置：system/memory_storage
 */

import { CategoryConfigPage } from '@/components/config/CategoryConfigPage'
import type { CategoryTabConfig } from '@/components/config/CategoryConfigPage'

const TABS: CategoryTabConfig[] = [
  { configPath: 'system/memory_storage', title: '记忆存储' },
]

/** 字段路径 → 中文标签映射 */
const LABEL_MAP: Record<string, string> = {
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
}

/**
 * 记忆配置页面组件
 */
export function MemorySettingsPage() {
  return (
    <CategoryConfigPage
      title="记忆配置"
      description="记忆存储后端、向量检索、维护配置"
      tabs={TABS}
      labelMap={LABEL_MAP}
    />
  )
}
