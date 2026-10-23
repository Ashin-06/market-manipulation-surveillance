"""
Benchmark Suite comparing `mktsurv` Level-3 Microstructure Surveillance Engine
against a Naive Regulatory Threshold Baseline (`Order-to-Trade Ratio > 15` / Cancel Rate > 85%).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Dict, List, Set

from .detectors import run_surveillance
from .simulate import generate_market_session


@dataclass
class MethodMetrics:
    """Classification metrics for a surveillance method."""

    name: str
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int
    precision: float
    recall: float
    f1_score: float
    hard_negative_fp_count: int
    hard_negative_fp_rate: float
    flagged_traders: List[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _compute_metrics(
    name: str,
    flagged: Set[str],
    positives: Set[str],
    negatives: Set[str],
    hard_negatives: Set[str],
) -> MethodMetrics:
    tp = len(flagged & positives)
    fp = len(flagged & negatives)
    fn = len(positives - flagged)
    tn = len(negatives - flagged)

    precision = tp / float(tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / float(tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (2.0 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    hn_fp = len(flagged & hard_negatives)
    hn_fpr = hn_fp / float(len(hard_negatives)) if hard_negatives else 0.0

    return MethodMetrics(
        name=name,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        true_negatives=tn,
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1_score=round(f1, 4),
        hard_negative_fp_count=hn_fp,
        hard_negative_fp_rate=round(hn_fpr, 4),
        flagged_traders=sorted(flagged),
    )


def run_benchmark(seed: int = 42) -> Dict[str, object]:
    """
    Runs the full Level-3 surveillance benchmark and compares `mktsurv` against
    the naive regulatory OTR / Cancel-Rate threshold baseline.
    """
    scenario = generate_market_session(symbol="ACME", seed=seed)
    lob, report = run_surveillance(
        scenario.events,
        symbol=scenario.symbol,
        vpin_bucket_volume=500,
        session_end_ts_ms=scenario.session_end_ts_ms,
    )

    positives = set(scenario.manipulative_traders)
    negatives = set(scenario.benign_traders)
    hard_negatives = set(scenario.hard_negative_traders)

    mktsurv_flagged = set(report.flagged_traders)
    naive_flagged = set(report.naive_baseline_flagged_traders)

    mktsurv_metrics = _compute_metrics(
        name="mktsurv (L3 Microstructure Engine)",
        flagged=mktsurv_flagged,
        positives=positives,
        negatives=negatives,
        hard_negatives=hard_negatives,
    )
    naive_metrics = _compute_metrics(
        name="Naive Regulatory Baseline (OTR > 15 / Cancel > 85%)",
        flagged=naive_flagged,
        positives=positives,
        negatives=negatives,
        hard_negatives=hard_negatives,
    )

    # Episode-level recall check
    manip_episodes = [ep for ep in scenario.episodes if ep.is_manipulation]
    detected_episodes = 0
    episode_results = []
    for ep in manip_episodes:
        hit = any(
            set(ep.traders) & set(a.involved_traders) and a.pattern == ep.pattern
            for a in report.unified_alerts
        )
        if hit:
            detected_episodes += 1
        episode_results.append(
            {
                "episode_id": ep.episode_id,
                "pattern": ep.pattern,
                "sub_type": ep.sub_type,
                "traders": ep.traders,
                "detected": hit,
            }
        )

    episode_recall = detected_episodes / float(max(1, len(manip_episodes)))

    return {
        "symbol": scenario.symbol,
        "total_events": len(lob.events),
        "total_fills": len(lob.fills),
        "ground_truth_manipulators": sorted(positives),
        "ground_truth_hard_negatives": sorted(hard_negatives),
        "mktsurv": mktsurv_metrics.to_dict(),
        "naive_baseline": naive_metrics.to_dict(),
        "episode_detection_rate": round(episode_recall, 4),
        "episode_results": episode_results,
        "vpin_summary": {
            "bucket_volume": report.vpin.bucket_volume,
            "num_buckets": len(report.vpin.buckets),
            "max_vpin": round(report.vpin.max_vpin, 4),
            "mean_vpin": round(report.vpin.mean_vpin, 4),
            "toxic_bucket_indices": report.vpin.toxic_bucket_indices,
        },
        "alerts_summary": {
            "spoofing_layering": len(report.spoofing_alerts),
            "wash_trading": len(report.wash_trading_alerts),
            "marking_the_close": len(report.closing_ramp_alerts),
            "front_running": len(report.front_running_alerts),
            "total_alerts": len(report.unified_alerts),
        },
    }


def format_benchmark_table(bench: Dict[str, object]) -> str:
    """Formats benchmark results into a clean console report."""
    m = bench["mktsurv"]  # type: ignore[index]
    n = bench["naive_baseline"]  # type: ignore[index]
    m_tpfp = f"{m['true_positives']} TP / {m['false_positives']} FP"
    n_tpfp = f"{n['true_positives']} TP / {n['false_positives']} FP"
    lines = [
        "=" * 86,
        "  LEVEL-3 MARKET MANIPULATION SURVEILLANCE BENCHMARK (`mktsurv` vs Naive OTR)",
        "=" * 86,
        f"  Symbol: {bench['symbol']}  |  L3 Events: {bench['total_events']}  |  Matched Fills: {bench['total_fills']}",
        "-" * 86,
        f"  {'Metric':<38} | {'mktsurv (L3 Engine)':<20} | {'Naive OTR > 15':<20}",
        "-" * 86,
        f"  {'Precision':<38} | {m['precision']*100:>18.2f}% | {n['precision']*100:>18.2f}%",
        f"  {'Recall':<38} | {m['recall']*100:>18.2f}% | {n['recall']*100:>18.2f}%",
        f"  {'F1 Score':<38} | {m['f1_score']*100:>18.2f}% | {n['f1_score']*100:>18.2f}%",
        f"  {'False Positives (MM & TWAP/Iceberg)':<38} | {m['hard_negative_fp_rate']*100:>18.2f}% | {n['hard_negative_fp_rate']*100:>18.2f}%",
        f"  {'True Positives / False Positives':<38} | {m_tpfp:>19} | {n_tpfp:>19}",
        "-" * 86,
        f"  Episode Recall: {bench['episode_detection_rate']*100:.1f}%  |  Max VPIN: {bench['vpin_summary']['max_vpin']:.4f}",  # type: ignore[index]
        "=" * 86,
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    res = run_benchmark()
    print(format_benchmark_table(res))
    print(json.dumps(res, indent=2))
