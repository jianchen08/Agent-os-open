"""system 提示词条件注入引擎（声明式单文件规则）。

定位（方案 docs/working/提示词条件注入引擎方案_20261018.md，设计真值）：
- pull 模型：规则文件（``prompts/contributions.yaml``，单文件自包含）躺在
  贡献者自己目录，prompt_build 在组装时刻读取求值；内容内联，不经过 state
  搬运，无独立写入时机；
- 条件键白名单 = 任务稳定键（任务内不变）：组装每轮重算且逐字节稳定，
  system 前缀缓存依赖此；白名单外的可变键（iteration 等）整条拒载
  （fail-closed），防任务中途注入漂移；
- 模型条件读落定键 ``state.model_id``（agent_config_load 初始化时落定），
  回退候选 ``state.model_tier``（落定步未接线时的兼容——同为任务稳定值，
  非漂移源）；禁用 ``llm_model``（llm_core 执行后回写、首轮缺席）；
- 治理参数内建：order 撞号（enabled 规则间判定，disabled 惰性不参与）整批
  拒载、单条/总量预算、文件大小上限、未知键 strict 拒载（防拼写错让规则
  比声明面更宽）、解析失败零注入（永不抛错打断提示词构建）、命中/拒载
  audit 日志；
- 位置固定 system 尾部 append（由 prompt_build 决定），规则不给位置语义。

装载面：构造时加载一次，之后按文件 mtime/size 指纹在每次 render 前探测
（管理面热改即生效，无需插件重载；同任务内管理员改动 = 显式内容变更，
下一次组装生效）。
"""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# 默认预算（prompt_build 组装参数，可经构造覆盖）
DEFAULT_MAX_RULE_CHARS = 16384  # 单条内容上限（对齐 PROJECT_AGENTS_MD_MAX_CHARS 量级）
DEFAULT_MAX_TOTAL_CHARS = 65536  # 命中内容总量上限
DEFAULT_MAX_FILE_BYTES = 1_048_576  # 规则文件大小上限（防 yaml 炸弹）

# when.state 允许的条件键白名单：任务稳定键（任务内不变）。含点号的键支持
# 平铺键优先、嵌套 dict 逐段下钻（execution_context.* 形态）。
STATE_KEY_WHITELIST = frozenset(
    {
        "agent_id",
        "core_type",
        "core_plugin",
        "pipeline_id",
        "session_id",
        "user_id",
        "model_id",
        "model_tier",
        "level",
        "category",
        "agent_type",
        "thinking_strength",
        "execution_context.mode",
        "execution_context.workspace.mode",
        "execution_context.isolation.level",
    }
)

# 严格键集：未知键拒载（拼写错静默忽略会让规则比声明面更宽）
_RULE_KEYS = frozenset({"name", "enabled", "when", "inject"})
_WHEN_KEYS = frozenset({"model", "agents", "tools", "state"})
_INJECT_KEYS = frozenset({"order", "content"})

_DEFAULT_ORDER = 100


@dataclass(frozen=True)
class Rule:
    """一条校验通过的注入规则。"""

    name: str
    order: int
    when: Mapping[str, Any]
    content: str
    enabled: bool = True


