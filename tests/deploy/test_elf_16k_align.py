# @feature: FP-0.2.PACK jniLibs 16KB ELF 对齐（HyperOS 兼容性弹窗修复配套） | @ci: python-test
"""elf_16k_align.py 单测（合成 ELF 多形态 + jniLibs 真库集成）。

行为契约（断输入→输出，不钉实现字节）：
- 修补语义（offset-remap）：congruence 失败的 PT_LOAD 文件内容搬到 16KB 同余
  新文件位、p_offset 更新、p_align 提至 16384；p_vaddr/e_entry/p_paddr 及其余
  vaddr 引用零改动；段内容逐字节等值搬运；已合规库零改动（幂等）。
- 引用同步：PT_DYNAMIC/PT_PHDR/PT_GNU_RELRO 的 offset 引用落入被搬旧区间时
  随宿主段平移。
- fail-closed：非 ELF64 小端拒识；修补结果 verify 不过/不幂等则不产出。
- verify 面：对齐、congruence、段间内存/文件重叠、地址序、PT 引用落段、
  e_entry 落段逐项可检出。
- 门禁语义：scan 有违约退出 1、全合规退出 0；--page 非 2 的幂拒绝。
- 真库（jniLibs/arm64-v8a）：六库全合规；libproot_loader 为 ET_EXEC 且
  vaddr/e_entry/PT_PHDR 语义面与修补设计一致。
"""

from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_helper_path = _REPO_ROOT / "deploy" / "elf_16k_align.py"
_spec = importlib.util.spec_from_file_location("elf_16k_align", _helper_path)
assert _spec is not None
assert _spec.loader is not None
ela = importlib.util.module_from_spec(_spec)
# 先注册 sys.modules 再执行：dataclass/py3.14 处理时按 __module__ 反查模块对象。
sys.modules.setdefault("elf_16k_align", ela)
_spec.loader.exec_module(ela)

JNI_LIBS = _REPO_ROOT / "app-mobile" / "android" / "app" / "src" / "main" / "jniLibs" / "arm64-v8a"
PAGE = 16384

pytestmark = pytest.mark.unit


def make_elf(
    segments: list[dict[str, object]],
    *,
    e_type: int = ela.ET_DYN,
    e_entry: int = 0,
) -> bytes:
    """合成最小 ELF64 小端：segments 按给定 offset 摆放 content，无节表。"""
    phnum = len(segments)
    phoff = 64
    phentsize = 56
    body = bytearray(phoff + phnum * phentsize)
    for seg in segments:
        off = int(seg["offset"])  # type: ignore[arg-type]
        content: bytes = seg["content"]  # type: ignore[assignment]
        if len(body) < off + len(content):
            body.extend(b"\x00" * (off + len(content) - len(body)))
        body[off:off + len(content)] = content
    struct.pack_into(
        "<16sHHIQQQIHHHHHH", body, 0,
        b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8,
        e_type, ela.EM_AARCH64, 1, e_entry, phoff, 0, 0, 64, phentsize, phnum, 0, 0, 0,
    )
    for i, seg in enumerate(segments):
        struct.pack_into(
            "<IIQQQQQQ", body, phoff + i * phentsize,
            int(seg["type"]), int(seg["flags"]), int(seg["offset"]), int(seg["vaddr"]),
            int(seg["vaddr"]), int(seg["filesz"]), int(seg["memsz"]), int(seg["align"]),
        )
    return bytes(body)


def _loader_shape(entry: int = 0x5400000000) -> bytes:
    """复刻 libproot_loader 修补前形态：ET_EXEC 双 LOAD，LOAD#2 congruence 差 0x1000。"""
    code = bytes(range(0x100)) * 4  # 0x400 伪代码段（可区分字节）
    return make_elf(
        [
            {"type": ela.PT_PHDR, "flags": 4, "offset": 0x40, "vaddr": 0x200040,
             "filesz": 0xE0, "memsz": 0xE0, "align": 8, "content": b"\x00" * 0xE0},
            {"type": ela.PT_LOAD, "flags": 4, "offset": 0x0, "vaddr": 0x200000,
             "filesz": 0x120, "memsz": 0x120, "align": 0x1000,
             "content": b"\x00" * 0x120},
            {"type": ela.PT_LOAD, "flags": 5, "offset": 0x1000, "vaddr": 0x5400000000,
             "filesz": 0x400, "memsz": 0x400, "align": 0x1000, "content": code},
            {"type": 0x6474E551, "flags": 6, "offset": 0x0, "vaddr": 0x0,
             "filesz": 0x0, "memsz": 0x0, "align": 0x0, "content": b""},
        ],
        e_type=ela.ET_EXEC,
        e_entry=entry,
    )


