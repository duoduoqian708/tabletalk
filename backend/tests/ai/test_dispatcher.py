"""意图调度：识别技能；拿不准/mock 兜底回 query。"""

from app.ai.agent.dispatcher import dispatch_skill


async def test_dispatch_defaults_to_query(app_state):
    # mock 下无关键词 → query
    assert await dispatch_skill(app_state, "随便一句话") == "query"


async def test_dispatch_report_keyword(app_state):
    # mock 下"报告"关键词 → report
    assert await dispatch_skill(app_state, "出一份销售分析报告") == "report"
