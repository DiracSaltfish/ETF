"""富国历史净值部分 PCF 回补入口。"""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
raise SystemExit(subprocess.call([sys.executable, str(ROOT / "backfill_sh_pcf.py"), "--managers", "富国", *sys.argv[1:]]))