class TestParseFailClosed:
    """非 ELF64 小端形态逐类拒识（fail-closed 面）。"""

    def test_garbage_rejected(self) -> None:
        with pytest.raises(ela.ElfFormatError):
            ela.parse_elf64(b"not an elf file at all")

    @pytest.mark.parametrize(("byte_idx", "cls"), [(4, 1), (4, 2), (5, 2)])
    def test_class_and_endian_rejected(self, byte_idx: int, cls: int) -> None:
        data = bytearray(make_elf([]))
        data[byte_idx] = cls  # class=1(ELF32)/坏类；data=2(大端)
        with pytest.raises(ela.ElfFormatError):
            ela.parse_elf64(bytes(data))

    def test_no_phdr_rejected(self) -> None:
        data = bytearray(make_elf([]))
        struct.pack_into("<HH", data, 54, 56, 0)  # e_phentsize=56, e_phnum=0
        with pytest.raises(ela.ElfFormatError):
            ela.parse_elf64(bytes(data))

    def test_truncated_phdr_table_rejected(self) -> None:
        # phdr 表声称存在但文件被截断：干净拒识（不抛裸 struct.error）。
        data = make_elf(
            [{"type": ela.PT_LOAD, "flags": 5, "offset": 0x0, "vaddr": 0x0,
              "filesz": 0x10, "memsz": 0x10, "align": PAGE, "content": b"\x00" * 0x10}]
        )
        with pytest.raises(ela.ElfFormatError, match="越界"):
            ela.parse_elf64(data[:80])


class TestPatchOffsetRemap:
    """offset-remap 修补语义：vaddr/e_entry 冻结、内容搬运、幂等。"""

    def test_et_exec_loader_shape(self) -> None:
        data = _loader_shape()
        result = ela.patch_bytes(data, PAGE)
        assert result.changed
        info = ela.parse_elf64(result.data)
        # vaddr/e_entry/段尺寸零改动（ET_EXEC 冻结面）。
        assert info.e_entry == 0x5400000000
        loads = info.loads()
        assert [(p.vaddr, p.filesz, p.memsz, p.flags) for p in loads] == [
            (0x200000, 0x120, 0x120, 4), (0x5400000000, 0x400, 0x400, 5)
        ]
        # align 提至 16K、congruence 成立。
        assert all(p.align == PAGE for p in loads)
        assert all(ela.congruence_delta(p, PAGE) == 0 for p in loads)
        # LOAD#2 搬到 16KB 对齐新位且内容逐字节等值。
        assert loads[1].offset % PAGE == 0
        assert loads[1].offset >= 0x1000 + 0x400
        assert result.data[loads[1].offset:loads[1].offset + 0x400] == data[0x1000:0x1400]
        # 动作记录可审计：align + move 各一。
        assert [(a.index, a.action) for a in result.actions] == [(1, "align"), (2, "move")]
        # 修补结果过 verify 且幂等。
        assert ela.verify_bytes(result.data, PAGE) == []
        again = ela.patch_bytes(result.data, PAGE)
        assert not again.changed
        assert again.data == result.data

    def test_et_dyn_single_load(self) -> None:
        # 普通 .so 形态：单 LOAD off=0x1000 vaddr=0（16K 同余差 0x1000）。
        code = b"\xab" * 0x300
        data = make_elf(
            [{"type": ela.PT_LOAD, "flags": 5, "offset": 0x1000, "vaddr": 0x0,
              "filesz": 0x300, "memsz": 0x300, "align": 0x1000, "content": code}]
        )
        result = ela.patch_bytes(data, PAGE)
        info = ela.parse_elf64(result.data)
        (load,) = info.loads()
        assert load.offset % PAGE == 0
        assert ela.congruence_delta(load, PAGE) == 0
        assert load.vaddr == 0
        assert info.e_entry == 0
        assert result.data[load.offset:load.offset + 0x300] == code

    def test_multi_move_keeps_each_congruence_slot(self) -> None:
        # 双段错位且 vaddr 同余位不同：各搬至 ≥ 现有内容的各自同余位，不重叠。
        data = make_elf(
            [
                {"type": ela.PT_LOAD, "flags": 5, "offset": 0x1000, "vaddr": 0x0,
                 "filesz": 0x100, "memsz": 0x100, "align": 0x1000, "content": b"\x01" * 0x100},
                {"type": ela.PT_LOAD, "flags": 5, "offset": 0x2000, "vaddr": 0x5400001000,
                 "filesz": 0x100, "memsz": 0x100, "align": 0x1000,
                 "content": b"\x02" * 0x100},
            ],
            e_type=ela.ET_EXEC,
            e_entry=0x5400001000,
        )
        result = ela.patch_bytes(data, PAGE)
        first, second = ela.parse_elf64(result.data).loads()
        assert first.offset % PAGE == 0
        assert second.offset % PAGE == 0x1000  # 与自身 vaddr 同余位一致
        assert second.offset >= first.offset + first.filesz
        assert ela.verify_bytes(result.data, PAGE) == []
    def test_dynamic_offset_ref_shifts_with_host(self) -> None:
        # PT_DYNAMIC 落在被搬 LOAD 内：offset 引用随宿主平移，vaddr 不动。
        dyn_content = b"\xcd" * 0x40
        data = make_elf(
            [
                {"type": ela.PT_LOAD, "flags": 6, "offset": 0x1000, "vaddr": 0x0,
                 "filesz": 0x200, "memsz": 0x200, "align": 0x1000,
                 "content": b"\x00" * 0x100 + dyn_content},
                {"type": ela.PT_DYNAMIC, "flags": 6, "offset": 0x1100, "vaddr": 0x100,
                 "filesz": 0x40, "memsz": 0x40, "align": 8, "content": dyn_content},
            ]
        )
        result = ela.patch_bytes(data, PAGE)
        info = ela.parse_elf64(result.data)
        dynamic = next(p for p in info.phdrs if p.type == ela.PT_DYNAMIC)
        load = info.loads()[0]
        assert dynamic.vaddr == 0x100  # vaddr 引用零改动
        assert dynamic.offset == load.offset + 0x100  # offset 引用随宿主平移
        assert any(a.index == 1 and a.action == "ref-shift" for a in result.actions)
        assert ela.verify_bytes(result.data, PAGE) == []

    def test_compliant_is_noop(self) -> None:
        data = make_elf(
            [{"type": ela.PT_LOAD, "flags": 5, "offset": 0x0, "vaddr": 0x0,
              "filesz": 0x100, "memsz": 0x100, "align": PAGE, "content": b"\x0f" * 0x100}]
        )
        result = ela.patch_bytes(data, PAGE)
        assert not result.changed
        assert result.data == data
        assert result.actions == ()


