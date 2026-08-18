"""易方达历史 PCF 回补入口。"""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
raise SystemExit(subprocess.call([sys.executable, str(ROOT / "backfill_sh_pcf.py"), "--managers", "易方达", *sys.argv[1:]]))
