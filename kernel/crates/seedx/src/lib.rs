//! 种子解包核心（lib + bin 共用）：tar / tar.zst → 目标目录，带进度与关键
//! 条目断言。bin 薄壳只做参数解析与退出码翻译，逻辑全部在此可测。

use std::fs::File;
use std::io::BufReader;
use std::path::Path;

/// 解包结果统计。
#[derive(Debug)]
pub struct UnpackStats {
    pub entries: u64,
    pub bytes: u64,
}

/// 解包种子到目标目录。
///
/// `seed` 按 `--zst` 走 zstd 流式解码（构建期已压缩，APK 内不再二次压缩），
/// 否则按裸 tar。tar 链接（symlink/hardlink）与权限位由 tar crate 在
/// Unix（Android/Linux）端原生重建——设备端 rootfs 必需；Windows 宿主端
/// 仅测试走裸 tar 无链接的子集。
pub fn unpack(
    seed: &Path,
    dest: &Path,
    zst: bool,
    progress_every: u64,
) -> Result<UnpackStats, String> {
    let file = File::open(seed).map_err(|e| format!("种子打开失败 {}: {e}", seed.display()))?;
    let entries_decoded = |src: Box<dyn std::io::Read>| -> Result<UnpackStats, String> {
        let mut archive = tar::Archive::new(src);
        archive.set_preserve_permissions(true);
        let mut entries = 0u64;
        let mut bytes = 0u64;
        let iter = archive
            .entries()
            .map_err(|e| format!("tar 条目流打开失败: {e}"))?;
        for entry in iter {
            let mut entry = entry.map_err(|e| format!("tar 条目读取失败: {e}"))?;
            let path_buf = entry
                .path()
                .map_err(|e| format!("条目路径读取失败: {e}"))?
                .to_path_buf();
            let size = entry.header().size().unwrap_or(0);
            entry
                .unpack_in(dest)
                .map_err(|e| format!("条目解包失败 {}: {e}", path_buf.display()))?;
            entries += 1;
            bytes += size;
            if progress_every > 0 && entries % progress_every == 0 {
                eprintln!("[seedx] 进度 entries={entries} bytes={bytes}");
            }
        }
        if entries == 0 {
            return Err("种子零条目（空包或格式误判）".into());
        }
        Ok(UnpackStats { entries, bytes })
    };
    if zst {
        let decoder = zstd::stream::Decoder::new(BufReader::new(file))
            .map_err(|e| format!("zstd 解码器初始化失败: {e}"))?;
        entries_decoded(Box::new(decoder))
    } else {
        entries_decoded(Box::new(BufReader::new(file)))
    }
}

/// 首启装配完成标记检查：种子解包后由 mobile_init.sh 写入（设备侧 Linux 语义，
/// Windows 宿主端仅做存在性判定的纯检查）。
pub fn marker_path(dest: &Path) -> std::path::PathBuf {
    dest.join("opt/agentos/user_root/.mobile_init_done")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn temp_dir(tag: &str) -> std::path::PathBuf {
        let base = std::env::temp_dir().join(format!("seedx-test-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        base
    }

    fn write_plain_tar(path: &Path) {
        let f = File::create(path).unwrap();
        let mut tb = tar::Builder::new(f);
        let mut h = tar::Header::new_gnu();
        let data = b"hello seed";
        h.set_size(data.len() as u64);
        h.set_mode(0o644);
        h.set_cksum();
        tb.append_data(&mut h, "opt/agentos/config/kernel/storage.yaml", &data[..])
            .unwrap();
        let mut h2 = tar::Header::new_gnu();
        h2.set_size(4);
        h2.set_mode(0o755);
        h2.set_cksum();
        tb.append_data(&mut h2, "opt/agentos/agentos-kernel", &b"ELF!"[..])
            .unwrap();
        tb.finish().unwrap();
        tb.into_inner().unwrap().flush().unwrap();
    }

    #[test]
    fn unpack_plain_tar_roundtrip() {
        let dir = temp_dir("plain");
        let tar_path = dir.join("seed.tar");
        write_plain_tar(&tar_path);
        let dest = dir.join("rootfs");
        std::fs::create_dir_all(&dest).unwrap();
        let st = unpack(&tar_path, &dest, false, 0).unwrap();
        assert_eq!(st.entries, 2);
        assert!(st.bytes > 0);
        let out = std::fs::read(dest.join("opt/agentos/config/kernel/storage.yaml")).unwrap();
        assert_eq!(out, b"hello seed");
        let exe = std::fs::read(dest.join("opt/agentos/agentos-kernel")).unwrap();
        assert_eq!(exe, b"ELF!");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn unpack_zst_roundtrip() {
        let dir = temp_dir("zst");
        let tar_path = dir.join("seed.tar");
        write_plain_tar(&tar_path);
        let zst_path = dir.join("seed.tar.zst");
        let src = File::open(&tar_path).unwrap();
        let mut enc = zstd::stream::Encoder::new(File::create(&zst_path).unwrap(), 3).unwrap();
        std::io::copy(&mut BufReader::new(src), &mut enc).unwrap();
        enc.finish().unwrap();
        let dest = dir.join("rootfs");
        std::fs::create_dir_all(&dest).unwrap();
        let st = unpack(&zst_path, &dest, true, 0).unwrap();
        assert_eq!(st.entries, 2);
        assert!(dest.join("opt/agentos/agentos-kernel").is_file());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn unpack_empty_archive_is_error() {
        let dir = temp_dir("empty");
        let tar_path = dir.join("seed.tar");
        let f = File::create(&tar_path).unwrap();
        let mut tb = tar::Builder::new(f);
        tb.finish().unwrap();
        let dest = dir.join("rootfs");
        std::fs::create_dir_all(&dest).unwrap();
        assert!(unpack(&tar_path, &dest, false, 0).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn unpack_missing_seed_is_error() {
        let dir = temp_dir("missing");
        let dest = dir.join("rootfs");
        std::fs::create_dir_all(&dest).unwrap();
        let err = unpack(&dir.join("nope.tar"), &dest, false, 0).unwrap_err();
        assert!(err.contains("种子打开失败"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn unpack_rebuilds_symlink_on_unix() {
        let dir = temp_dir("symlink");
        let tar_path = dir.join("seed.tar");
        let f = File::create(&tar_path).unwrap();
        let mut tb = tar::Builder::new(f);
        let mut h = tar::Header::new_gnu();
        h.set_size(0);
        h.set_entry_type(tar::EntryType::Symlink);
        h.set_mode(0o777);
        tb.append_link(&mut h, "opt/agentos/python/bin/python3", "python3.12")
            .unwrap();
        tb.finish().unwrap();
        let dest = dir.join("rootfs");
        std::fs::create_dir_all(&dest).unwrap();
        unpack(&tar_path, &dest, false, 0).unwrap();
        let md = std::fs::symlink_metadata(dest.join("opt/agentos/python/bin/python3")).unwrap();
        assert!(md.file_type().is_symlink());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
