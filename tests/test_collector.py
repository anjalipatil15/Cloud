"""Collector parsing tests (no AWS needed). Run: python -m pytest tests  (or python tests/test_collector.py)"""
import base64
import gzip
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The collector ships cost.py/pricing.json next to app.py (copied at build time).
for name in ("cost.py", "pricing.json"):
    shutil.copy(ROOT / "ml" / name, ROOT / "collector" / name)
sys.path.insert(0, str(ROOT / "collector"))

import app  # noqa: E402

COLD_REPORT = (
    "REPORT RequestId: 3f1c-aa\tDuration: 2.15 ms\tBilled Duration: 812 ms\t"
    "Memory Size: 512 MB\tMax Memory Used: 98 MB\tInit Duration: 809.33 ms\t"
)
WARM_REPORT = (
    "REPORT RequestId: 77bd-cc\tDuration: 1.02 ms\tBilled Duration: 2 ms\t"
    "Memory Size: 512 MB\tMax Memory Used: 98 MB\t"
)
SNAP_REPORT = (
    "REPORT RequestId: 9e9e-dd\tDuration: 3.50 ms\tBilled Duration: 120 ms\t"
    "Memory Size: 512 MB\tMax Memory Used: 98 MB\tRestore Duration: 210.40 ms\t"
    "Billed Restore Duration: 116 ms\t"
)
IMPORTS = [
    "import time: self [us] | cumulative | imported package",
    "import time:       850 |        850 |   _io",
    "import time:     12000 |     402311 | pandas",
    "import time:      3100 |       3100 |     numpy.core._multiarray_umath",
]


def payload(group, messages, stream="2026/10/19/[$LATEST]abc"):
    return {
        "messageType": "DATA_MESSAGE",
        "logGroup": group,
        "logStream": stream,
        "logEvents": [{"id": str(i), "timestamp": 1760870000000 + i, "message": m} for i, m in enumerate(messages)],
    }


def test_parse_report_cold_and_warm():
    cold = app.parse_report(COLD_REPORT)
    assert cold["request_id"] == "3f1c-aa"
    assert cold["init_ms"] == 809.33 and cold["billed_ms"] == 812 and cold["duration_ms"] == 2.15
    assert cold["is_cold"] is True
    warm = app.parse_report(WARM_REPORT)
    assert warm["is_cold"] is False and "init_ms" not in warm


def test_parse_report_snapstart():
    row = app.parse_report(SNAP_REPORT)
    assert row["restore_ms"] == 210.40 and row["is_cold"] is True
    assert "init_ms" not in row


def test_build_rows_joins_imports_to_cold_start():
    inv, prof, n_lines = app.build_rows(payload("/aws/lambda/data-512-x86-zip", IMPORTS + [COLD_REPORT]))
    assert len(inv) == 1
    row = inv[0]
    assert row["workload"] == "data" and row["memory_mb"] == 512 and row["arch"] == "x86"
    assert n_lines == 3  # header line is not a module
    assert row["log_stream"] == "2026/10/19/[$LATEST]abc"
    assert row["init_cost_usd"] > 0
    # _io (850 us) is below the 1 ms threshold
    assert sorted(p["module"] for p in prof) == ["numpy.core._multiarray_umath", "pandas"]


def test_unknown_function_ignored():
    assert app.build_rows(payload("/aws/lambda/observatory-collector", [COLD_REPORT])) == ([], [], 0)


def test_imports_split_across_batches():
    # CloudWatch may deliver one cold start's import lines in several batches.
    stream = "2026/10/19/[$LATEST]split"
    _, _, first = app.build_rows(payload("/aws/lambda/data-512-x86-zip", IMPORTS[:2], stream))
    inv, _, second = app.build_rows(payload("/aws/lambda/data-512-x86-zip", IMPORTS[2:] + [COLD_REPORT], stream))
    assert first + second == 3
    assert inv[0]["log_stream"] == stream


def test_decode_roundtrip():
    p = payload("/aws/lambda/tiny-128-arm64-zip", [WARM_REPORT])
    event = {"awslogs": {"data": base64.b64encode(gzip.compress(json.dumps(p).encode())).decode()}}
    assert app.decode(event) == p


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
