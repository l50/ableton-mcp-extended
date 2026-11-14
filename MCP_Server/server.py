"""Entry point for the AbletonMCP server.

Run it by path (``python MCP_Server/server.py``) or through the
``ableton-mcp-extended`` script. By default this process is only a thin
crash-resilient supervisor (see supervisor.py) that runs ``server.py
--worker`` as a child and restarts it -- replaying the MCP handshake -- if it
dies. Claude Desktop never restarts a dead MCP server on its own, so without
this a single stray kill leaves the Ableton tools gone until the app is
relaunched. ``--worker`` or ``ABLETON_MCP_NO_SUPERVISOR=1`` serves MCP in
this process instead.
"""

import logging
import os
import sys

if not __package__:  # pragma: no cover - only when run by path
    # Put the repository root on sys.path in place of MCP_Server/, so the
    # absolute package imports below resolve and no module in MCP_Server/
    # can shadow a top-level module of the same name.
    _package_dir = os.path.dirname(os.path.abspath(__file__))
    if sys.path and os.path.abspath(sys.path[0]) == _package_dir:
        sys.path[0] = os.path.dirname(_package_dir)
    else:
        sys.path.insert(0, os.path.dirname(_package_dir))

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
# Configure logging before MCP_Server.app builds the FastMCP instance: FastMCP
# calls logging.basicConfig itself, and the first call wins.
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

from MCP_Server import supervisor, tools  # noqa: E402,F401  (importing tools registers them)
from MCP_Server.app import mcp  # noqa: E402

WORKER_FLAG = "--worker"


def _run_worker() -> None:
    """Serve MCP over stdio in this process."""
    worker_log = supervisor.setup_file_logging("AbletonMCPWorker")
    supervisor.install_signal_logging(worker_log)
    worker_log.info("worker started (ppid=%s)", os.getppid())
    mcp.run()


def main() -> None:
    """Run the worker in-process or supervise a worker subprocess."""
    if WORKER_FLAG in sys.argv[1:] or os.environ.get(supervisor.NO_SUPERVISOR_ENV):
        _run_worker()
        return
    server_path = os.path.abspath(__file__)
    worker_cmd = [sys.executable, server_path, WORKER_FLAG]
    repo_root = os.path.dirname(os.path.dirname(server_path))
    raise SystemExit(supervisor.run_supervisor(worker_cmd, cwd=repo_root))


if __name__ == "__main__":
    main()
