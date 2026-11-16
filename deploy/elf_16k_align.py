#!/usr/bin/env python3
# @feature: FP-0.2.PACK jniLibs 16KB ELF 对齐（HyperOS 兼容性弹窗修复配套工具） | @ci: python-test
"""Android 16KB 页设备下的 ELF PT_LOAD 扫描 / 修补 / 完整性校验工具。

背景：Android 15+ 16KB 页设备要求所有 PT_LOAD 满足
    p_align >= 16384 且 p_offset ≡ p_vaddr (mod p_align)。
不满足时：HyperOS 每次启动弹「Android 应用兼容性」对话框；且内核按页映射
（binfmt_elf / 动态 linker 同理：mmap(vaddr 下取页, 文件 offset 下取页)）时
内容错位——entry 落到错位字节。

修补算法（offset-remap，本工具唯一语义）：对 congruence 失败的 PT_LOAD，
把其文件内容搬到 ≥ 现有全部文件内容的 16KB 同余位（new_offset ≡ p_vaddr
(mod 16384)），仅更新 p_offset；p_vaddr / e_entry / 其余 vaddr 引用零改动。

为何不用「p_vaddr += delta」的 vaddr 平移算法（其他开源 16KB patcher 的
常见做法）：本仓 libproot_loader.so 是 ET_EXEC（e_type=2，proot 外挂
loader，经 PROOT_LOADER env 由内核 binfmt_elf 直接 execve 装载，见
KernelHostPlugin/SeedInstaller 的 proot 命令拼装），vaddr 平移需同步
e_entry，且静态无动态段的 ET_EXEC 内存在链接期冻结的绝对地址引用，平移
有断裂风险；offset-remap 对所有装载方语义不变，4KB 页设备向后兼容
（congruence mod 4096 仍成立）。

子命令：
    scan    逐 .so 报告各 PT_LOAD 对齐与 congruence（有 BAD 退出 1，可作门禁）
    patch   原位修补单库（幂等：已合规则字节零改动；修补前自动校验，不产坏文件）
    verify  完整性校验：对齐/congruence、段间重叠与序、PT_PHDR/PT_DYNAMIC/
            PT_GNU_RELRO 引用完整、e_entry 落段

仅支持 ELF64 小端（jniLibs 全 aarch64），其余形态 fail-closed。
"""

from __future__ import annotations

import argparse
import hashlib
import struct
import sys
from dataclasses import dataclass, replace
from pathlib import Path

PAGE_16K = 16384
EM_AARCH64 = 183
ET_EXEC = 2
ET_DYN = 3

PT_LOAD = 1
PT_DYNAMIC = 2
PT_PHDR = 6
PT_GNU_RELRO = 0x6474E552

EHDR = struct.Struct("<16sHHIQQQIHHHHHH")  # e_ident..e_shstrndx
PHDR = struct.Struct("<IIQQQQQQ")  # p_type, p_flags, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_align

PT_NAMES = {
    PT_LOAD: "PT_LOAD",
    PT_DYNAMIC: "PT_DYNAMIC",
    PT_PHDR: "PT_PHDR",
    PT_GNU_RELRO: "PT_GNU_RELRO",
    0x6474E550: "PT_GNU_EH_FRAME",
    0x6474E551: "PT_GNU_STACK",
    0x6474E553: "PT_GNU_PROPERTY",
}


class ElfFormatError(ValueError):
    """ELF 形态不受支持（非 ELF64 小端 / phentsize 不符）。"""


@dataclass(frozen=True)
class Phdr:
    index: int
    type: int
    flags: int
    offset: int
    vaddr: int
    paddr: int
    filesz: int
    memsz: int
    align: int

    @property
    def type_name(self) -> str:
        return PT_NAMES.get(self.type, hex(self.type))

    def mem_range(self) -> tuple[int, int]:
        return (self.vaddr, self.vaddr + self.memsz)

    def file_range(self) -> tuple[int, int]:
        return (self.offset, self.offset + self.filesz)