class TestPatchFailClosed:
    """修补结果违约必须抛错不产出。"""

    def test_verify_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        data = _loader_shape()
        # 注入 verify 恒违约 → patch_bytes 拒绝产出。
        monkeypatch.setattr(ela, "verify_bytes", lambda _d, _p: ["injected"])
        with pytest.raises(ValueError, match="完整性校验失败"):
            ela.patch_bytes(data, PAGE)


class TestVerifyDetects:
    """verify 逐项检出能力（每类违约至少一例）。"""

    def test_congruence_violation(self) -> None:
        issues = ela.verify_bytes(_loader_shape(), PAGE)
        assert any("congruence" in i for i in issues)

    def test_align_too_small(self) -> None:
        data = make_elf(
            [{"type": ela.PT_LOAD, "flags": 5, "offset": 0x0, "vaddr": 0x0,
              "filesz": 0x10, "memsz": 0x10, "align": PAGE, "content": b"\x00" * 0x10}]
        )
        info = ela.parse_elf64(data)
        loads = list(info.loads())
        loads[0] = ela.Phdr(loads[0].index, loads[0].type, loads[0].flags, loads[0].offset,
                            loads[0].vaddr, loads[0].paddr, loads[0].filesz, loads[0].memsz, 0x1000)
        rebuilt = bytearray(data)
        ela.PHDR.pack_into(rebuilt, info.e_phoff, loads[0].type, loads[0].flags, loads[0].offset,
                           loads[0].vaddr, loads[0].paddr, loads[0].filesz, loads[0].memsz, 0x1000)
        issues = ela.verify_bytes(bytes(rebuilt), PAGE)
        assert any("p_align" in i for i in issues)

    def test_overlap_and_order_and_entry(self) -> None:
        # 内存重叠 + e_entry 出段：两项违约同时命中。
        data = make_elf(
            [
                {"type": ela.PT_LOAD, "flags": 5, "offset": 0x0, "vaddr": 0x10000,
                 "filesz": 0x100, "memsz": 0x2000, "align": PAGE, "content": b"\x00" * 0x100},
                {"type": ela.PT_LOAD, "flags": 5, "offset": 0x4000, "vaddr": 0x11000,
                 "filesz": 0x100, "memsz": 0x100, "align": PAGE, "content": b"\x00" * 0x100},
            ],
            e_entry=0x99999,
        )
        issues = ela.verify_bytes(data, PAGE)
        assert any("内存重叠" in i for i in issues)
        assert any("e_entry" in i for i in issues)

    def test_phdr_reference_outside_load(self) -> None:
        data = make_elf(
            [
                {"type": ela.PT_LOAD, "flags": 5, "offset": 0x0, "vaddr": 0x0,
                 "filesz": 0x100, "memsz": 0x100, "align": PAGE, "content": b"\x00" * 0x100},
                {"type": ela.PT_PHDR, "flags": 4, "offset": 0x5000, "vaddr": 0x80000,
                 "filesz": 0x40, "memsz": 0x40, "align": 8, "content": b"\x00" * 0x40},
            ]
        )
        issues = ela.verify_bytes(data, PAGE)
        assert any("PT_PHDR" in i for i in issues)

    def test_file_overlap_detected(self) -> None:
        # 文件面重叠（offset 交错）独立可检出。
        data = make_elf(
            [
                {"type": ela.PT_LOAD, "flags": 5, "offset": 0x4000, "vaddr": 0x40000,
                 "filesz": 0x1000, "memsz": 0x1000, "align": PAGE, "content": b"\x00" * 0x1000},
                {"type": ela.PT_LOAD, "flags": 5, "offset": 0x4000 + 0x800, "vaddr": 0x80000,
                 "filesz": 0x100, "memsz": 0x100, "align": PAGE, "content": b"\x00" * 0x100},
            ],
            e_entry=0x40000,
        )
        issues = ela.verify_bytes(data, PAGE)
        assert any("文件重叠" in i for i in issues)


