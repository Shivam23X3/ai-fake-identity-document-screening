"""Signal fusion: indicators + coverage → risk score (0-100) + verdict.

Fusion rules (deliberately conservative, fully explainable):
- Each indicator contributes ``weight(type) * factor(severity) * confidence``
  risk points; ``low``-severity signals are dampened and the whole set is
  normalized by the strongest signal, so a pile of weak heuristics can never
  manufacture "suspicious" on a document whose best evidence is weak.
- Non-informational indicators are what can make a document suspicious;
  ``info`` indicators are context only and never route the verdict.
- The verdict is routed from the fused score AND the number of independent
  detector types that fired meaningfully (confidence >= 0.55):
    * any coverage/analysis problem → ``inconclusive``
    * >= 55 points or >= 3 agreeing detector types → ``likely_manipulated``
    * >= 20 points or any medium/high indicator → ``suspicious``
    * else → ``no_obvious_manipulation``
- ELA can *never* push the verdict to likely_manipulated on its own: its
  per-indicator cap keeps a maxed-out ELA below the "likely" threshold.
- A small constant noise floor keeps low scores honest — absence of
  indicators must not look like proof of authenticity.
"""
from __future__ import annotations

import math
from typing import Any

from app.services.tampering.constants import (
    AGREEMENT_CONFIDENCE_MIN,
    BASE_NOISE_POINTS,
    INCONCLUSIVE_COVERAGE_MAX,
    LIKELY_AGREEING_INDICATORS,
    MAX_INDIVIDUAL_POINTS,
    RISK_CAP,
    RISK_LIKELY_MIN,
    RISK_SUSPICIOUS_MIN,
    SEVERITY_FACTORS,
    VERDICT_CLEAN,
    VERDICT_INCONCLUSIVE,
    VERDICT_LIKELY,
    VERDICT_SUSPICIOUS,
)


def _indicator_points(ind: dict[str, Any]) -> float:
    from app.services.tampering.constants import INDICATOR_WEIGHTS

    weight = INDICATOR_WEIGHTS.get(ind.get("type", ""), 10.0)
    factor = SEVERITY_FACTORS.get(ind.get("severity", "info"), 0.0)
    confidence = max(0.0, min(1.0, float(ind.get("confidence", 0.0) or 0.0)))
    raw = weight * factor * (0.5 + 0.5 * confidence)
    return min(raw, MAX_INDIVIDUAL_POINTS)


