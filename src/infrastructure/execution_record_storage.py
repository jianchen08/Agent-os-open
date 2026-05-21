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

import logging
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

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

    error: str | None = None

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
    # BUG-FIX-fix_pipeline_thread_association:
    # 问题根因: 管道运行后生成的 YAML 文件没有存储 thread_id，
    #           导致 7/9 个管道文件是"无主"的，无法被任何 thread 加载。
    # 修复方案: 在 PipelineRunSummary 中新增 thread_id 字段，
    #           管道运行时将 thread_id 写入 summary 并持久化到 YAML。
    # 影响范围: list_messages、get_thread_detail 等消息查询接口的管道关联逻辑。
    # 修复日期: 2026-05-05
    thread_id: str = ""

    total_iterations: int = 0
    total_tokens: dict[str, int] = field(default_factory=dict)
    total_seconds: float = 0.0
    total_records: int = 0

    status: str = ""
    final_output: str = ""
    error: str | None = None

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
            # BUG-FIX-fix_yaml_records_parse_error:
            # 问题根因: 使用 "records: []" (flow 空序列) 后再追加 "- record_id: ..."
            #           会产生无效 YAML，导致 yaml.safe_load 抛出 ParserError，
            #           重启后整个文件被跳过，summary 和 records 全部丢失。
            # 修复方案: 使用 "records:" (block 序列头)，后续追加的 "- ..." 能正确解析。
            file_path.write_text(
                new_summary_text + "\nrecords:\n",
                encoding="utf-8",
            )
            return

        text = file_path.read_text(encoding="utf-8")
        marker = "\nrecords:"
        marker_idx = text.find(marker)
        if marker_idx == -1:
            logger.warning("YAML 文件格式异常，无法定位 records 段: %s", file_path.name)
            return

        new_text = new_summary_text + text[marker_idx:]
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
            # BUG-FIX-fix_yaml_records_parse_error:
            # 修复已有的损坏文件：将 "records: []" 替换为 "records:"，
            # 使后续追加的 "- record_id: ..." 序列项能被正确解析。
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
            # BUG-FIX-fix_yaml_records_parse_error: 同 _load_pipeline_file
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
        self, pipeline_run_id: str
    ) -> list[ExecutionRecordData]:
        self._ensure_loaded(pipeline_run_id)
        records = [
            r for r in self._records.values()
            if r.pipeline_run_id == pipeline_run_id
        ]
        records.sort(key=lambda r: (r.sequence, r.created_at or ""))
        return records

    def list_by_pipelines_batch(
        self, pipeline_ids: list[str],
    ) -> list[ExecutionRecordData]:
        """批量加载多个管道的执行记录，避免 N+1 查询问题。

        一次性确保所有指定管道的数据已加载到内存，
        然后从 self._records 中过滤并排序返回。

        Args:
            pipeline_ids: 管道运行 ID 列表

        Returns:
            所有管道的执行记录列表，按 pipeline_order + sequence 排序
        """
        if not pipeline_ids:
            return []
        # 确保所有指定管道的数据已加载
        pid_set = set(pipeline_ids)
        for pid in pipeline_ids:
            self._ensure_loaded(pid)
        # 从内存缓存中过滤
        pipeline_order = {pid: idx for idx, pid in enumerate(pipeline_ids)}
        records = [
            r for r in self._records.values()
            if r.pipeline_run_id in pid_set
        ]
        records.sort(key=lambda r: (
            pipeline_order.get(r.pipeline_run_id, 999),
            r.sequence,
            r.created_at or "",
        ))
        return records

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

    @staticmethod
    def _record_to_message(record: ExecutionRecordData) -> dict[str, Any]:
        """将 ExecutionRecordData 转换为 message dict 格式。"""
        role = record.role or "user"
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

        BUG-FIX-fix_pipeline_thread_association:
        用于在管道运行过程中/结束后将 thread_id 等关联信息写入 summary，
        确保管道 YAML 文件与 thread 之间的关联关系被正确持久化。

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

        BUG-FIX-fix_pipeline_thread_association:
        用于扫描所有管道文件，根据 summary.thread_id 反查
        属于某个 thread 的所有 pipeline_run_id。

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
