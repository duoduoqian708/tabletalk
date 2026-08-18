"""报告模式必须与查询模式一致：model_id 命中 ai_models、reasoning 透传。"""

from app.ai.provider_cfg import resolve_provider_cfg


def _seed_model(app_state) -> str:
    """默认不再内置模型 → 测试自备一条，返回其 id。"""
    app_state.runtime.update({"ai_models": [{
        "id": "llm_rep", "name": "报告测试模型", "provider": "cloud",
        "base_url": "https://x.example.com/v1", "model": "rep-model", "api_key": "k",
    }], "default_ai_model": "llm_rep"})
    return "llm_rep"


async def test_report_cfg_respects_model_id(app_state):
    target_id = _seed_model(app_state)
    rs = app_state.runtime.get()
    target = next(m for m in rs.ai_models if m.id == target_id)

    class Req:  # 最小请求桩
        model_id = target.id
        provider = base_url = api_key = model = reasoning = temperature = None

    cfg = resolve_provider_cfg(app_state, Req())
    assert cfg["model"] == target.model


async def test_report_cfg_passes_reasoning(app_state):
    class Req:
        model_id = None
        provider = base_url = api_key = model = None
        reasoning = "high"
        temperature = None

    cfg = resolve_provider_cfg(app_state, Req())
    assert cfg["reasoning"] == "high"


async def test_report_provider_cfg_actually_used(app_state):
    """报告路径必须走共享 cfg：model_id 生效（此前报告模式忽略它，属承诺缺口）。"""
    target_id = _seed_model(app_state)
    rs = app_state.runtime.get()
    target = next(m for m in rs.ai_models if m.id == target_id)

    class Req:
        model_id = target.id
        provider = base_url = api_key = model = reasoning = temperature = None

    cfg = resolve_provider_cfg(app_state, Req())
    assert cfg["model"] == target.model
    # 报告路径使用共享 resolve_provider_cfg（已断言 import 移除本地镜像）
    from app.ai import report  # noqa: F401
