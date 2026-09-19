"""Pipeline core behavior tests (from Step 1, kept green in Step 2)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.pipeline import (
    Pipeline,
    PipelineContext,
    StageResult,
    StageStatus,
)

# NOTE: DB-touching tests live in test_step2_api.py (session-scoped client).
test_client_module = TestClient  # re-export for readability


def test_stage_result_defaults() -> None:
    r = StageResult(stage="x")
    assert r.status == StageStatus.OK
    assert r.confidence is None
    assert r.human_review_required is False
    assert r.to_dict()["stage"] == "x"


def test_pipeline_skips_stage_with_missing_inputs() -> None:
    class NeedsThing:
        name = "needs_thing"
        requires = ("thing",)
        provides = ()

        def run(self, ctx: PipelineContext) -> StageResult:  # noqa: ARG002
            return StageResult(stage=self.name)  # pragma: no cover

    pipe = Pipeline(name="t", stages=[NeedsThing()])
    results = pipe.run(PipelineContext({}))
    assert results[0].status == StageStatus.SKIPPED
    assert results[0].human_review_required is True
    assert results[0].data["missing_inputs"] == ["thing"]


def test_pipeline_isolates_crashing_stage() -> None:
    class Boom:
        name = "boom"
        requires = ()
        provides = ()

        def run(self, ctx: PipelineContext) -> StageResult:  # noqa: ARG002
            raise RuntimeError("boom")

    class After:
        name = "after"
        requires = ()
        provides = ()

        def run(self, ctx: PipelineContext) -> StageResult:  # noqa: ARG002
            return StageResult(stage=self.name, data={"ok": True})

    pipe = Pipeline(name="t", stages=[Boom(), After()])
    results = pipe.run(PipelineContext({}))
    assert results[0].status == StageStatus.ERROR
    assert "boom" in (results[0].error or "")
    assert results[1].status == StageStatus.OK  # run continued


def test_pipeline_passes_outputs_to_context() -> None:
    class Producer:
        name = "producer"
        requires = ()
        provides = ("value",)

        def run(self, ctx: PipelineContext) -> StageResult:
            return StageResult(stage=self.name, data={"value": 42})

    class Consumer:
        name = "consumer"
        requires = ("value",)
        provides = ()

        def run(self, ctx: PipelineContext) -> StageResult:
            return StageResult(stage=self.name, data={"seen": ctx.get("value")})

    pipe = Pipeline(name="t", stages=[Producer(), Consumer()])
    results = pipe.run(PipelineContext({}))
    assert results[1].data["seen"] == 42


def test_placeholder_stages_publish_passthrough_keys() -> None:
    """not_implemented stages must still forward pass-through outputs."""
    from app.pipelines.stages import PreprocessStage, OcrStage

    ctx = PipelineContext({"original_path": "/tmp/x.png"})
    pipe = Pipeline(name="t", stages=[PreprocessStage(), OcrStage()])
    results = pipe.run(ctx)

    assert results[0].status == StageStatus.NOT_IMPLEMENTED
    assert ctx.get("preprocessed_image_path") == "/tmp/x.png"
    assert results[1].status == StageStatus.NOT_IMPLEMENTED
    assert ctx.get("ocr_fields") == []
    assert all(r.human_review_required for r in results)
