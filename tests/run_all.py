"""跑完全部階段的回歸測試：python tests/run_all.py（需在 repo 根目錄執行）"""
import pathlib
import subprocess
import sys

here = pathlib.Path(__file__).parent
fail = 0
for f in sorted(here.glob("test_stage*.py")):
    r = subprocess.run([sys.executable, str(f)], capture_output=True, text=True)
    ok = sum(1 for ln in r.stdout.splitlines() if ln.startswith("  OK"))
    bad = [ln for ln in r.stdout.splitlines() if ln.startswith("  FAIL")]
    print(f"{f.name:20s} 通過 {ok:3d}" + (f"　失敗 {len(bad)}" if bad else ""))
    for ln in bad:
        print("   ", ln.strip())
    if r.returncode:
        fail += 1
        if not bad:
            print(r.stderr[-800:])
print("全部通過" if not fail else f"{fail} 個檔案有失敗")
sys.exit(1 if fail else 0)
