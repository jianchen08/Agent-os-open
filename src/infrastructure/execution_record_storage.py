"""执行记录存储 — YAML 多文件持久化。

记录管道每轮迭代的执行详情，包括 LLM 输出摘要、工具结果摘要、
token 用量、耗时和错误信息。按 pipeline_run_id 拆分为独立 YAML 文件。

存储模式：
- 按 pipeline_run_id 拆分为独立 YAML 文件
- 目录结构：data/pipelines/{pipeline_run_id}.yaml
- 每个 YAML 文件包含 summary 和 records 两部分
- 内存缓存 + 文件持久化
- 同步 API（单管道顺序写入）
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_SUMMARY_MAX_LEN = 500
_MAX_RECORDS_PER_FILE = 500


def _fix_records_empty_flow(text: str) -> str:
    """修复 YAML 中 records: [] 后追加序列项导致的解析错误。

    旧版 _update_summary_in_file 写入 "records: []"，而 _append_record_to_file
    追加 "- record_id: ..."，两者混合产生无效 YAML。将 "records: []" 替换为
    "records:" 即可恢复正确格式。
    """
    return re.sub(r'^records:\s*\[\]\s*$', 'records:', text, flags=re.MULTILINE)


@dataclass
class ExecutionRecordData:
    """L0 原始执行记录（压缩体系的 L0 层）。

    保存每个原子动作的原始消息内容，
    作为 L0→L1→L2 压缩链路的输入。

    一次 LLM 输出 = 一条 type=ai 记录（保存完整输出）
    一次工具调用 = 一条 type=tool 记录（保存输入+输出）
    """

    record_id: str = ""
    pipeline_run_id: str = ""

    type: str = "ai"
    name: str | None = None

    sequence: int = 0
    iteration: int = 0

    role: str = ""
    content: str = ""
    tool_call_id: str | None = None
    tool_input: dict[str, Any] | None = None
    thinking_content: str | None = None
    tool_calls_json: str | None = None

    container_task_id: str | None = None

    error: str | None = None

    # 前端乐观消息 ID，用于 API 历史加载时与本地临时消息对账（消除重复/丢失）
    client_message_id: str | None = None

    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.record_id:
            self.record_id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


@dataclass
class PipelineRunSummary:
    """管道运行摘要（每次 pipeline_run 产生一条）。

    由 PipelineEngine 在运行结束时写入，
    用于成本统计、运行日志、/cost 命令等场景。
    """

    run_id: str = ""
    thread_id: str = ""

    total_iterations: int = 0
    total_tokens: dict[str, int] = field(default_factory=dict)
    total_seconds: float = 0.0
    total_records: int = 0

    status: str = ""
    final_output: str = ""
    error: str | None = None

    review_status: str = "pending"       # "pending" 或 "reviewed"
    reviewed_at: str | None = None       # 复盘完成时间

    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


class ExecutionRecordStorage:
    """执行记录存储 — 内存缓存 + YAML 多文件持久化。

    按 pipeline_run_id 拆分为独立 YAML 文件，每个文件包含 summary 和 records。
    内存缓存按 pipeline_run_id 分组的嵌套结构。

    Attributes:
        _records: 内存中的记录缓存（record_id -> ExecutionRecordData）
        _summaries: 内存中的摘要缓存（run_id -> PipelineRunSummary）
        _pipelines: 按 pipeline_run_id 分组的管道数据（run_id -> {summary, records}）
        _data_dir: YAML 文件目录路径
    """

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._records: dict[str, ExecutionRecordData] = {}
        self._summaries: dict[str, PipelineRunSummary] = {}
        self._loaded_pipelines: set[str] = set()
        self._data_dir = Path(data_dir) if data_dir else None
        if self._data_dir:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        self._pipeline_root_map: dict[str, str] = {}
        self._map_file = self._data_dir / "_pipeline_root_map.json" if self._data_dir else None
        if self._map_file:
            self._load_root_map()
        # pipeline_run_id -> current part number
        self._active_part: dict[str, int] = {}
        # 标记是否已通过 _load_all_summaries_only 加载过全部 summary（避免重复解析）
        self._all_summaries_loaded: bool = False
        self._records_in_active_file: dict[str, int] = {}

    def _load_all(self) -> None:
        if not self._data_dir:
            return
        # 扁平文件（向后兼容）
        for yaml_file in sorted(self._data_dir.glob("*.yaml")):
            self._load_pipeline_file(yaml_file)
        # 子目录中的分组文件
        for subdir in sorted(self._data_dir.iterdir()):
            if not subdir.is_dir():
                continue
            for yaml_file in sorted(subdir.glob("*.yaml")):
                self._load_pipeline_file(yaml_file)

    def _ensure_loaded(self, pipeline_run_id: str) -> None:
        """按需加载指定 pipeline 的所有分片文件（懒加载）。"""
        if pipeline_run_id in self._loaded_pipelines or not self._data_dir:
            return
        part_files = self._get_part_files(pipeline_run_id)
        for pf in part_files:
            self._load_pipeline_file(pf)
        if part_files:
            self._detect_active_part(pipeline_run_id, part_files)
            active_file = part_files[-1]
            try:
                text = active_file.read_text(encoding="utf-8")
                data = yaml.safe_load(text)
                if isinstance(data, dict):
                    recs = data.get("records") or []
                    self._records_in_active_file[pipeline_run_id] = len(recs)
            except Exception:
                logger.warning(
                    "活跃分片记录数检测失败，设为 0: pipeline=%s, file=%s",
                    pipeline_run_id,
                    getattr(active_file, "name", "?"),
                )
                self._records_in_active_file[pipeline_run_id] = 0
        self._loaded_pipelines.add(pipeline_run_id)

    def _append_record_to_file(self, record: ExecutionRecordData) -> None:
        """追加单条记录到 YAML 文件末尾，避免全量重写。

        利用 YAML 序列语法：records 列表项以 '- ' 开头，
        直接在文件末尾追加一条即可被正确解析。

        分片策略：内存维护 _records_in_active_file 计数器，
        达到 _MAX_RECORDS_PER_FILE 时切换到新文件。
        """
        if not self._data_dir:
            return
        pipeline_run_id = record.pipeline_run_id
        part = self._active_part.get(pipeline_run_id, 1)
        file_path = self._get_pipeline_file(pipeline_run_id, part=part)
        if file_path is None:
            return

        record_dict = self._record_to_dict(record)
        record_yaml = yaml.safe_dump(
            [record_dict],
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            indent=2,
        )

        if not file_path.exists():
            file_path.parent.mkdir(parents=True, exist_ok=True)
            header = "summary: null\nrecords:\n"
            file_path.write_text(header + record_yaml, encoding="utf-8")
            self._records_in_active_file[pipeline_run_id] = 1
        else:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write("\n")
                f.write(record_yaml)
            current = self._records_in_active_file.get(pipeline_run_id, 0)
            self._records_in_active_file[pipeline_run_id] = current + 1

        if self._records_in_active_file.get(pipeline_run_id, 0) >= _MAX_RECORDS_PER_FILE:
            self._active_part[pipeline_run_id] = part + 1
            self._records_in_active_file[pipeline_run_id] = 0

    def _update_summary_in_file(self, pipeline_run_id: str) -> None:
        """文本级替换 YAML 文件中的 summary 段，避免全量重写 records。

        定位文件开头的 'summary:' 到 '\\nrecords:' 之间的内容，
        替换为最新的 summary YAML 文本。
        """
        if not self._data_dir:
            return
        part = self._active_part.get(pipeline_run_id, 1)
        file_path = self._get_pipeline_file(pipeline_run_id, part=part)
        if file_path is None:
            return

        summary = self._summaries.get(pipeline_run_id)
        summary_dict = self._summary_to_dict(summary) if summary else None
        new_summary_text = yaml.safe_dump(
            {"summary": summary_dict},
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            indent=2,
        ).rstrip("\n")

        if not file_path.exists():
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(
                new_summary_text + "\nrecords:\n",
                encoding="utf-8",
            )
            return

        text = file_path.read_text(encoding="utf-8")
        # 使用正则匹配文件顶层的 "records:" 行，排除 record 内容中的嵌套匹配
        _records_marker = re.search(r'^records:', text, re.MULTILINE)
        if _records_marker is None:
            logger.warning("YAML 文件格式异常，无法定位 records 段: %s", file_path.name)
            return
        marker_idx = _records_marker.start()
        # 保留 records: 及其后面的所有内容
        new_text = new_summary_text + "\n" + text[marker_idx:]
        file_path.write_text(new_text, encoding="utf-8")

    def _detect_active_part(
        self, pipeline_run_id: str, part_files: list[Path]
    ) -> None:
        """从文件列表推断活跃分片编号。"""
        last = part_files[-1]
        name = last.name
        if "_" in name and name.endswith(".yaml"):
            suffix = name.rsplit("_", 1)[-1].replace(".yaml", "")
            try:
                self._active_part[pipeline_run_id] = int(suffix)
                return
            except ValueError:
                pass
        self._active_part[pipeline_run_id] = 1

    def _load_root_map(self) -> None:
        if not self._map_file or not self._map_file.exists():
            return
        try:
            text = self._map_file.read_text(encoding="utf-8")
            self._pipeline_root_map = json.loads(text)
        except Exception:
            logger.warning("管道映射文件损坏，使用空映射: %s", self._map_file)
            self._pipeline_root_map = {}

    def _load_pipeline_file(self, yaml_file: Path) -> None:
        try:
            text = yaml_file.read_text(encoding="utf-8")
            # 修复损坏文件：将 "records: []" 替换为 "records:"
            text = _fix_records_empty_flow(text)
            data = yaml.safe_load(text)
            if not isinstance(data, dict):
                return
            summary_dict = data.get("summary")
            if summary_dict and isinstance(summary_dict, dict):
                summary = self._dict_to_summary(summary_dict)
                self._summaries[summary.run_id] = summary
            records_list = data.get("records")
            if records_list and isinstance(records_list, list):
                for record_dict in records_list:
                    if isinstance(record_dict, dict):
                        record = self._dict_to_record(record_dict)
                        self._records[record.record_id] = record
        except Exception:
            logger.warning("管道文件损坏，跳过: %s", yaml_file.name)

    def _load_all_summaries_only(self) -> None:
        """仅加载所有 YAML 文件的 summary 部分，跳过 records 解析。

        用于 list_all_summaries() 场景，只需要 summary 信息（如 thread_id），
        无需解析可能很大的 records 列表，显著减少内存和 CPU 开销。
        """
        if not self._data_dir:
            return
        # 扁平文件（向后兼容）
        for yaml_file in sorted(self._data_dir.glob("*.yaml")):
            self._load_summary_only(yaml_file)
        # 子目录中的分组文件
        for subdir in sorted(self._data_dir.iterdir()):
            if not subdir.is_dir():
                continue
            for yaml_file in sorted(subdir.glob("*.yaml")):
                self._load_summary_only(yaml_file)

    def _load_summary_only(self, yaml_file: Path) -> None:
        """从单个 YAML 文件中仅解析 summary 部分，跳过 records。

        Args:
            yaml_file: YAML 文件路径
        """
        try:
            text = yaml_file.read_text(encoding="utf-8")
            # 同 _load_pipeline_file 的修复逻辑
            text = _fix_records_empty_flow(text)
            data = yaml.safe_load(text)
            if not isinstance(data, dict):
                return
            summary_dict = data.get("summary")
            if summary_dict and isinstance(summary_dict, dict):
                summary = self._dict_to_summary(summary_dict)
                self._summaries[summary.run_id] = summary
        except Exception:
            logger.warning("管道文件损坏，跳过 summary 加载: %s", yaml_file.name)

    def _get_pipeline_file(
        self, pipeline_run_id: str, part: int | None = None
    ) -> Path | None:
        if not self._data_dir:
            return None
        root_id = self._pipeline_root_map.get(pipeline_run_id)
        base_dir = (
            self._data_dir / root_id if root_id else self._data_dir
        )
        if part is None:
            part = self._active_part.get(pipeline_run_id, 1)
        if part <= 1:
            return base_dir / f"{pipeline_run_id}.yaml"
        return base_dir / f"{pipeline_run_id}_{part:03d}.yaml"

    def _get_part_files(self, pipeline_run_id: str) -> list[Path]:
        """返回该 pipeline 所有分片文件，按编号升序。"""
        if not self._data_dir:
            return []
        root_id = self._pipeline_root_map.get(pipeline_run_id)
        base_dir = (
            self._data_dir / root_id if root_id else self._data_dir
        )
        if not base_dir.exists():
            return []
        files = [base_dir / f"{pipeline_run_id}.yaml"]
        files.extend(
            sorted(base_dir.glob(f"{pipeline_run_id}_*.yaml"))
        )
        return [f for f in files if f.exists()]

    @staticmethod
    def _record_to_dict(record: ExecutionRecordData) -> dict[str, Any]:
        try:
            return asdict(record)
        except TypeError:
            return ExecutionRecordStorage._safe_record_to_dict(record)

    @staticmethod
    def _safe_record_to_dict(record: ExecutionRecordData) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for fld in record.__dataclass_fields__:
            val = getattr(record, fld)
            if fld == "tool_input" and isinstance(val, dict):
                result[fld] = ExecutionRecordStorage._sanitize_dict(val)
            else:
                result[fld] = val
        return result

    @staticmethod
    def _sanitize_dict(d: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for k, v in d.items():
            if isinstance(v, (str, int, float, bool, type(None))):
                result[k] = v
            elif isinstance(v, dict):
                result[k] = ExecutionRecordStorage._sanitize_dict(v)
            elif isinstance(v, list):
                result[k] = [
                    ExecutionRecordStorage._sanitize_dict(i) if isinstance(i, dict)
                    else str(i) if not isinstance(i, (str, int, float, bool, type(None)))
                    else i
                    for i in v
                ]
            else:
                result[k] = str(v)
        return result

    @staticmethod
    def _dict_to_record(data: dict[str, Any]) -> ExecutionRecordData:
        return ExecutionRecordData(**data)

    @staticmethod
    def _summary_to_dict(summary: PipelineRunSummary) -> dict[str, Any]:
        return asdict(summary)

    @staticmethod
    def _dict_to_summary(data: dict[str, Any]) -> PipelineRunSummary:
        return PipelineRunSummary(**data)

    def save(self, record: ExecutionRecordData) -> str:
        if not record.record_id:
            record.record_id = uuid.uuid4().hex[:12]
        if not record.created_at:
            record.created_at = datetime.now().isoformat()
        self._records[record.record_id] = record
        if record.pipeline_run_id:
            self._loaded_pipelines.add(record.pipeline_run_id)
            self._append_record_to_file(record)
        self._all_summaries_loaded = False
        logger.debug("保存执行记录: %s (pipeline=%s, iteration=%d)",
                      record.record_id, record.pipeline_run_id, record.iteration)
        return record.record_id

    def get(self, record_id: str) -> ExecutionRecordData | None:
        return self._records.get(record_id)

    def list_by_session(
        self, session_id: str, limit: int = 50
    ) -> list[ExecutionRecordData]:
        self._ensure_loaded(session_id)
        records = [
            r for r in self._records.values() if r.pipeline_run_id == session_id
        ]
        records.sort(key=lambda r: r.iteration)
        return records[:limit]

    def count_by_session(self, session_id: str) -> int:
        self._ensure_loaded(session_id)
        return sum(
            1 for r in self._records.values() if r.pipeline_run_id == session_id
        )

    def delete_by_session(self, session_id: str) -> int:
        to_delete = [
            rid for rid, r in self._records.items()
            if r.pipeline_run_id == session_id
        ]
        for rid in to_delete:
            del self._records[rid]
        if session_id in self._summaries:
            del self._summaries[session_id]
        self._active_part.pop(session_id, None)
        self._records_in_active_file.pop(session_id, None)
        if to_delete and self._data_dir:
            for file_path in self._get_part_files(session_id):
                file_path.unlink()
            # 清理空目录
            root_id = self._pipeline_root_map.get(session_id)
            if root_id:
                parent_dir = self._data_dir / root_id
                try:
                    if parent_dir.exists() and not any(parent_dir.iterdir()):
                        parent_dir.rmdir()
                except OSError:
                    pass
            # 清理映射
            self._pipeline_root_map.pop(session_id, None)
            self._persist_root_map()
        # 删除会话记录后重置 summary 缓存标记
        self._all_summaries_loaded = False
        logger.debug("删除会话 %s 的执行记录: %d 条", session_id, len(to_delete))
        return len(to_delete)

    def list_by_pipeline(
        self,
        pipeline_run_id: str,
        limit: int | None = None,
        before_sequence: int | None = None,
        after_sequence: int | None = None,
    ) -> tuple[list[ExecutionRecordData], bool]:
        """
        加载指定管道的执行记录（支持游标分页）。

        FEATURE-pipeline_unify: 所有管道（主/子）统一通过 pipelineRunId 加载，
        该方法是唯一的消息加载入口，分页逻辑内联在此处。

        FEATURE-tail_read: 性能优化路径 — 传 limit 时从 YAML 文件尾部反向读取
        最近 N 条 record，避免全量反序列化 1.3MB 大文件（主管道 4-5s
        加载时间降到 0.3-0.8s）。

        调用契约:
          - limit=None: 保留原行为，全量加载所有 records（兼容 review_engine、
            reconstruct_messages 等需要完整历史的场景）。
          - limit=int:  走尾部反向读优化，只解析最近 N 条 record（适用于前端
            list_messages 翻页，主管道 4-5s → 0.3-0.8s）。

        Args:
            pipeline_run_id: 管道运行 ID
            limit: 返回的最大记录数（None 表示不限制，保留全量行为）
            before_sequence: 只返回 sequence < before_sequence 的记录（向上翻页）
            after_sequence: 只返回 sequence > after_sequence 的记录（断线补漏）

        Returns:
            (records, has_more) 元组，has_more 表示按 before_sequence 过滤后
            是否存在比 limit 更多的更早记录。
        """
        if limit is None:
            return self._list_by_pipeline_full(
                pipeline_run_id, before_sequence, after_sequence
            )

        if before_sequence is not None:
            return self._list_by_pipeline_full(
                pipeline_run_id, before_sequence, after_sequence, limit=limit
            )

        records, has_more = self.read_records_from_tail(
            pipeline_run_id,
            limit=limit,
            before_sequence=before_sequence,
            after_sequence=after_sequence,
        )
        if records:
            return records, has_more

        logger.debug(
            "[list_by_pipeline] 尾部读取无结果，fallback 全量加载: %s",
            pipeline_run_id,
        )
        return self._list_by_pipeline_full(
            pipeline_run_id, before_sequence, after_sequence, limit=limit
        )

    def _list_by_pipeline_full(
        self,
        pipeline_run_id: str,
        before_sequence: int | None,
        after_sequence: int | None,
        limit: int | None = None,
    ) -> tuple[list[ExecutionRecordData], bool]:
        """全量加载指定管道的 records，支持游标分页和 limit 截断。

        Args:
            pipeline_run_id: 管道运行 ID
            before_sequence: 只返回 sequence < before_sequence 的记录
            after_sequence: 只返回 sequence > after_sequence 的记录
            limit: 返回的最大记录数（None 不截断）

        Returns:
            (records, has_more) 元组
        """
        self._ensure_loaded(pipeline_run_id)
        records = [r for r in self._records.values() if r.pipeline_run_id == pipeline_run_id]
        records.sort(key=lambda r: (r.sequence, r.created_at or ""))

        if after_sequence is not None:
            return [r for r in records if r.sequence > after_sequence], False

        if before_sequence is not None:
            records = [r for r in records if r.sequence < before_sequence]

        has_more = limit is not None and len(records) > limit
        if limit is not None and len(records) > limit:
            records = records[-limit:]

        return records, has_more

    def reconstruct_messages(
        self,
        pipeline_run_id: str,
        budget: int | None = None,
        token_fn: Callable[..., int] | None = None,
    ) -> list[dict[str, Any]]:
        """从 L0 持久化记录回读近期消息（从后往前，按预算截取）。

        惰性加载：只读取当前活跃分片文件，预算不够时才往前读更早的分片。

        Args:
            pipeline_run_id: 管道运行 ID
            budget: token 预算限制（None 表示无限制）
            token_fn: token 估算函数，默认 len(text)//2

        Returns:
            消息字典列表（按时间顺序，旧的在前）
        """
        if token_fn is None:
            def _default_token_fn(text: str) -> int:
                return max(1, len(text) // 2) if text else 0
            token_fn = _default_token_fn

        # 内存回退：无 data_dir 时直接从缓存读取
        all_records = [
            r for r in self._records.values()
            if r.pipeline_run_id == pipeline_run_id
        ]
        all_records.sort(key=lambda r: (r.sequence, r.created_at or ""))

        if not all_records:
            # 磁盘回读：从分片文件倒序加载
            for pf in reversed(self._get_part_files(pipeline_run_id)):
                part_records = self._load_part_records(pf)
                all_records.extend(part_records)
                if budget is not None:
                    # 按预算判断是否需要继续读更早的分片
                    total = sum(token_fn(r.content or "") for r in all_records)
                    if total >= budget:
                        break
            all_records.sort(key=lambda r: (r.sequence, r.created_at or ""))

        if not all_records:
            return []

        return self._select_within_budget(all_records, budget, token_fn)

    def _select_within_budget(
        self,
        records: list[ExecutionRecordData],
        budget: int | None,
        token_fn: Callable[..., int],
    ) -> list[dict[str, Any]]:
        """从已排序的记录中，从后往前按预算截取消息。"""
        selected: list[ExecutionRecordData] = []
        used_tokens = 0
        pending_tools: list[ExecutionRecordData] = []

        for record in reversed(records):
            if record.type == "compression_marker":
                continue

            if record.type == "tool":
                pending_tools.append(record)
                continue

            rec_tokens = token_fn(record.content or "")
            if record.type == "ai" and record.tool_calls_json:
                rec_tokens += token_fn(record.tool_calls_json)

            tool_tokens = sum(
                token_fn(r.content or "") for r in pending_tools
            )
            total_tokens = rec_tokens + tool_tokens

            if budget is not None and used_tokens + total_tokens > budget:
                pending_tools.clear()
                break

            if pending_tools:
                selected.extend(pending_tools)
                used_tokens += tool_tokens
                pending_tools.clear()
            selected.append(record)
            used_tokens += rec_tokens

        pending_tools.clear()
        selected.reverse()
        return [self._record_to_message(r) for r in selected]

    def _load_part_records(self, file_path: Path) -> list[ExecutionRecordData]:
        """加载单个分片文件的记录（不更新全局缓存）。"""
        try:
            text = file_path.read_text(encoding="utf-8")
            data = yaml.safe_load(text)
            if not isinstance(data, dict):
                return []
            records_list = data.get("records", [])
            if not records_list or not isinstance(records_list, list):
                return []
            return [
                self._dict_to_record(d)
                for d in records_list
                if isinstance(d, dict)
            ]
        except Exception as exc:
            logger.warning("分片文件损坏，跳过加载: %s - %s", file_path.name, exc)
            return []

    # 尾部读取窗口大小：64KB 足够覆盖 20-50 条 record（按平均 2KB/record 估算）
    _TAIL_READ_BYTES = 64 * 1024
    # 扩大窗口上限：128KB，足够覆盖单条超大 record
    _TAIL_READ_BYTES_MAX = 128 * 1024

    def _extract_tail_blocks(self, yaml_file: Path, n: int) -> list[str]:
        """从单个 YAML 分片文件尾部提取最后 n 个 record 的文本块（不解 YAML）。

        FEATURE-tail_read:
        算法说明:
          1. 读取文件末尾固定字节窗口（64KB），反序列化量从全文件降到 KB 级。
          2. 在窗口内按 "\n- " 切分序列项起点（continuation 行是缩进，不含 "- "），
             取最后 n 个起点作为 record 块边界。
          3. 若窗口内 - 起点少于 n 个（边界不够），扩大窗口重试一次。

        YAML 序列项结构:
          records:
          - record_id: r001
            field: value
          - record_id: r002
            field: value

        切分时 "\n- " 只匹配真正的 record 起始位置（continuation 行无 "- "），
        不会切到 record 内部的字段值。

        Args:
            yaml_file: 单个分片 YAML 文件路径
            n: 期望提取的 record 块数（可能被文件实际数量截断）

        Returns:
            record 文本块列表（按文件中出现的顺序，每个块以 "- " 开头）
        """
        if n <= 0:
            return []
        try:
            file_size = yaml_file.stat().st_size
        except OSError:
            return []
        if file_size == 0:
            return []

        pattern = "\n- "
        # 两次尝试的窗口都必须用 min(MAX, file_size) 限制，
        # 避免小文件（<窗口大小）下第二次 seek 越过文件起始位置触发 OSError。
        first_window = min(self._TAIL_READ_BYTES, file_size)
        second_window = min(self._TAIL_READ_BYTES_MAX, file_size)
        blocks: list[str] = []

        for attempt_size in (first_window, second_window):
            try:
                with open(yaml_file, "rb") as f:
                    f.seek(file_size - attempt_size)
                    tail_bytes = f.read()
            except OSError:
                return blocks
            tail_text = tail_bytes.decode("utf-8", errors="replace")

            indices = [i for i in range(len(tail_text)) if tail_text.startswith(pattern, i)]
            # 满足任一条件则返回已找到的块：
            # 1) 已找到足够多的 record 起点；
            # 2) 本次窗口已覆盖整个文件（读不到更多）；
            # 3) 已用最大窗口重试。
            enough = len(indices) >= n
            full_file_covered = attempt_size >= file_size
            max_window_reached = attempt_size >= self._TAIL_READ_BYTES_MAX
            if enough or full_file_covered or max_window_reached:
                take = min(n, len(indices))
                start_indices = indices[-take:] if take > 0 else []
                for i, start in enumerate(start_indices):
                    block_start = start + 1  # 跳过 \n，保留 "- "
                    block_end = start_indices[i + 1] if i + 1 < len(start_indices) else len(tail_text)
                    block = tail_text[block_start:block_end].rstrip()
                    if block:
                        blocks.append(block)
                return blocks

        return blocks

    def read_records_from_tail(  # noqa: PLR0911,PLR0912
        self,
        pipeline_run_id: str,
        limit: int,
        before_sequence: int | None = None,
        after_sequence: int | None = None,
    ) -> tuple[list[ExecutionRecordData], bool]:
        """从 YAML 分片文件尾部反向读取 records（不加载整个文件）。

        FEATURE-tail_read:
        性能优化: 主管道单文件 1.3MB / 500 条 record 时，全量 yaml.safe_load
        需 4-5s；本方法只读末尾 64KB 窗口，单次解析 ~20 条 record，
        加载时间降到 0.3-0.8s（5-10x 提升）。

        算法:
          1. 倒序遍历所有分片文件（最新的分片 part 编号最大），
             从每个分片尾部提取 N 个 record 文本块。
          2. 跨分片累积直到凑够 limit 条（或所有分片读完）。
          3. 拼装为最小 YAML 文档（records:\\n + 文本块）后 safe_load。
          4. 按游标（before/after sequence）过滤并截断 limit。

        Args:
            pipeline_run_id: 管道运行 ID
            limit: 最多返回的 records 数（断线补漏场景下不截断）
            before_sequence: 只返回 sequence < before_sequence 的 records
            after_sequence: 只返回 sequence > after_sequence 的 records（断线补漏）

        Returns:
            (records, has_more) - records 按 sequence 升序；has_more 表示
            在 before_sequence 边界内是否还有更多未读取的更早 records。

        边界处理:
          - 无 _data_dir / 无分片文件: 返回 ([], False)
          - 解析失败: 返回 ([], False)，由调用方 fallback 到全量加载
          - 末尾分片为空: 自动读上一个分片
        """
        if not self._data_dir:
            return [], False

        part_files = sorted(self._get_part_files(pipeline_run_id), reverse=True)
        if not part_files:
            return [], False

        collected_blocks: list[str] = []
        has_more = False
        # 单分片读取上限：单分片最多 _MAX_RECORDS_PER_FILE 条 record，
        # 断线补漏场景下需要把所有新 record 都捞回来，不能用 limit 截断
        per_part_cap = _MAX_RECORDS_PER_FILE

        for part_file in part_files:
            if after_sequence is not None:
                # 断线补漏：每个分片都参与收集，不受 limit 截断
                blocks = self._extract_tail_blocks(part_file, per_part_cap)
                collected_blocks.extend(blocks)
                continue
            # 初始加载 / 向上翻页：从尾部读，按需扩大
            needed = limit - len(collected_blocks)
            if needed <= 0:
                has_more = True
                break
            blocks = self._extract_tail_blocks(part_file, needed)
            collected_blocks.extend(blocks)
            # 当前分片已提供足够块，说明更早分片可能还有更早 record
            if len(blocks) >= needed:
                has_more = True
                break
            # 提取的块数不足，检查是否因读取窗口限制导致
            # 如果文件大于最大读取窗口，窗口外可能还有更多 record
            try:
                file_size = part_file.stat().st_size
            except OSError:
                file_size = 0
            if file_size > self._TAIL_READ_BYTES_MAX:
                has_more = True
                break

        if not collected_blocks:
            return [], False

        yaml_text = "records:\n" + "\n".join(collected_blocks)
        try:
            data = yaml.safe_load(yaml_text)
        except Exception as exc:
            logger.warning(
                "反向读取 YAML 解析失败: %s - %s", pipeline_run_id, exc
            )
            return [], False

        raw_records = data.get("records") if isinstance(data, dict) else []
        if not isinstance(raw_records, list) or not raw_records:
            return [], False

        records = [
            self._dict_to_record(rd)
            for rd in raw_records
            if isinstance(rd, dict)
        ]
        records.sort(key=lambda r: (r.sequence, r.created_at or ""))

        # 断线补漏：只过滤，不截断
        if after_sequence is not None:
            return [r for r in records if r.sequence > after_sequence], False

        if before_sequence is not None:
            records = [r for r in records if r.sequence < before_sequence]

        # 截断到 limit 条（保留最新的），has_more 保留收集循环的判断
        if limit is not None and len(records) > limit:
            records = records[-limit:]

        return records, has_more

    @staticmethod
    def _record_to_message(record: ExecutionRecordData) -> dict[str, Any]:
        """将 ExecutionRecordData 转换为 message dict 格式。"""
        # 优先基于 record.type 映射 role
        _type_to_role = {"user": "user", "ai": "assistant", "tool": "tool", "system": "system"}
        role = record.role or _type_to_role.get(record.type, "user")
        msg: dict[str, Any] = {
            "role": role,
            "content": record.content or "",
        }

        # 恢复 tool_calls
        if record.type == "ai" and record.tool_calls_json:
            try:
                tool_calls = json.loads(record.tool_calls_json)
                if tool_calls:
                    msg["tool_calls"] = tool_calls
            except (json.JSONDecodeError, TypeError):
                pass

        # 恢复 tool_call_id
        if record.type == "tool" and record.tool_call_id:
            msg["tool_call_id"] = record.tool_call_id

        return msg

    def save_summary(self, summary: PipelineRunSummary) -> str:
        if not summary.run_id:
            summary.run_id = uuid.uuid4().hex[:12]
        if not summary.created_at:
            summary.created_at = datetime.now().isoformat()
        self._summaries[summary.run_id] = summary
        self._loaded_pipelines.add(summary.run_id)
        self._update_summary_in_file(summary.run_id)
        self._all_summaries_loaded = False
        logger.debug("保存管道摘要: %s (iterations=%d, status=%s)",
                      summary.run_id, summary.total_iterations, summary.status)
        return summary.run_id

    def register_pipeline(
        self, pipeline_run_id: str, root_task_id: str,
    ) -> None:
        old_root = self._pipeline_root_map.get(pipeline_run_id)
        if old_root == root_task_id:
            return
        self._pipeline_root_map[pipeline_run_id] = root_task_id
        self._persist_root_map()
        # 如果扁平位置有文件，迁移到子目录
        if self._data_dir:
            flat_path = self._data_dir / f"{pipeline_run_id}.yaml"
            if flat_path.exists():
                target_dir = self._data_dir / root_task_id
                target_dir.mkdir(parents=True, exist_ok=True)
                target_path = target_dir / f"{pipeline_run_id}.yaml"
                flat_path.rename(target_path)
                logger.info("迁移管道文件: %s -> %s", flat_path.name, root_task_id)
            # 也迁移分片文件
            for flat_part in self._data_dir.glob(f"{pipeline_run_id}_*.yaml"):
                target_dir = self._data_dir / root_task_id
                target_dir.mkdir(parents=True, exist_ok=True)
                target_path = target_dir / flat_part.name
                flat_part.rename(target_path)
            self._active_part.pop(pipeline_run_id, None)

    def _persist_root_map(self) -> None:
        if not self._data_dir or not self._map_file:
            return
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._map_file.write_text(
            json.dumps(self._pipeline_root_map, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def get_summary(self, run_id: str) -> PipelineRunSummary | None:
        self._ensure_loaded(run_id)
        return self._summaries.get(run_id)

    def update_summary(self, pipeline_run_id: str, updates: dict[str, Any]) -> None:
        """更新指定管道运行的 summary 字段并持久化到磁盘。

        将 thread_id 等关联信息写入 summary。

        Args:
            pipeline_run_id: 管道运行 ID
            updates: 需要更新的字段字典，例如 {"thread_id": "abc123"}
        """
        self._ensure_loaded(pipeline_run_id)
        summary = self._summaries.get(pipeline_run_id)
        if summary is None:
            # summary 尚不存在，创建一个空 summary 再更新
            summary = PipelineRunSummary(run_id=pipeline_run_id)
            self._summaries[pipeline_run_id] = summary
        for key, value in updates.items():
            if hasattr(summary, key):
                setattr(summary, key, value)
        self._update_summary_in_file(pipeline_run_id)
        self._all_summaries_loaded = False
        logger.debug(
            "更新管道摘要字段: %s (updates=%s)",
            pipeline_run_id,
            list(updates.keys()),
        )

    def list_all_summaries(self) -> list[PipelineRunSummary]:
        """返回所有已加载的管道运行摘要列表（不做数量限制）。

        根据 summary.thread_id 反查属于某个 thread 的所有 pipeline_run_id。

        性能优化: 使用 _load_all_summaries_only 仅加载 summary 部分，
        并通过 _all_summaries_loaded 标记避免重复解析。

        Returns:
            全部 PipelineRunSummary 列表
        """
        if self._data_dir and not self._all_summaries_loaded:
            self._load_all_summaries_only()
            self._all_summaries_loaded = True
        return list(self._summaries.values())

    def list_summaries(
        self, limit: int = 50
    ) -> list[PipelineRunSummary]:
        # summaries 需要全量扫描，触发一次性加载
        if self._data_dir and not self._loaded_pipelines:
            self._load_all()
        summaries = sorted(
            self._summaries.values(),
            key=lambda s: s.created_at,
            reverse=True,
        )
        return summaries[:limit]

    def get_total_tokens(self) -> dict[str, int]:
        if self._data_dir and not self._loaded_pipelines:
            self._load_all()
        total: dict[str, int] = {}
        for summary in self._summaries.values():
            for key, value in summary.total_tokens.items():
                total[key] = total.get(key, 0) + value
        return total


def summarize_text(text: Any, max_len: int = 500) -> str:
    """截断长文本用于摘要显示。"""
    if text is None:
        return ""
    s = str(text)
    if len(s) <= max_len:
        return s
    return s[:max_len] + "...(truncated)"
