"""Independent outcome checks with an optional DeepEval judge."""

from __future__ import annotations

import math
import os
from typing import Protocol

from pydantic import BaseModel, JsonValue

from .models import Case, Metric, MetricResult, json_bytes


class Judge(Protocol):
    def measure(
        self, metric: Metric, case: Case, actual: JsonValue
    ) -> tuple[float, str]: ...


def _text(value: JsonValue) -> str:
    return value if isinstance(value, str) else json_bytes(value).decode("utf-8")


def _subset(expected: JsonValue, actual: JsonValue) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _subset(value, actual[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(
                _subset(left, right)
                for left, right in zip(expected, actual, strict=True)
            )
        )
    return json_bytes(expected) == json_bytes(actual)


def score(
    metric: Metric, case: Case, actual: JsonValue, judge: Judge | None
) -> MetricResult:
    if metric.kind == "geval":
        if judge is None:
            raise RuntimeError("GEval requires an explicitly enabled judge")
        value, reason = judge.measure(metric, case, actual)
        if (
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise ValueError("judge returned an invalid score")
    else:
        if metric.kind == "exact_match":
            passed = json_bytes(case.expected_output) == json_bytes(actual)
        elif metric.kind == "contains":
            passed = isinstance(actual, str) and case.expected_output in actual
        else:
            passed = _subset(case.expected_output, actual)
        value, reason = float(passed), "matched" if passed else "did not match"
    return MetricResult(
        name=metric.name,
        kind=metric.kind,
        threshold=metric.threshold,
        score=float(value),
        passed=value >= metric.threshold,
        reason=str(reason)[:4000],
    )


class DeepEvalJudge:
    """Lazy GEval with configurable criteria and a private model-gateway connection."""

    def __init__(self):
        # Evaluation content stays in the selected gateway and private artifacts.
        os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "1")
        os.environ.setdefault("DEEPEVAL_FILE_SYSTEM", "READ_ONLY")
        try:
            from deepeval.metrics import GEval
            from deepeval.models import DeepEvalBaseLLM
            from deepeval.test_case import LLMTestCase, SingleTurnParams
            from openai import AsyncOpenAI, OpenAI
        except ImportError:
            raise RuntimeError("install the judge extra to use GEval") from None
        api_key = os.environ.get("MODEL_GATEWAY_API_KEY")
        model = os.environ.get("AGENT_EVAL_JUDGE_MODEL")
        if not api_key or not model:
            raise RuntimeError(
                "MODEL_GATEWAY_API_KEY and AGENT_EVAL_JUDGE_MODEL are required"
            )
        base_url = os.environ.get("MODEL_GATEWAY_BASE_URL") or None

        class GatewayModel(DeepEvalBaseLLM):
            def __init__(self):
                self.sync = OpenAI(
                    api_key=api_key, base_url=base_url, timeout=60, max_retries=0
                )
                self.async_client = AsyncOpenAI(
                    api_key=api_key, base_url=base_url, timeout=60, max_retries=0
                )

            def load_model(self):
                return self.sync

            def get_model_name(self):
                # Provider/model identifiers must not enter SDK telemetry.
                return "model-gateway-judge"

            @staticmethod
            def request(prompt: str, schema: type[BaseModel] | None):
                if schema is not None:
                    prompt += "\nReturn only JSON matching this schema:\n" + json_bytes(
                        schema.model_json_schema()
                    ).decode("utf-8")
                return {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"} if schema else None,
                }

            def generate(self, prompt: str, schema: type[BaseModel] | None = None):
                response = self.sync.chat.completions.create(
                    **self.request(prompt, schema)
                )
                content = response.choices[0].message.content or ""
                return schema.model_validate_json(content) if schema else content

            async def a_generate(
                self, prompt: str, schema: type[BaseModel] | None = None
            ):
                response = await self.async_client.chat.completions.create(
                    **self.request(prompt, schema)
                )
                content = response.choices[0].message.content or ""
                return schema.model_validate_json(content) if schema else content

        self.model = GatewayModel()
        self.metric_type, self.case_type, self.params = (
            GEval,
            LLMTestCase,
            SingleTurnParams,
        )

    def measure(
        self, metric: Metric, case: Case, actual: JsonValue
    ) -> tuple[float, str]:
        params = [
            self.params.INPUT,
            self.params.ACTUAL_OUTPUT,
            self.params.EXPECTED_OUTPUT,
        ]
        if case.context:
            params.append(self.params.CONTEXT)
        # A fresh metric prevents a failed call from retaining a prior score.
        evaluator = self.metric_type(
            name=metric.name,
            criteria=metric.criteria,
            evaluation_params=params,
            model=self.model,
            threshold=metric.threshold,
            async_mode=False,
        )
        evaluator.measure(
            self.case_type(
                input=_text(case.input),
                actual_output=_text(actual),
                expected_output=_text(case.expected_output),
                context=case.context or None,
            )
        )
        # None and non-finite values are errors, never silently converted to zero.
        return evaluator.score, evaluator.reason or ""
