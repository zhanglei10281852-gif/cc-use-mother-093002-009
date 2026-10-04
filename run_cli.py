"""命令行入口：python run_cli.py ..."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from consent_governance.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
