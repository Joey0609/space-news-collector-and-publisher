# -*- coding: utf-8 -*-
"""Dashboard selftest: service output forwarding is importable and labeled."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard.server as server

def test_service_labels():
    assert server.SERVICE_NAMES == {"collect": "采集", "process": "处理", "distribute": "分发"}
    assert callable(server.pump_service_output)

if __name__ == "__main__":
    test_service_labels()
    print("dashboard output forwarding: OK")
