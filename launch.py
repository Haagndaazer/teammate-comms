"""Start the MCP server from this folder's sources, whether or not the package is installed."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from teammate_comms.server import main  # noqa: E402

if __name__ == "__main__":
    main()