@dataclass(frozen=True)
class ElfInfo:
    e_type: int
    e_machine: int
    e_entry: int
    e_phoff: int
    e_phentsize: int
    e_phnum: int
    e_shoff: int
    e_shentsize: int
    e_shnum: int
    phdrs: tuple[Phdr, ...]
    size: int

    @property
    def type_name(self) -> str:
        return {ET_EXEC: "ET_EXEC", ET_DYN: "ET_DYN"}.get(self.e_type, hex(self.e_type))

    def loads(self) -> tuple[Phdr, ...]:
        return tuple(p for p in self.phdrs if p.type == PT_LOAD)


def parse_elf64(data: bytes) -> ElfInfo:
    """解析 ELF64 小端头与程序头表；形态不符抛 ElfFormatError（fail-closed）。"""
    if len(data) < EHDR.size:
        raise ElfFormatError(f"文件过小（{len(data)}B）不是 ELF")
    (
        e_ident,
        e_type,
        e_machine,
        _e_version,
        e_entry,
        e_phoff,
        e_shoff,
        _e_flags,
        _e_ehsize,
        e_phentsize,
        e_phnum,
        e_shentsize,
        e_shnum,
        _e_shstrndx,
    ) = EHDR.unpack_from(data, 0)
    if e_ident[:4] != b"\x7fELF":
        raise ElfFormatError("ELF magic 不符")
    if e_ident[4] != 2:
        raise ElfFormatError(f"仅支持 ELF64（class={e_ident[4]}）")
    if e_ident[5] != 1:
        raise ElfFormatError(f"仅支持小端（data={e_ident[5]}）")
    if e_phnum == 0 or e_phentsize < PHDR.size:
        raise ElfFormatError(f"程序头表缺失或 phentsize={e_phentsize} < {PHDR.size}")
    phdrs: list[Phdr] = []
    for i in range(e_phnum):
        try:
            (p_type, p_flags, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_align) = PHDR.unpack_from(
                data, e_phoff + i * e_phentsize
            )
        except struct.error as exc:
            raise ElfFormatError(f"程序头表越界（phdr[{i}] 超出文件）: {exc}") from exc
        phdrs.append(Phdr(i, p_type, p_flags, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_align))
    return ElfInfo(
        e_type,
        e_machine,
        e_entry,
        e_phoff,
        e_phentsize,
        e_phnum,
        e_shoff,
        e_shentsize,
        e_shnum,
        tuple(phdrs),
        len(data),
    )


def congruence_delta(p: Phdr, page: int) -> int:
    """(p_offset - p_vaddr) mod page；0 = congruence 成立。"""
    return (p.offset - p.vaddr) % page


