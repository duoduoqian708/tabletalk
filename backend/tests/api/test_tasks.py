"""任务 API（全脚本化）测试：deploy / 列表 / 启停 / 编辑脚本 / 运行 / 删除 / cron 预览。"""
from __future__ import annotations


async def _deploy(client, name="hello_job", cron="0 9 * * *", script=None):
    script = script or '# name: {0}\n# cron: {1}\nprint("TT-SUMMARY: 完成")\n'.format(name, cron)
    return await client.post("/api/v1/tasks/deploy", json={"name": name, "cron": cron, "script": script})


async def test_list_includes_seeded_system_task(client):
    r = await client.get("/api/v1/tasks")
    assert r.status_code == 200
    body = r.json()
    names = {t["name"] for t in body["tasks"]}
    assert "日志保留清理" in names
    sys_t = next(t for t in body["tasks"] if t["name"] == "日志保留清理")
    assert sys_t["system"] is True and "next_run" in sys_t and sys_t["friendly"]


async def test_deploy_registers_script_with_header(client):
    # 未带声明头的脚本 → deploy 自动补 name/cron 头，注册成功
    r = await _deploy(client, name="auto_head", cron="0 9 * * *", script='print("hello")\n')
    assert r.status_code == 200
    task = r.json()["task"]
    assert task["name"] == "auto_head" and task["cron"] == "0 9 * * *" and task["enabled"] is True
    # 文件头已写入
    f = next(p for p in (await client.get("/api/v1/tasks")).json()["tasks"] if p["name"] == "auto_head")
    assert f["next_run"] is not None


async def test_deploy_conflict_and_overwrite(client):
    await _deploy(client, name="dup_job", cron="0 9 * * *")
    c = await client.post("/api/v1/tasks/deploy", json={"name": "dup_job", "cron": "0 8 * * *"})
    assert c.status_code == 409
    # overwrite 后 cron 更新
    o = await client.post("/api/v1/tasks/deploy", json={
        "name": "dup_job", "cron": "0 8 * * *",
        "script": '# name: dup_job\n# cron: 0 8 * * *\nprint("x")',
        "overwrite": True,
    })
    assert o.status_code == 200 and o.json()["task"]["cron"] == "0 8 * * *"


async def test_patch_enable_and_run_now(client):
    await _deploy(client, name="rr_job", cron="0 9 * * *")
    # 停用 → 头重写
    p = await client.put("/api/v1/tasks/rr_job", json={"enabled": False})
    assert p.status_code == 200 and p.json()["task"]["enabled"] is False
    # 跑一次 → runs 记录
    run = await client.post("/api/v1/tasks/rr_job/run")
    assert run.status_code == 200 and run.json()["ok"] is True and run.json()["status"] == "success"
    runs = await client.get("/api/v1/tasks/rr_job/runs")
    assert runs.status_code == 200
    assert runs.json()["runs"][0]["status"] == "success"
    assert runs.json()["runs"][0]["summary"] == "完成"


async def test_save_script_and_delete(client):
    await _deploy(client, name="edit_job", cron="0 9 * * *")
    s = await client.put("/api/v1/tasks/edit_job/script", json={
        "script": '# name: edit_job\n# cron: 0 9 * * *\nprint("TT-SUMMARY: 变了")',
    })
    assert s.status_code == 200
    run = await client.post("/api/v1/tasks/edit_job/run")
    assert run.json()["summary"] == "变了"
    # 删除 → 列表消失
    d = await client.delete("/api/v1/tasks/edit_job")
    assert d.status_code == 200
    names = {t["name"] for t in (await client.get("/api/v1/tasks")).json()["tasks"]}
    assert "edit_job" not in names


async def test_cron_preview(client):
    ok = await client.post("/api/v1/tasks/cron/preview", json={"cron": "0 9 * * *"})
    assert ok.status_code == 200 and ok.json()["friendly"] == "每天 09:00" and ok.json()["next_run"]
    bad = await client.post("/api/v1/tasks/cron/preview", json={"cron": "nope"})
    assert bad.status_code == 400


async def test_run_missing_task_404(client):
    r = await client.post("/api/v1/tasks/不存在/run")
    assert r.status_code == 404


async def test_get_script_returns_source(client):
    await _deploy(client, name="src_job", cron="0 9 * * *", script='# name: src_job\n# cron: 0 9 * * *\nprint("x")')
    r = await client.get("/api/v1/tasks/src_job/script")
    assert r.status_code == 200
    assert "# name: src_job" in r.json()["script"] and r.json()["cron"] == "0 9 * * *"


async def test_agent_mock_proposal(client):
    # 默认 mock provider：离线直接给提案（mock 走 SSE；读 done 事件验证）
    r = await client.post("/api/v1/tasks/agent", json={
        "messages": [{"role": "user", "content": "每天早上给我一张订单日报"}],
    })
    assert r.status_code == 200
    body = ""
    for chunk in r.iter_text():
        body += chunk
    done = [line for line in body.split("\n") if line.startswith("data: ") and "done" in line[6:]]
    assert done, f"没有 done 事件: {body[:300]}"
    import json
    evt = json.loads(done[0][6:])
    assert evt.get("needs") == "proposal"
    assert evt.get("proposal") and "# name: 示例日报" in (evt["proposal"].get("script") or "")
    assert evt["proposal"]["cron"] == "0 9 * * *"


def test_parse_proposal():
    from app.api.tasks import _parse_proposal

    text = "好的，给你脚本。\n---PROPOSAL---\n```json\n{\"script\": \"print(1)\", \"name\": \"x\", \"cron\": \"0 9 * * *\", \"summary\": \"s\"}\n```\n"
    p = _parse_proposal(text)
    assert p and p["script"] == "print(1)" and p["name"] == "x" and p["next_run"]

    # 纯澄清回复 → 无提案
    assert _parse_proposal("还需要确认：目标表是哪个？") is None
    # 容错：无 ``` 也能解析
    p2 = _parse_proposal('---PROPOSAL---\n{"script": "y", "cron": "bad cron"}')
    assert p2 and p2["script"] == "y" and p2["next_run"] is None