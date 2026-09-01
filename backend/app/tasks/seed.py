"""播种 data_dir/jobs/：SDK lib.py + 系统保留脚本。幂等（缺失才写）。

调度器扫描时跳过无 `# name:`+`# cron:` 声明的文件——lib.py 是辅助模块不是任务。
保留时长 = 改系统脚本顶部 RETENTION 常量（或让 AI 会话帮忙改）。
"""
from __future__ import annotations

from pathlib import Path

LIB_PY = '''"""TableTalk 定时任务 SDK —— 脚本经此访问平台（闸门 + 审计）。

用法:
    from lib import query, insert, write, retain_logs, log, summary
    rows = query("SELECT region, SUM(amount) AS gmv FROM orders GROUP BY region")
    insert("order_daily", ["region", "gmv"], rows)   # 汇总写另一表（scheduled INSERT 放行）
    log("processed %d rows" % len(rows))
    summary("写入 order_daily %d 行" % len(rows))

环境由调度器注入: TABLETALK_URL / TABLETALK_TOKEN / TABLETALK_JOB / TABLETALK_JOB_CONNECTION
脚本 SQL 一律走后端闸门并被审计；被拦时抛 RuntimeError（含原因）。
"""
import json
import os
import urllib.error
import urllib.request

_URL = os.environ.get("TABLETALK_URL", "http://127.0.0.1:8777")
_TOKEN = os.environ.get("TABLETALK_TOKEN", "")
_JOB = os.environ.get("TABLETALK_JOB", "job")
_CONNECTION = os.environ.get("TABLETALK_JOB_CONNECTION", "")

_conn_id = None


def _request(path, payload=None, method="POST", _timeout=90):
    url = _URL.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if _TOKEN:
        req.add_header("X-TableTalk-Token", _TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=_timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return e.code, raw
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"TableTalk 平台调用失败: {e}") from e


def _resolve_conn():
    global _conn_id
    if _conn_id:
        return _conn_id
    status, raw = _request("/api/v1/connections", None, "GET", _timeout=30)
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        data = []
    items = data if isinstance(data, list) else data.get("connections") or data.get("items") or []
    name = _CONNECTION or ""
    for c in items:
        if c.get("id"):
            if not name or c.get("name") == name:
                _conn_id = c["id"]
                break
    if not _conn_id and items:
        _conn_id = items[0]["id"]
    if not _conn_id:
        raise RuntimeError(f"未找到连接: {name or '(默认)'}，请检查脚本头 # connection:")
    return _conn_id


def _query(sql):
    conn = _resolve_conn()
    status, raw = _request("/api/v1/query", {"connection_id": conn, "sql": sql, "origin": "scheduled"})
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        raise RuntimeError(f"查询失败({status}): {raw[:200]}")
    verdict = data.get("verdict")
    if verdict != "allow" and verdict != "executed":
        msg = data.get("reason") or (data.get("detail") or {}).get("message") if isinstance(data.get("detail"), dict) else data.get("detail")
        raise RuntimeError(f"被闸门拦截 [{data.get('tier')}]: {msg or verdict}")
    return data


def query(sql):
    """只读/统计查询，返回行列表（每行 dict 由列名键）。"""
    data = _query(sql)
    cols = data.get("columns") or []
    rows = data.get("rows") or []
    return [dict(zip(cols, r)) if r is not None else {} for r in rows]


def write(sql):
    """执行 INSERT（scheduled 身份放行）。其余 DML/DDL 被闸门拒（抛 RuntimeError）。"""
    _query(sql)


def _sql_val(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def insert(table, columns, rows, batch=500):
    """批量生成 INSERT 并执行（自动切批次）。rows 为 [{col:val},...] 或 [[val,...],...]。
    幂等提醒：由脚本自行保证（如按天先删/覆盖），平台不隐式去重。"""
    columns = list(columns)
    if rows and isinstance(rows[0], dict):
        rows = [[r.get(c) for c in columns] for r in rows]
    chunks = [rows[i:i + batch] for i in range(0, len(rows), batch)]
    total = 0
    for chunk in chunks:
        if not chunk:
            continue
        values = ", ".join("(" + ", ".join(_sql_val(v) for v in row) + ")" for row in chunk)
        write(f"INSERT INTO {table} ({', '.join(columns)}) VALUES {values}")
        total += len(chunk)
    return total


def retain_logs(kinds):
    """按天清理审计/聊天/成本日志（平台拥有删除权并审计）。kinds: {audit:int, chat:int, cost:int}"""
    status, raw = _request("/api/v1/system/retain", kinds)
    try:
        return json.loads(raw or "{}")
    except ValueError:
        raise RuntimeError(f"保留清理失败({status}): {raw[:200]}")


def log(msg):
    print(f"TT-LOG: {msg}", flush=True)


def summary(msg):
    print(f"TT-SUMMARY: {msg}", flush=True)


def report(sql, title="report"):
    """查询数据 + 格式化为 Markdown 报表 + 落盘（data_dir/reports/）。"""
    from datetime import datetime
    from pathlib import Path

    rows = query(sql)
    cols = list(rows[0].keys()) if rows else []
    lines = [f"# {title}", "", f"生成时间：{datetime.now().isoformat()}", ""]
    if cols:
        lines.append("| " + " | ".join(str(c) for c in cols) + " |")
        lines.append("| " + " | ".join(["---"] * len(cols)) + " |")
        for r in rows:
            lines.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    else:
        lines.append("（无数据）")
    report_text = "\n".join(lines)
    reports_dir = Path(os.environ.get("TABLETALK_DATA_DIR", ".")) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{title}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    (reports_dir / fname).write_text(report_text, encoding="utf-8")
    summary(f"报表已生成：reports/{fname}，共 {len(rows)} 行")
    return rows


if __name__ == "__main__":
    log(f"SDK self-test for job {_JOB} (connection={_CONNECTION!r})")
    summary("SDK OK")
'''

RETENTION_SCRIPT = '''# name: 日志保留清理
# cron: 0 3 * * *
# enabled: true
# system: true
# 平台内置：审计 / 聊天 / 成本日志保留清理。改下面天数即可调整保留时长。
from lib import retain_logs, summary

RETENTION = {"audit": 90, "chat": 90, "cost": 90}

if __name__ == "__main__":
    result = retain_logs(RETENTION)
    deleted = result.get("deleted") or {}
    parts = [f"{k} 清理 {n} 行" for k, n in deleted.items()]
    summary("日志保留清理: " + ("；".join(parts) if parts else "无过期日志"))
'''


def seed_jobs_dir(data_dir) -> Path:
    jobs = Path(data_dir) / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    lib = jobs / "lib.py"
    if not lib.exists():
        lib.write_text(LIB_PY, encoding="utf-8")
    ret = jobs / "_system_log_retention.py"
    if not ret.exists():
        ret.write_text(RETENTION_SCRIPT, encoding="utf-8")
    return jobs