"""报告模式必须与查询模式一致：model_id 命中 ai_models、reasoning 透传。"""

from app.ai.provider_cfg import resolve_provider_cfg


async def test_report_cfg_respects_model_id(app_state):
    rs = app_state.runtime.get()
    target = rs.ai_models[0]

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
    rs = app_state.runtime.get()
    target = rs.ai_models[0]

    class Req:
        model_id = target.id
        provider = base_url = api_key = model = reasoning = temperature = None

    cfg = resolve_provider_cfg(app_state, Req())
    assert cfg["model"] == target.model
    # 报告路径使用共享 resolve_provider_cfg（已断言 import 移除本地镜像）
    from app.ai import report  # noqa: F401