def _align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def _next_congruent(cursor: int, vaddr: int, page: int) -> int:
    """≥ cursor 且 ≡ vaddr (mod page) 的最小值（多段搬移时保持各自同余位）。"""
    r = vaddr % page
    candidate = -(-(cursor - r) // page) * page + r
    if candidate < cursor:
        candidate += page
    return candidate


def _load_verdicts(info: ElfInfo, page: int) -> list[tuple[Phdr, int]]:
    """每个 PT_LOAD 的 congruence 违约量（0 = 成立）。"""
    return [(p, congruence_delta(p, page)) for p in info.loads()]


def verify_bytes(data: bytes, page: int = PAGE_16K) -> list[str]:
    """静态完整性校验，返回违约清单（空 = 通过）。

    检查项（16KB 修补验收清单）：
    1. 每个 PT_LOAD：p_align >= page 且 p_offset ≡ p_vaddr (mod p_align)；
    2. 段间内存区间无重叠、文件区间无重叠、按 vaddr 地址序保持；
    3. PT_PHDR / PT_DYNAMIC / PT_GNU_RELRO 的 vaddr 落在某 PT_LOAD 内存区间、
       offset 落在对应 PT_LOAD 文件区间（vaddr 引用完整性——offset-remap 下
       vaddr 不动，此项按构造成立，仍显式复核）；
    4. e_entry（非 0）落在某 PT_LOAD 文件内容区间内。
    """
    issues: list[str] = []
    info = parse_elf64(data)
    loads = info.loads()

    for p in loads:
        if p.align < page:
            issues.append(f"phdr[{p.index}] {p.type_name}: p_align=0x{p.align:x} < 0x{page:x}")
        # congruence 主判按目标页：p_align 本身违约（< page）时 mod p_align 可能
        # 恰好成立，不得漏报真实装载违约；p_align > page 时再按 p_align 复核。
        delta_page = congruence_delta(p, page)
        delta_mod_align = congruence_delta(p, p.align) if p.align > 0 else -1
        if delta_page != 0:
            issues.append(
                f"phdr[{p.index}] {p.type_name}: congruence 失败 "
                f"(off=0x{p.offset:x} vaddr=0x{p.vaddr:x} mod 0x{page:x} 差 0x{delta_page:x})"
            )
        elif p.align > page and delta_mod_align != 0:
            issues.append(
                f"phdr[{p.index}] {p.type_name}: congruence 失败 "
                f"(off=0x{p.offset:x} vaddr=0x{p.vaddr:x} mod p_align=0x{p.align:x} 差 0x{delta_mod_align:x})"
            )

    by_addr = sorted(loads, key=lambda p: p.vaddr)
    if [p.index for p in by_addr] != sorted(p.index for p in loads):
        issues.append("PT_LOAD 地址序与 phdr 序不一致（序保持破坏）")
    for i in range(1, len(by_addr)):
        prev_end = by_addr[i - 1].vaddr + by_addr[i - 1].memsz
        if prev_end > by_addr[i].vaddr:
            issues.append(f"PT_LOAD 内存重叠: phdr[{by_addr[i - 1].index}] 与 phdr[{by_addr[i].index}]")
    ordered = sorted(loads, key=lambda p: p.offset)
    for i in range(1, len(ordered)):
        prev_end = ordered[i - 1].offset + ordered[i - 1].filesz
        if prev_end > ordered[i].offset:
            issues.append(f"PT_LOAD 文件重叠: phdr[{ordered[i - 1].index}] 与 phdr[{ordered[i].index}]")

    def find_load_containing(vaddr: int, size: int) -> Phdr | None:
        for p in loads:
            if p.vaddr <= vaddr and vaddr + size <= p.vaddr + p.memsz:
                return p
        return None

    for p in info.phdrs:
        if p.type not in (PT_PHDR, PT_DYNAMIC, PT_GNU_RELRO):
            continue
        host = find_load_containing(p.vaddr, p.memsz)
        if host is None:
            issues.append(f"phdr[{p.index}] {p.type_name}: vaddr=0x{p.vaddr:x} 不落在任何 PT_LOAD 内存区间")
        elif not (host.offset <= p.offset and p.offset + p.filesz <= host.offset + host.filesz):
            issues.append(
                f"phdr[{p.index}] {p.type_name}: offset=0x{p.offset:x} 不落在宿主段 phdr[{host.index}] 文件区间内"
            )

    if info.e_entry != 0 and not any(p.vaddr <= info.e_entry and info.e_entry < p.vaddr + p.filesz for p in loads):
        issues.append(f"e_entry=0x{info.e_entry:x} 不落在任何 PT_LOAD 文件内容区间")

    return issues


@dataclass(frozen=True)
class PatchAction:
    """单段修补动作记录（审计面：哪些段被搬移/改对齐）。"""

    index: int
    action: str  # "move" | "align" | "none"
    old_offset: int
    new_offset: int
    old_align: int
    new_align: int
    note: str = ""


@dataclass(frozen=True)
class PatchResult:
    data: bytes
    actions: tuple[PatchAction, ...]
    changed: bool


def patch_bytes(data: bytes, page: int = PAGE_16K) -> PatchResult:
    """offset-remap 修补（纯函数）：返回新字节与动作记录；已合规则原样返回。

    fail-closed：修补结果必须通过 verify_bytes 且对 patch_bytes 幂等，否则抛
    ValueError 不产出。
    """
    info = parse_elf64(data)
    phdrs = list(info.phdrs)
    actions: list[PatchAction] = []

    # 搬移目标游标：现有全部文件内容之后（含节表尾），永不过写任何现存字节。
    tail_end = info.size
    if info.e_shnum > 0 and info.e_shentsize > 0:
        tail_end = max(tail_end, info.e_shoff + info.e_shnum * info.e_shentsize)
    for p in phdrs:
        if p.type == PT_LOAD:
            tail_end = max(tail_end, p.offset + p.filesz)

    # 旧偏移 → 新偏移映射（供 PT_DYNAMIC/PT_PHDR/PT_GNU_RELRO 的 offset 引用同步）。
    offset_map: list[tuple[int, int, int]] = []  # (old_start, old_end, new_start)
    moves: list[tuple[int, int]] = []  # (phdr index, new_offset)

    for p in phdrs:
        if p.type != PT_LOAD:
            continue
        delta = congruence_delta(p, page)
        if delta != 0:
            new_offset = _next_congruent(tail_end, p.vaddr, page)
            tail_end = new_offset + p.filesz
            offset_map.append((p.offset, p.offset + p.filesz, new_offset))
            moves.append((p.index, new_offset))
            actions.append(
                PatchAction(
                    p.index,
                    "move",
                    p.offset,
                    new_offset,
                    p.align,
                    max(p.align, page),
                    note=f"congruence 差 0x{delta:x}，内容搬移",
                )
            )
        elif p.align < page:
            actions.append(PatchAction(p.index, "align", p.offset, p.offset, p.align, page, note="仅提对齐"))

    if not actions:
        return PatchResult(data, (), False)

    buf = bytearray(data)
    for idx, new_offset in moves:
        old = phdrs[idx]
        buf.extend(b"\x00" * (new_offset + old.filesz - len(buf)))
        buf[new_offset : new_offset + old.filesz] = data[old.offset : old.offset + old.filesz]
        phdrs[idx] = replace(old, offset=new_offset, align=max(old.align, page))
    # 仅提对齐的段。
    for i, p in enumerate(phdrs):
        if p.type == PT_LOAD and congruence_delta(p, page) == 0 and p.align < page:
            phdrs[i] = replace(p, align=page)

    # PT_DYNAMIC/PT_PHDR/PT_GNU_RELRO 的文件偏移引用若落入被搬旧区间则同步平移
    # 并记录（offset-remap 下 vaddr 引用零改动，仅 offset 面需要跟）。
    for i, p in enumerate(phdrs):
        if p.type not in (PT_DYNAMIC, PT_PHDR, PT_GNU_RELRO):
            continue
        for old_start, old_end, new_start in offset_map:
            if old_start <= p.offset < old_end:
                shifted = new_start + (p.offset - old_start)
                phdrs[i] = replace(p, offset=shifted)
                actions.append(
                    PatchAction(
                        p.index,
                        "ref-shift",
                        p.offset,
                        shifted,
                        p.align,
                        p.align,
                        note=f"{p.type_name} offset 引用随宿主段平移",
                    )
                )
                break

    for i, p in enumerate(phdrs):
        PHDR.pack_into(
            buf,
            info.e_phoff + i * info.e_phentsize,
            p.type,
            p.flags,
            p.offset,
            p.vaddr,
            p.paddr,
            p.filesz,
            p.memsz,
            p.align,
        )

    result = bytes(buf)
    issues = verify_bytes(result, page)
    if issues:
        raise ValueError("修补结果完整性校验失败（不产出）: " + "; ".join(issues))
    if bytes(patch_bytes(result, page).data) != result:
        raise ValueError("修补结果不幂等（不产出）")
    return PatchResult(result, tuple(actions), True)


def _iter_so(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for path in paths:
        if path.is_dir():
            out.extend(sorted(path.rglob("*.so")))
        else:
            out.append(path)
    return out


def cmd_scan(paths: list[Path], page: int) -> int:
    bad = 0
    for path in _iter_so(paths):
        data = path.read_bytes()
        try:
            info = parse_elf64(data)
        except ElfFormatError as exc:
            print(f"[BAD ] {path.name}: {exc}")
            bad += 1
            continue
        print(
            f"[{'OK  ' if all(d == 0 and p.align >= page for p, d in _load_verdicts(info, page)) else 'BAD '}"
            f"] {path.name}: e_type={info.type_name} size={info.size}"
        )
        for p, delta in _load_verdicts(info, page):
            state = "OK" if delta == 0 and p.align >= page else ("ALIGN" if delta == 0 else "CONGRUENCE")
            if state != "OK":
                bad += 1
            print(
                f"    phdr[{p.index}] off=0x{p.offset:x} vaddr=0x{p.vaddr:x} "
                f"filesz=0x{p.filesz:x} align=0x{p.align:x} congruence_delta=0x{delta:x} [{state}]"
            )
    if bad:
        print(f"scan: {bad} 项违约")
        return 1
    print("scan: 全部合规")
    return 0


def cmd_patch(path: Path, page: int, dry_run: bool) -> int:
    data = path.read_bytes()
    before_sha = hashlib.sha256(data).hexdigest()
    result = patch_bytes(data, page)
    after_sha = hashlib.sha256(result.data).hexdigest()
    print(f"patch: {path}")
    print(f"  sha256 before: {before_sha}")
    print(f"  sha256 after : {after_sha}")
    if not result.changed:
        print("  已合规，零改动（幂等）")
        return 0
    for action in result.actions:
        print(
            f"  phdr[{action.index}] {action.action}: off 0x{action.old_offset:x} -> 0x{action.new_offset:x}, "
            f"align 0x{action.old_align:x} -> 0x{action.new_align:x} {action.note}"
        )
    if dry_run:
        print("  --dry-run：不落盘")
        return 0
    path.write_bytes(result.data)
    print(f"  已写回（{len(data)} -> {len(result.data)} 字节）")
    return 0


def cmd_verify(paths: list[Path], page: int) -> int:
    failed = False
    for path in _iter_so(paths):
        data = path.read_bytes()
        try:
            issues = verify_bytes(data, page)
        except ElfFormatError as exc:
            print(f"[FAIL] {path}: {exc}")
            failed = True
            continue
        if issues:
            failed = True
            print(f"[FAIL] {path}")
            for issue in issues:
                print(f"    {issue}")
        else:
            print(f"[PASS] {path}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="jniLibs 16KB ELF 对齐工具（scan/patch/verify）")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_page(arg: argparse.ArgumentParser) -> None:
        arg.add_argument("--page", type=int, default=PAGE_16K, help="目标页大小（默认 16384）")

    p_scan = sub.add_parser("scan", help="逐 .so 扫描对齐与 congruence")
    p_scan.add_argument("paths", nargs="+", type=Path)
    add_page(p_scan)

    p_patch = sub.add_parser("patch", help="原位修补单库（幂等）")
    p_patch.add_argument("file", type=Path)
    p_patch.add_argument("--dry-run", action="store_true")
    add_page(p_patch)

    p_verify = sub.add_parser("verify", help="静态完整性校验")
    p_verify.add_argument("paths", nargs="+", type=Path)
    add_page(p_verify)

    args = parser.parse_args(argv)
    if args.page <= 0 or args.page & (args.page - 1) != 0:
        parser.error(f"--page 必须为 2 的幂，得到 {args.page}")
    if args.command == "scan":
        return cmd_scan(args.paths, args.page)
    if args.command == "patch":
        return cmd_patch(args.file, args.page, args.dry_run)
    return cmd_verify(args.paths, args.page)


if __name__ == "__main__":
    sys.exit(main())