def _is_pattern(value: Any) -> bool:
    """条件值形态：字符串或字符串列表（fnmatch 模式）。"""
    if isinstance(value, str):
        return True
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _norm_value(value: Any) -> str:
    """条件比对值归一：布尔 → true/false（大小写不敏感比对），其余 str 化。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _value_matches(value: Any, pattern: Any) -> bool:
    """值 vs 模式（str 或列表，任一命中即命中）；空值恒不命中。"""
    norm = _norm_value(value)
    if not norm:
        return False
    patterns = [pattern] if isinstance(pattern, str) else list(pattern)
    return any(fnmatch.fnmatchcase(norm, p) for p in patterns)


def _lookup_state(state: Mapping[str, Any], key: str) -> Any:
    """state 取值：平铺键优先，含点号的键逐段下钻嵌套 dict；缺失返回 None。"""
    if key in state:
        return state[key]
    current: Any = state
    for segment in key.split("."):
        if isinstance(current, Mapping) and segment in current:
            current = current[segment]
        else:
            return None
    return current


class ContributionEngine:
    """条件注入规则引擎：加载校验 + 组装时刻纯函数求值。

    用法：``engine = ContributionEngine(plugin_dir / "prompts" / "contributions.yaml")``；
    组装时 ``section = engine.render(ctx.state)``（零命中返回空串）。
    任何解析/装载问题都降级为零注入 + 告警日志，绝不抛错。
    """

    def __init__(
        self,
        path: Path | str,
        *,
        max_rule_chars: int = DEFAULT_MAX_RULE_CHARS,
        max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self.path = Path(path)
        self._max_rule_chars = max_rule_chars
        self._max_total_chars = max_total_chars
        self._max_file_bytes = max_file_bytes
        self._rules: list[Rule] = []
        self._fingerprint: tuple[int, int] | None = None
        self._reload()

    # ── 装载 ──

    def _reload_if_changed(self) -> None:
        """文件 mtime/size 指纹变化即重载（管理面热改即生效）。"""
        try:
            stat = self.path.stat()
            fingerprint: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            fingerprint = None
        if fingerprint != self._fingerprint:
            logger.info("[contributions] 规则文件变化，重载: %s", self.path)
            self._reload()

    def _reload(self) -> None:
        """读盘 → 解析 → 校验（fail-closed：任何致命问题零注入 + 告警）。"""
        try:
            stat = self.path.stat()
        except OSError:
            self._fingerprint = None
            self._rules = []
            return
        self._fingerprint = (stat.st_mtime_ns, stat.st_size)
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            logger.warning("[contributions] 规则文件读取失败，零注入: %s | err=%s", self.path, exc)
            self._rules = []
            return
        if len(raw) > self._max_file_bytes:
            logger.warning(
                "[contributions] 规则文件超大小上限拒载（%d > %d）: %s",
                len(raw),
                self._max_file_bytes,
                self.path,
            )
            self._rules = []
            return
        try:
            data = yaml.safe_load(raw.decode("utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            logger.warning("[contributions] 规则文件解析失败，零注入: %s | err=%s", self.path, exc)
            self._rules = []
            return
        self._rules = self._validate(data)

    def _validate(self, data: Any) -> list[Rule]:
        """结构校验：逐条拒载 + order 撞号整批拒载（确定、可归因）。"""
        if data is None:
            return []
        if not isinstance(data, Mapping):
            logger.warning("[contributions] 规则文档顶层非映射，零注入: %s", self.path)
            return []
        raw_rules = data.get("rules")
        if raw_rules is None:
            return []
        if not isinstance(raw_rules, list):
            logger.warning("[contributions] rules 字段非列表，零注入: %s", self.path)
            return []
        parsed: list[Rule] = []
        for index, item in enumerate(raw_rules):
            rule = self._validate_rule(index, item)
            if rule is not None:
                parsed.append(rule)
        enabled = [r for r in parsed if r.enabled]
        for rule in parsed:
            if not rule.enabled:
                logger.info("[contributions] 规则 disabled 跳过（惰性不参与撞号判定）: %s", rule.name)
        orders = [r.order for r in enabled]
        duplicates = sorted({o for o in orders if orders.count(o) > 1})
        if duplicates:
            logger.warning(
                "[contributions] order 撞号整批拒载（%s），零注入: %s",
                duplicates,
                self.path,
            )
            return []
        return sorted(enabled, key=lambda r: r.order)

    def _validate_rule(self, index: int, item: Any) -> Rule | None:
        """单条规则校验；任何缺陷 → None + warning（拒载该条，不影响他条）。"""
        if not isinstance(item, Mapping):
            logger.warning("[contributions] 第 %d 条规则非映射，拒载", index)
            return None
        unknown = set(item.keys()) - _RULE_KEYS
        if unknown:
            logger.warning("[contributions] 第 %d 条规则含未知键 %s，拒载", index, sorted(unknown))
            return None
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            logger.warning("[contributions] 第 %d 条规则缺 name，拒载", index)
            return None
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            logger.warning("[contributions] 规则 %s enabled 非布尔，拒载", name)
            return None
        inject = item.get("inject")
        if not isinstance(inject, Mapping):
            logger.warning("[contributions] 规则 %s 缺 inject 映射，拒载", name)
            return None
        unknown_inject = set(inject.keys()) - _INJECT_KEYS
        if unknown_inject:
            logger.warning(
                "[contributions] 规则 %s inject 含未知键 %s，拒载", name, sorted(unknown_inject)
            )
            return None
        order = inject.get("order", _DEFAULT_ORDER)
        if isinstance(order, bool) or not isinstance(order, int):
            logger.warning("[contributions] 规则 %s inject.order 非整数，拒载", name)
            return None
        content = inject.get("content")
        if not isinstance(content, str) or not content.strip():
            logger.warning("[contributions] 规则 %s content 缺失/空，拒载", name)
            return None
        if len(content) > self._max_rule_chars:
            logger.warning(
                "[contributions] 规则 %s 内容超单条预算（%d > %d），拒载",
                name,
                len(content),
                self._max_rule_chars,
            )
            return None
        when = item.get("when") or {}
        if not isinstance(when, Mapping):
            logger.warning("[contributions] 规则 %s when 非映射，拒载", name)
            return None
        unknown_when = set(when.keys()) - _WHEN_KEYS
        if unknown_when:
            logger.warning(
                "[contributions] 规则 %s when 含未知键 %s，拒载", name, sorted(unknown_when)
            )
            return None
        for key in ("model", "agents"):
            pattern = when.get(key)
            if pattern is not None and not _is_pattern(pattern):
                logger.warning("[contributions] 规则 %s when.%s 非模式形态，拒载", name, key)
                return None
        tools = when.get("tools")
        if tools is not None and not (
            isinstance(tools, list) and all(isinstance(t, str) and t for t in tools)
        ):
            logger.warning("[contributions] 规则 %s when.tools 非字符串列表，拒载", name)
            return None
        state_cond = when.get("state")
        if state_cond is not None:
            if not isinstance(state_cond, Mapping):
                logger.warning("[contributions] 规则 %s when.state 非映射，拒载", name)
                return None
            bad_keys = [k for k in state_cond if k not in STATE_KEY_WHITELIST]
            if bad_keys:
                logger.warning(
                    "[contributions] 规则 %s when.state 含非任务稳定键 %s，拒载（白名单外禁止条件）",
                    name,
                    bad_keys,
                )
                return None
        return Rule(name=name, order=order, when=when, content=content, enabled=enabled)

    # ── 求值 ──

    def render(self, state: Mapping[str, Any]) -> str:
        """组装时刻求值：命中规则内容按 order 拼接；零命中返回空串。

        纯函数（快照语义）：同一 state 与同一规则集恒得同一输出——system
        前缀缓存稳定性依赖此。
        """
        self._reload_if_changed()
        matched: list[Rule] = []
        total = 0
        for rule in self._rules:
            if not self._matches(rule, state):
                continue
            if total + len(rule.content) > self._max_total_chars:
                logger.warning(
                    "[contributions] 总量超预算拒载 rule=%s（chars=%d, total=%d）",
                    rule.name,
                    len(rule.content),
                    total,
                )
                continue
            matched.append(rule)
            total += len(rule.content)
        if not matched:
            return ""
        for rule in matched:
            logger.info(
                "[contributions] 命中 rule=%s order=%d chars=%d", rule.name, rule.order, len(rule.content)
            )
        return "\n\n".join(rule.content for rule in matched)

    @staticmethod
    def _matches(rule: Rule, state: Mapping[str, Any]) -> bool:
        """when 全条件 AND；缺省 when = 无条件命中。"""
        when = rule.when
        model_pattern = when.get("model")
        if model_pattern is not None:
            candidates = (state.get("model_id"), state.get("model_tier"))
            if not any(_value_matches(c, model_pattern) for c in candidates):
                return False
        agents_pattern = when.get("agents")
        if agents_pattern is not None and not _value_matches(state.get("agent_id"), agents_pattern):
            return False
        tools = when.get("tools")
        if tools:
            present = state.get("tool_ids")
            present_set = (
                {str(t) for t in present} if isinstance(present, (list, tuple, set)) else set()
            )
            if not all(t in present_set for t in tools):
                return False
        state_cond = when.get("state") or {}
        return all(
            _value_matches(_lookup_state(state, key), pattern)
            for key, pattern in state_cond.items()
        )
