"""AI 离线黄金评测集结构完整性与端到端评测运行器回归单测。"""

from __future__ import annotations

import json
from pathlib import Path

from evals.run_evals import DEFAULT_DATASET_PATH, run_evaluation_suite


def test_golden_dataset_has_twelve_balanced_cases() -> None:
    """黄金评测集必须包含 12 条用例且均匀覆盖 4 大业务与安全维度（每维度 3 条）。"""
    assert DEFAULT_DATASET_PATH.is_file(), "缺少 evals/datasets/ecommerce_golden.json"
    payload = json.loads(DEFAULT_DATASET_PATH.read_text(encoding="utf-8"))
    cases = payload.get("cases", [])
    assert len(cases) == 12

    dim_counts: dict[str, int] = {}
    case_ids: set[str] = set()
    for item in cases:
        cid = item["id"]
        assert cid not in case_ids, f"重复的用例 ID: {cid}"
        case_ids.add(cid)
        dim = item["dimension"]
        dim_counts[dim] = dim_counts.get(dim, 0) + 1

    assert dim_counts == {
        "shopping_guide": 3,
        "order_aftersales": 3,
        "security_adversarial": 3,
        "merchant_analytics_sourcing": 3,
    }


async def test_run_evaluation_suite_mock_mode_passes_100_percent(tmp_path: Path) -> None:
    """运行离线异步评测器（mock 模式），12 条黄金用例应 100% 通过并正确导出 JSON 报告。"""
    out_report = tmp_path / "eval_report.json"
    report = await run_evaluation_suite(
        mode="mock",
        dataset_path=DEFAULT_DATASET_PATH,
        report_path=out_report,
    )

    assert out_report.is_file()
    persisted = json.loads(out_report.read_text(encoding="utf-8"))
    summary = persisted["summary"]

    assert summary["total_cases"] == 12
    assert summary["passed_cases"] == 12
    assert summary["overall_pass_rate"] == 1.0
    assert summary["tool_selection_accuracy"] == 1.0
    assert summary["schema_contract_pass_rate"] == 1.0
    assert summary["unauthorized_block_rate"] == 1.0
    assert summary["hitl_trigger_accuracy"] == 1.0
    assert summary["max_margin_formula_error"] == 0.0
    assert report["summary"] == summary
