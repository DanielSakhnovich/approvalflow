import sys
from pathlib import Path

# Add libs and libs/afcommon to Python path for absolute imports
repo_root = Path(__file__).parent
afcommon_dir = repo_root / "libs" / "afcommon"
if str(afcommon_dir) not in sys.path:
    sys.path.insert(0, str(afcommon_dir))
