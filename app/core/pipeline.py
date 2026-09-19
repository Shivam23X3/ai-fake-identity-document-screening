"""Pipeline core: the backbone that every AI module plugs into.

Design rules
------------
1. A stage is a class implementing :class:`PipelineStage`. It declares:
   - ``name``: unique stage id
   - ``requires``: context keys it needs to run
   - ``provides``: context keys it produces
   - ``run(ctx)``: does the work, returns a :class:`StageResult`

2. Every stage returns a :class:`StageResult` with:
   - ``status``: ok | not_implemented | error | skipped
   - ``confidence``: float in [0, 1] or None when not applicable
   - ``data``: JSON-serializable payload
   - ``human_review_required``: True when the stage cannot decide
     reliably (low confidence, error, missing input, or high risk).

3. The pipeline NEVER raises out of a stage: failures are captured,
   recorded, and by default the run continues (decision-support tool,
   not a brittle auto-rejector).

4. ``PipelineContext`` is a dict of JSON-serializable values shared
   between stages, plus helper accessors.
"""
from __future__ import annotations

import logging
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)


class StageStatus:
    OK = "ok"
    NOT_IMPLEMENTED = "not_implemented"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass
class StageResult:
    """Uniform output of every pipeline stage."""

    stage: str
    status: str = StageStatus.OK
    confidence: float | None = None          # 0.0 - 1.0, None = not applicable
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    human_review_required: bool = False
    duration_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "confidence": self.confidence,
            "data": self.data,
            "error": self.error,
            "human_review_required": self.human_review_required,
            "duration_ms": self.duration_ms,
        }


class PipelineStage(Protocol):
    """Contract for every pluggable stage."""

    name: str
    requires: tuple[str, ...]
    provides: tuple[str, ...]

    def run(self, ctx: "PipelineContext") -> StageResult: ...


class PipelineContext:
    """Shared, JSON-serializable blackboard passed through the pipeline."""

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = dict(initial or {})

    # -- dict-like access ------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def has(self, *keys: str) -> bool:
        return all(k in self._data and self._data[k] is not None for k in keys)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)


@dataclass
class Pipeline:
    """Ordered chain of stages with failure isolation."""

    name: str
    stages: list[PipelineStage]
    failure_policy: str = "skip"  # "skip" | "fail"

    def run(self, ctx: PipelineContext) -> list[StageResult]:
        results: list[StageResult] = []
        for stage in self.stages:
            results.append(self._run_stage(stage, ctx))
            # Publish completed outcomes so later stages (risk fusion) can
            # weigh error/skip evidence from their predecessors.
            ctx.set("stage_results", [r.to_dict() for r in results])
            if stage.name in {r.stage for r in results if r.status == StageStatus.ERROR} \
                    and self.failure_policy == "fail":
                break
        return results

    def _run_stage(self, stage: PipelineStage, ctx: PipelineContext) -> StageResult:
        started = time.perf_counter()
        logger.info("stage '%s' starting", stage.name)

        # Dependency check: skip instead of crashing when inputs are absent.
        missing = [k for k in stage.requires if not ctx.has(k)]
        if missing:
            return StageResult(
                stage=stage.name,
                status=StageStatus.SKIPPED,
                data={"missing_inputs": missing},
                human_review_required=True,
                duration_ms=_elapsed_ms(started),
            )

        try:
            result = stage.run(ctx)
        except Exception as exc:  # noqa: BLE001 - isolation is the point
            logger.exception("stage '%s' crashed", stage.name)
            result = StageResult(
                stage=stage.name,
                status=StageStatus.ERROR,
                error=f"{type(exc).__name__}: {exc}",
                data={"trace": traceback.format_exc(limit=3)},
                human_review_required=True,
            )

        result.duration_ms = _elapsed_ms(started)

        # Store stage outputs into the context so later stages can use them.
        # not_implemented stages still publish pass-through keys so the full
        # chain remains observable while engines are being plugged in.
        if result.status in {StageStatus.OK, StageStatus.NOT_IMPLEMENTED}:
            for key in stage.provides:
                if key in result.data:
                    ctx.set(key, result.data[key])

        return result


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def screening_run_id() -> str:
    return uuid.uuid4().hex