def fuse_signals(
    indicators: list[dict[str, Any]],
    *,
    coverage: float,
    detector_failures: list[str],
    model_probability: float | None,
) -> dict[str, Any]:
    """Combine indicators into the final tampering payload.

    ``coverage``: fraction of the image usable by the forensic detectors
    (textured, decodable, at sufficient resolution). ``detector_failures``
    lists detector names that crashed — too many failures ⇒ inconclusive.
    """
    non_info = [i for i in indicators if i.get("severity") != "info"]

    points_list = [_indicator_points(i) for i in non_info]
    contributions: list[dict[str, Any]] = []

    # Normalize by the strongest signal: weak heuristics stacking cannot
    # outweigh the best available evidence.
    max_pts = max(points_list, default=0.0)
    norm = max(max_pts, 1.0)
    for ind, pts in zip(non_info, points_list):
        # The strongest signal counts in full; weaker ones are damped.
        effective = pts if pts >= max_pts else pts * (pts / norm)
        contributions.append(
            {
                "type": ind.get("type"),
                "severity": ind.get("severity"),
                "confidence": ind.get("confidence"),
                "points": round(effective, 1),
            }
        )

    raw_sum = sum(c["points"] for c in contributions)

    # Saturating aggregate: sqrt-compress so many signals escalate
    # sub-linearly, then rescale to 0-100.
    saturated = math.sqrt(raw_sum) * 10.0

    # Trained model (when present) shifts the score toward its probability.
    if model_probability is not None:
        p = max(0.0, min(1.0, float(model_probability)))
        model_points = 30.0 * p          # up to 30 points
        saturated = saturated * 0.7 + (saturated + model_points) * 0.3

    risk = min(RISK_CAP, BASE_NOISE_POINTS + saturated)
    risk_score = round(risk, 1)

    # Independent detector types that fired meaningfully.
    agreeing = sorted(
        {
            ind["type"]
            for ind in non_info
            if float(ind.get("confidence", 0.0) or 0.0) >= AGREEMENT_CONFIDENCE_MIN
        }
    )

    # Severity evidence used directly by the router.
    has_strong = any(i.get("severity") in {"medium", "high"} for i in non_info)

    # --- verdict routing ---------------------------------------------------
    if len(detector_failures) >= 3 or coverage < INCONCLUSIVE_COVERAGE_MAX:
        verdict = VERDICT_INCONCLUSIVE
    elif risk_score >= RISK_LIKELY_MIN or len(agreeing) >= LIKELY_AGREEING_INDICATORS:
        verdict = VERDICT_LIKELY
    elif risk_score >= RISK_SUSPICIOUS_MIN or has_strong:
        verdict = VERDICT_SUSPICIOUS
    else:
        verdict = VERDICT_CLEAN

    # Confidence in the *verdict itself* (not in tampering existing):
    # grows with the number of agreeing detectors and image coverage.
    if verdict == VERDICT_INCONCLUSIVE:
        verdict_conf = 0.2
    elif verdict == VERDICT_CLEAN:
        verdict_conf = min(0.85, 0.5 + coverage * 0.35)
    else:
        base = 0.45 + 0.12 * len(agreeing)
        verdict_conf = min(0.95, base * (0.6 + 0.4 * coverage))

    human_review = verdict in {VERDICT_SUSPICIOUS, VERDICT_LIKELY, VERDICT_INCONCLUSIVE}

    explanation = _explanation(verdict, risk_score, non_info, agreeing, detector_failures)

    return {
        "tampering_detected": verdict in {VERDICT_SUSPICIOUS, VERDICT_LIKELY},
        "risk_score": risk_score,               # 0-100
        "confidence": round(verdict_conf, 3),   # 0-1 confidence in the verdict
        "verdict": verdict,
        "verdict_confidence": round(verdict_conf, 3),
        "indicators": indicators,
        "contributions": contributions,
        "agreeing_types": agreeing,
        "coverage": round(coverage, 3),
        "detector_failures": detector_failures,
        "explanation": explanation,
        "human_review_required": human_review,
        "disclaimer": (
            "Heuristic forensic signals only — NOT proof of forgery. ELA and "
            "statistical detectors produce false positives on legitimate scans "
            "(text, guilloche, saturation) and can miss sophisticated edits. "
            "Every non-clean verdict requires human inspection of the original."
        ),
    }


def _explanation(
    verdict: str,
    risk_score: float,
    indicators: list[dict[str, Any]],
    agreeing: list[str],
    failures: list[str],
) -> str:
    fired_desc = ", ".join(sorted({str(i.get("type")) for i in indicators})) or "none"
    if verdict == VERDICT_LIKELY:
        return (
            f"Multiple independent forensic signals agree ({fired_desc}); fused "
            f"risk {risk_score:.0f}/100. Treat as likely manipulated pending "
            f"expert human inspection — still not forensic proof."
        )
    if verdict == VERDICT_SUSPICIOUS:
        return (
            f"Some forensic signals fired ({fired_desc}); fused risk "
            f"{risk_score:.0f}/100. Individually these are explainable by "
            f"scanning/printing artifacts; manual inspection recommended."
        )
    if verdict == VERDICT_INCONCLUSIVE:
        reasons = ", ".join(failures) if failures else "low image coverage/quality"
        return (
            f"Analysis could not be completed reliably ({reasons}). "
            f"No conclusion about tampering is possible; inspect the original."
        )
    return (
        "Forensic detectors ran and found no indicators worth flagging. This "
        "is NOT a certificate of authenticity — subtle forgeries can evade "
        "all heuristics."
    )
