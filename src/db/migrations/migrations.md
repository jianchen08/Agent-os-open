# 迁移组件

## 需求
### 职责
提供数据库迁移管理功能，支持迁移脚本的创建、应用、回滚和版本跟踪。

### 对外接口
- 输入：迁移命令（status/migrate/rollback/create）
- 输出：迁移状态、执行结果

### 依赖
- 外部依赖：sqlalchemy、yaml
- 内部依赖：src.db.connection（数据库连接）

## 逻辑
### 流程设计
1. **迁移创建**：生成带版本号的 YAML 迁移文件
2. **状态检查**：查询 schema_migrations 表获取已应用版本
3. **迁移应用**：执行 up SQL，记录版本
4. **迁移回滚**：执行 down SQL，删除版本记录

### 数据流向
```
迁移文件 -> MigrationManager -> schema_migrations 表 -> 数据库 Schema 变更
```

### 数据模型
#### Migration（迁移定义）
| 字段 | 类型 | 说明 |
|---|---|---|
| version | str | 版本号（YYYYMMDDHHMMSS） |
| name | str | 迁移名称 |
| up_sql | str | 升级 SQL |
| down_sql | str | 降级 SQL |
| description | str | 描述 |

#### schema_migrations 表
| 字段 | 类型 | 说明 |
|---|---|---|
| version | VARCHAR(255) | 版本号（主键） |
| name | VARCHAR(255) | 迁移名称 |
| applied_at | TIMESTAMP | 应用时间 |

### 配置设计
| 配置项 | 说明 | 默认值 |
|---|---|---|
| migration_dir | 迁移脚本目录 | src/db/migrations/scripts |

### 错误处理
- 迁移文件加载失败：跳过并记录警告
- SQL 执行失败：事务回滚
- 版本不存在：抛出 ValueError

## 结构
### 子组件清单
无

### 文件清单（代码文件 - 具体接口）
#### manager.py
职责：迁移管理器核心实现
暴露接口：
- `Migration`：迁移定义类
  - `full_name -> str`：完整迁移名称
- `MigrationManager`：迁移管理器类
  - `get_status() -> dict`：获取迁移状态
  - `migrate(target_version: str | None, session: AsyncSession | None) -> list[str]`：执行迁移
  - `rollback(target_version: str | None, steps: int) -> list[str]`：回滚迁移
  - `create_migration(name: str, description: str | None) -> Path`：创建迁移文件
- `migration_manager`：全局迁移管理器实例

#### runner.py
职责：命令行运行器
暴露接口：
- `main() -> None`：命令行主函数

#### scripts/
职责：迁移脚本存放目录
包含：
- YAML 格式的迁移脚本文件
- Python 格式的迁移脚本文件

### 测试策略
#### 组件测试
- 单元测试：迁移文件解析、版本比较
- 集成测试：迁移应用和回滚
- 覆盖率要求：核心逻辑 >= 80%

## 实现
-> 见代码文件：src/db/migrations/
