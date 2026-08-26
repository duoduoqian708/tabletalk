"""段7：构建进度总线测试——overall 单调窗口映射 / 子步元数据 / 失败定位 / SSE 帧流。"""
from __future__ import annotations

import json

from app.knowledge.jobs import PHASES, _new_progress, _overall, _step_label


def test_overall_window_monotonic_mapping():
    """阶段内 0-100 映射到全局窗口，且典型构建序列全局单调不降。"""
    # 窗口边界
    assert _overall(None, 5) == 5              # 发现结构（全局原始）
    assert _overall("annotate", 0) == 16        # 阶段一窗口起点
    assert _overall("annotate", 100) == 45
    assert _overall("tags", 50) == 52           # 45 + 15*0.5
    assert _overall("graph", 0) == 60
    assert _overall("graph", 100) == 78
    assert _overall(None, 98) == 98             # 落盘（全局原始）
    # 典型构建序列：结构性 10 → 阶段一 0..100 → 阶段二 → 阶段三 → 构图/向量化/落盘 → 完成
    seq = [
        _overall(None, 10),                # 发现结构
        _overall("annotate", 0), _overall("annotate", 33), _overall("annotate", 100),
        _overall("tags", 0), _overall("tags", 50), _overall("tags", 100),
        _overall("graph", 0), _overall("graph", 100),
        _overall(None, 80), _overall(None, 95), _overall(None, 98), 100,
    ]
    assert seq == sorted(seq), f"overall 应单调不降：{seq}"


def test_new_progress_has_steps_metadata_and_substep_fields():
    p = _new_progress()
    assert len(p["phases"]) == 3
    keys = [ph["key"] for ph in p["phases"]]
    assert keys == ["annotate", "tags", "graph"]
    # 子步静态元数据进帧（前端可直接渲染，无需前端硬编码）
    tags = next(ph for ph in p["phases"] if ph["key"] == "tags")
    assert tags["steps"] == [
        {"key": "partition", "label": "领域划分"},
        {"key": "selfcheck", "label": "审校自检"},
    ]
    # 动态子步字段初始为空
    for ph in p["phases"]:
        assert ph["step"] is None and ph["step_label"] is None
        assert ph["step_index"] is None and ph["step_total"] is None


def test_step_label_resolution():
    assert _step_label("tags", "partition") == "领域划分"
    assert _step_label("graph", "verify") == "候选裁决"
    assert _step_label("annotate", "per_table") == "逐表注释"
    assert _step_label("tags", "nope") == "nope"  # 未知子步 → 原样返回


async def test_build_events_frames_monotonic_with_substeps(client, app_state, conn_id):
    """SSE 帧流：全局 percent 单调不降；阶段条带子步；结束 done 无 error。"""
    r = await client.post(
        f"/api/v1/knowledge/{conn_id}/build",
        json={"include_samples": False, "trigger": "init"},
    )
    assert r.status_code == 200
    frames: list[dict] = []
    async with client.stream("GET", f"/api/v1/knowledge/{conn_id}/build/events") as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                frames.append(json.loads(line[6:]))
            if frames and frames[-1].get("done"):
                break
    assert frames, "应收到至少一个进度帧"
    percents = [f["percent"] for f in frames]
    assert percents == sorted(percents), f"全局 percent 应单调不降：{percents}"
    assert frames[-1]["done"] and not frames[-1].get("error")
    # 三阶段条都出现过，且至少某个中间帧带子步标注
    phase_keys = {ph["key"] for f in frames for ph in f.get("phases", [])}
    assert phase_keys == {"annotate", "tags", "graph"}
    step_annotations = [
        ph for f in frames for ph in f.get("phases", [])
        if ph.get("step") and ph.get("step_label")
    ]
    assert step_annotations, "阶段条应携带子步标注（step/step_label）"