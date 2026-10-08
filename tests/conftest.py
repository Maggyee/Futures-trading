import os
import tempfile

# Set before any vn.py import; tests never read or overwrite live runtime state.
os.environ["WORKBENCH_RUNTIME"] = tempfile.mkdtemp(prefix="workbench-tests-")
os.environ["WORKBENCH_ORIGIN"] = "http://testserver"
from backend.config import ROOT, prepare_runtime

prepare_runtime()
# Resolve vn.py's runtime once, then restore pytest's discovery directory.
import vnpy.trader.utility

os.chdir(ROOT)
