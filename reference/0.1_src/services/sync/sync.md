# 同步服务组件

## 需求
### 职责
提供 YAML 配置文件到数据库同步的通用功能，核心原则是 YAML 文件是配置的唯一来源，数据库用于运行时读取。

### 对外接口
- 输入：配置目录路径、数据库会话
- 输出：同步统计结果（created、updated、skipped、failed）

### 依赖
- 依赖模块：sqlalchemy（数据库操作）
- 依赖模块：yaml（YAML 解析）

## 逻辑
### 流程设计
1. 扫描配置目录中的 YAML 文件
2. 计算配置的校验和（MD5）
3. 与数据库中的记录比较
4. 创建新记录或更新已有记录

### 数据流向
```
YAML文件 → 解析 → 计算校验和 → 与数据库比较 → 创建/更新/跳过
```

### 配置设计
#### 同步配置
| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| config_dir | 配置目录 | 子类指定 |
| force | 强制同步 | False |

### 错误处理
- 跳过无效配置文件（缺少必需字段）
- 记录同步失败的文件和错误信息

## 结构
### 子组件清单（文件夹 - 抽象说明）
无子组件，为原子服务组件。

### 文件清单（代码文件 - 具体接口）
#### base.py
职责：YAML 配置同步服务基类
暴露接口：
- `YamlConfigSyncService`：YAML 配置同步服务抽象基类
  - `__init__(config_dir: Path | None = None)`：初始化
  - `async sync_all(session: AsyncSession, force: bool = False) -> dict[str, int]`：同步所有配置
  - `async sync_one(session: AsyncSession, yaml_file: Path, force: bool = False) -> str`：同步单个配置
  - `_calculate_checksum(data: dict) -> str`：计算配置校验和
  - `_scan_yaml_files() -> list[Path]`：扫描 YAML 文件
  - `_should_skip_file(yaml_file: Path) -> bool`：判断是否跳过文件

  抽象方法（子类必须实现）：
  - `_get_default_config_dir() -> Path`：获取默认配置目录
  - `_get_config_id_field() -> str`：获取配置 ID 字段名
  - `_get_entity_class() -> type`：获取数据库实体类
  - `_get_entity_id_field() -> str`：获取实体 ID 字段名
  - `_get_checksum_from_entity(entity: Any) -> str | None`：从实体获取校验和
  - `_prepare_entity_data(data: dict, checksum: str) -> dict`：准备实体数据
  - `_get_log_prefix() -> str`：获取日志前缀

### 测试策略
#### 组件测试
- 单元测试：校验和计算、文件扫描
- 集成测试：完整同步流程
- Mock策略：数据库会话 Mock

## 实现
→ 见代码文件
