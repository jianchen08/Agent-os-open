## 修复：定时触发器（schedule 类型）不触发

### 根因（已用日志 + 复现脚本确认）

用户设置「12:07 触发」，工具入参 `schedule_time="2026-07-08T12:07:00"`（naive，无时区）。

`src/tools/builtin/trigger_setup/tool.py` 的 `_setup_schedule_trigger`（line 500-513）：
- `datetime.fromisoformat(...)` 得到 **naive** datetime（用户的本地时间 12:07）
- 用 `datetime.utcnow()`（也是 naive）做「过去时间」校验，**校验通过**（因为本地 12:07 > UTC 04:07 的字面值）
- naive datetime 直接存入 `config.scheduled_at`

`src/triggers/manager.py` 的 `_check_scheduled_time` → `_normalize_datetime`（line 1025-1026）：
- naive datetime 被当作 **UTC** 加上 `tzinfo=utc` → 变成 `12:07:00+00:00`
- 真实 UTC 约 `04:xx`（北京 12:xx），`04:xx >= 12:07` 恒为 **False** → **永不触发**
- 实际要等到真实 UTC 12:07（北京 20:07）才触发，晚 8 小时

**铁证**：日志显示 `trigger_schedule_4846ccdad04c`（目标12:07）、`trigger_schedule_a4376cbb8dcf`（目标12:10）均成功注册且管道持续挂起等待唤醒，但从未出现「消息已注入」；而同期的 delay 类型（10秒/20秒）均正常触发。后台线程、主事件循环、消息注入通道全部正常——唯一差异就是 schedule 的时间解释错误。

之前 REQ-3 只修了 manager 里 aware/naive 比较 TypeError 的问题，**没修源头**（tool.py 把本地 naive 时间误存）。现有测试 `test_trigger_timezone.py` 用 `now(UTC)±1s` 构造 aware `scheduled_at`，绕过了真实用户路径，故未覆盖此 bug。

### 修复方案（外科手术式，仅改 1 个函数）

**改动文件**：`src/tools/builtin/trigger_setup/tool.py` 的 `_setup_schedule_trigger`（line 484-554）

**核心逻辑**：以 `APP_TIMEZONE`（=本地时区 `Asia/Shanghai`）为标准解释用户输入
1. `fromisoformat` 解析后，若 naive（无 tzinfo）→ 视为 `APP_TIMEZONE` 本地时间，用 `ZoneInfo(tz).localize()` 或 `replace(tzinfo=ZoneInfo(tz))` 打上本地时区，再 `.astimezone(UTC)` 转成 aware UTC 存入 `scheduled_at`
2. 若 aware（用户带了 `+08:00`/`Z`）→ 直接 `.astimezone(UTC)` 统一到 UTC
3. 「过去时间」校验和「7天上限」校验统一改用 `datetime.now(UTC)`（aware），替换废弃的 `datetime.utcnow()`（naive）——避免 aware vs naive 比较隐患，顺带消除 deprecation warning
4. `manager.py` **不改**：`_normalize_datetime` 对 aware datetime 会正确 `.astimezone(UTC)`，比较天然正确

**导入补充**：`from src.config.settings import get_settings` 和 `from zoneinfo import ZoneInfo`（与同目录 `executor.py`、`prompt_build/plugin.py` 的既有模式一致）

时区名无效时的降级：沿用 `prompt_build/plugin.py` 既有做法——`ZoneInfo` 抛异常则 fallback 到 UTC 并 log warning（用户可见、状态可感知，符合降级铁律）。

### 为何是最小改动
- 触发器只 DELAY/SCHEDULED/INTERVAL 受后台线程调度；DELAY/INTERVAL 用 `register_time`+秒数，不涉时区，已正常工作
- 唯一时间解释错误点就是 SCHEDULED 的 `scheduled_at` 源头
- `scheduled_at` 无其它消费者（已确认 registry/state_manager/trigger_review 均未引用）
- 改 1 个函数即可让 schedule 触发器在本地时间准时触发

### 验证
1. 新增针对「naive 本地时间字符串」路径的测试（现有测试只覆盖 aware UTC 路径，是盲区）：构造 `schedule_time="2026-07-08T12:07:00"`（naive），mock `APP_TIMEZONE=Asia/Shanghai`，断言存入的 `scheduled_at` 等于 `12:07:00+08:00`（即 UTC `04:07:00+00:00`），且 `check_scheduled` 在 UTC 04:07 时触发、04:06 时不触发
2. 跑现有 `tests/test_trigger_timezone.py` 确保不回归
3. 不改动 manager.py，避免影响已正常的 DELAY/INTERVAL

### 不做的事
- 不改 `manager.py`（`_normalize_datetime` 对 aware 已正确）
- 不改 `time_trigger.py`（旧 APScheduler 系统，当前未被 `trigger_setup` 工具使用，不在本次故障路径）
- 不动 `APP_TIMEZONE` 配置语义