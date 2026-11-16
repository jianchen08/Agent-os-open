//! seedx bin 薄壳：参数解析 + 退出码翻译。逻辑在 lib（可测）。

use std::path::PathBuf;

const USAGE: &str = "用法: seedx --seed <rootfs_seed.tar[.zst]> --dest <目录> [--zst] [--progress-every N]\n退出码: 0 成功；2 参数错；1 解包失败（stderr 有原因）";

fn main() {
    let mut seed: Option<PathBuf> = None;
    let mut dest: Option<PathBuf> = None;
    let mut zst = false;
    let mut progress_every = 0u64;
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--seed" => seed = args.next().map(PathBuf::from),
            "--dest" => dest = args.next().map(PathBuf::from),
            "--zst" => zst = true,
            "--progress-every" => {
                progress_every = args.next().and_then(|v| v.parse().ok()).unwrap_or(0)
            }
            "-h" | "--help" => {
                println!("{USAGE}");
                return;
            }
            other => {
                eprintln!("[seedx][ERROR] 未知参数: {other}\n{USAGE}");
                std::process::exit(2);
            }
        }
    }
    let (Some(seed), Some(dest)) = (seed, dest) else {
        eprintln!("[seedx][ERROR] 缺 --seed/--dest\n{USAGE}");
        std::process::exit(2);
    };
    match seedx::unpack(&seed, &dest, zst, progress_every) {
        Ok(st) => {
            eprintln!("[seedx] OK entries={} bytes={}", st.entries, st.bytes);
        }
        Err(e) => {
            eprintln!("[seedx][ERROR] {e}");
            std::process::exit(1);
        }
    }
}
