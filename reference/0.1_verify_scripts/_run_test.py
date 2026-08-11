"""运行 generate_resume.py 并验证产出"""
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
script = SCRIPT_DIR / "generate_resume.py"

# 生成中文版（不导出 PDF 以避免依赖）
result = subprocess.run(
    [sys.executable, str(script), "--lang", "zh"],
    capture_output=True, text=True, encoding="utf-8",
    cwd=str(SCRIPT_DIR)
)
print("STDOUT:")
print(result.stdout)
print("STDERR:")
print(result.stderr)
print(f"Return code: {result.returncode}")

# 检查产出文件
print("\n📁 生成的产出文件：")
for f in sorted(SCRIPT_DIR.glob("*")):
    if f.is_file() and f.name not in ("_test_minimal.py", "_run_test.py", "_test_gen.py", "generate_resume.py", "resume_data.yaml"):
        size_kb = f.stat().st_size / 1024
        print(f"   ✅ {f.name}  ({size_kb:.1f} KB)")