class TestRealJniLibs:
    """真库集成：jniLibs/arm64-v8a 六库全合规；loader 语义面锁定。"""

    def test_all_libs_compliant(self) -> None:
        assert JNI_LIBS.is_dir(), "jniLibs 资产缺失（先跑 build_mobile apk 步）"
        so_files = sorted(JNI_LIBS.glob("*.so"))
        assert len(so_files) == 6
        for path in so_files:
            data = path.read_bytes()
            assert ela.verify_bytes(data, PAGE) == [], path.name

    def test_loader_semantics_locked(self) -> None:
        data = (JNI_LIBS / "libproot_loader.so").read_bytes()
        info = ela.parse_elf64(data)
        assert info.e_type == ela.ET_EXEC
        assert info.e_entry == 0x5400000000  # entry == LOAD#2 vaddr（ET_EXEC 冻结面）
        first, second = info.loads()
        assert (first.vaddr, first.offset, first.align) == (0x200000, 0x0, PAGE)
        assert second.vaddr == 0x5400000000
        assert second.offset % PAGE == 0
        assert ela.congruence_delta(second, PAGE) == 0
        # 幂等性质：对现盘修补件再跑 patch 零改动。
        assert not ela.patch_bytes(data, PAGE).changed


class TestCli:
    """CLI 门禁语义：scan 违约退出 1；--page 非 2 的幂拒绝；dry-run 不落盘。"""

    def test_scan_exit_codes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (tmp_path / "bad.so").write_bytes(_loader_shape())
        assert ela.main(["scan", str(tmp_path)]) == 1
        assert "CONGRUENCE" in capsys.readouterr().out
        (tmp_path / "good.so").write_bytes(
            make_elf([{"type": ela.PT_LOAD, "flags": 5, "offset": 0x0, "vaddr": 0x0,
                       "filesz": 0x10, "memsz": 0x10, "align": PAGE, "content": b"\x00" * 0x10}])
        )
        bad = tmp_path / "bad.so"
        bad.unlink()
        assert ela.main(["scan", str(tmp_path)]) == 0

    def test_patch_dry_run_and_verify_cli(self, tmp_path: Path) -> None:
        target = tmp_path / "lib.so"
        target.write_bytes(_loader_shape())
        before = target.read_bytes()
        assert ela.main(["patch", str(target), "--dry-run"]) == 0
        assert target.read_bytes() == before  # dry-run 不落盘
        assert ela.main(["patch", str(target)]) == 0
        assert target.read_bytes() != before
        assert ela.main(["verify", str(tmp_path)]) == 0

    def test_page_must_be_power_of_two(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            ela.main(["scan", str(tmp_path), "--page", "1000"])
