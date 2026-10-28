"""P2-21 回归测试：评估门禁资产缺失不得静默 exit 0。

- 缺 gold / gold 为空 / 相关项不足 默认 exit 2（CI 不得静默通过）；
- 仅显式 `--allow-missing` 才 exit 0 跳过。
"""
import importlib.util
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_gate(args):
    proc = subprocess.run(
        [sys.executable, os.path.join(REPO, "scripts/eval_feedback_gate.py"), *args],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_gate_missing_gold_exits_nonzero_by_default(tmp_path):
    missing = tmp_path / "nope.json"
    code, out = _run_gate([f"--gold={missing}"])
    assert code == 2, f"期望 exit 2，实际 {code}；输出：{out}"
    assert "exit 2" in out


def test_gate_missing_gold_exits_zero_only_with_allow_missing(tmp_path):
    missing = tmp_path / "nope.json"
    code, out = _run_gate([f"--gold={missing}", "--allow-missing"])
    assert code == 0, f"--allow-missing 应 exit 0，实际 {code}；输出：{out}"
    assert "[skip]" in out


def test_gate_empty_gold_exits_nonzero(tmp_path):
    g = tmp_path / "empty.json"
    g.write_text(json.dumps({"queries": []}), encoding="utf-8")
    code, _ = _run_gate([f"--gold={g}"])
    assert code == 2


def test_gate_passes_when_assets_present(tmp_path):
    g = tmp_path / "ok.json"
    g.write_text(json.dumps({"queries": [
        {"query": "q1", "relevant": [f"douban:{i}" for i in range(1, 20)]},
    ]}), encoding="utf-8")
    code, out = _run_gate([f"--gold={g}", "--distractors", "6"])
    assert code == 0, f"有资产应 exit 0，实际 {code}；输出：{out}"
    assert "[PASS]" in out or "平均" in out
