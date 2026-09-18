from agent_trace_eval.metrics_export import prometheus_text


def test_prometheus_text_includes_genai_tokens():
    text = prometheus_text(
        {
            "success_rate": 1.0,
            "usd_per_task": 0.0002,
            "tool_recall": 1.0,
            "groundedness": 1.0,
            "guardrail_recall": 1.0,
            "guardrail_precision": 1.0,
            "p95_latency_ms": 2.4,
            "mean_input_tokens": 1200,
            "mean_output_tokens": 140,
        },
        scores=[{"task_id": "nsf-retry", "success": True}],
    )
    assert "eval_success_rate 1.000000" in text
    assert 'gen_ai_client_token_usage{gen_ai_token_type="input"}' in text
    assert 'eval_task_success{task_id="nsf-retry"} 1' in text
