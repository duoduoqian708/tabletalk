"""生成 northwind 风格的演示 SQLite 库（无服务器也能跑通全流程）。

用法：python scripts/seed_demo_db.py [输出路径]
默认输出到 data_dir/demo.db。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_env  # noqa: E402

SCHEMA = """
CREATE TABLE customers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT,
  region TEXT
);
CREATE TABLE addresses (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER REFERENCES customers(id),
  city TEXT,
  zip TEXT
);
CREATE TABLE campaigns (
  id INTEGER PRIMARY KEY,
  name TEXT,
  channel TEXT,
  started_at DATE
);
CREATE TABLE categories (
  id INTEGER PRIMARY KEY,
  name TEXT,
  parent_id INTEGER REFERENCES categories(id)
);
CREATE TABLE suppliers (
  id INTEGER PRIMARY KEY,
  name TEXT,
  region TEXT
);
CREATE TABLE products (
  id INTEGER PRIMARY KEY,
  product_name TEXT NOT NULL,
  category_id INTEGER REFERENCES categories(id),
  supplier_id INTEGER REFERENCES suppliers(id),
  price NUMERIC(10,2),
  stock INTEGER
);
CREATE TABLE orders (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER REFERENCES customers(id),
  address_id INTEGER REFERENCES addresses(id),
  campaign_id INTEGER REFERENCES campaigns(id),
  status TEXT,
  created_at TIMESTAMP,
  total_amount NUMERIC(10,2)
);
CREATE TABLE order_items (
  id INTEGER PRIMARY KEY,
  order_id INTEGER REFERENCES orders(id),
  product_id INTEGER REFERENCES products(id),
  quantity INTEGER,
  unit_price NUMERIC(10,2)
);
CREATE TABLE returns (
  id INTEGER PRIMARY KEY,
  order_item_id INTEGER REFERENCES order_items(id),
  returned_at TIMESTAMP,
  reason TEXT
);
CREATE TABLE payments (
  id INTEGER PRIMARY KEY,
  order_id INTEGER REFERENCES orders(id),
  method TEXT,
  amount NUMERIC(10,2),
  paid_at TIMESTAMP
);
CREATE TABLE shipments (
  id INTEGER PRIMARY KEY,
  order_id INTEGER REFERENCES orders(id),
  tracking TEXT,
  shipped_at TIMESTAMP,
  carrier TEXT
);
CREATE TABLE inventory (
  id INTEGER PRIMARY KEY,
  product_id INTEGER REFERENCES products(id),
  qty INTEGER,
  last_moved_at TIMESTAMP
);
CREATE TABLE reviews (
  id INTEGER PRIMARY KEY,
  product_id INTEGER REFERENCES products(id),
  rating INTEGER,
  comment TEXT
);
CREATE TABLE order_status_history (
  id INTEGER PRIMARY KEY,
  order_id INTEGER REFERENCES orders(id),
  status TEXT,
  changed_at TIMESTAMP
);
CREATE INDEX idx_orders_created_at ON orders(created_at);
CREATE INDEX idx_order_items_product ON order_items(product_id);
CREATE TABLE big_values (
  id INTEGER PRIMARY KEY,
  label TEXT,
  obj_json TEXT,
  arr_json TEXT,
  long_text TEXT,
  big_int INTEGER,
  precise_amt NUMERIC(30,6)
);
"""


def build_demo_db(path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    cur = conn.cursor()

    customers = [(i, n, f"{n}@example.com", r) for i, (n, r) in enumerate([
        ("陈嘉禾", "华东"), ("林可欣", "华南"), ("王振宇", "华北"), ("赵晓彤", "西南"),
        ("刘安琪", "华东"), ("周子墨", "华中"), ("孙晨曦", "东北"), ("吴雨桐", "华南"),
        ("郑一帆", "华北"), ("黄诗琪", "西南"),
    ], start=1)]
    cur.executemany("INSERT INTO customers VALUES (?,?,?,?)", customers)

    addresses = [
        (1, 1, "上海", "200040"), (2, 2, "广州", "510600"), (3, 3, "北京", "100020"),
        (4, 4, "成都", "610041"), (5, 5, "杭州", "310000"), (6, 6, "武汉", "430070"),
        (7, 7, "沈阳", "110001"), (8, 8, "深圳", "518000"), (9, 9, "天津", "300100"),
        (10, 10, "重庆", "400010"),
    ]
    cur.executemany("INSERT INTO addresses VALUES (?,?,?,?)", addresses)

    campaigns = [
        (1, "夏季大促", "email", "2026-06-01"), (2, "新品首发", "social", "2026-03-15"),
        (3, "会员日", "sms", "2026-05-20"), (4, "清仓特卖", "email", "2026-07-10"),
        (5, "返校季", "affiliate", "2026-08-01"),
    ]
    cur.executemany("INSERT INTO campaigns VALUES (?,?,?,?)", campaigns)

    categories = [(1, "外设", None), (2, "办公", 1), (3, "摄影", None), (4, "箱包", None),
                  (5, "配件", 1), (6, "智能家居", None)]
    cur.executemany("INSERT INTO categories VALUES (?,?,?)", categories)
    suppliers = [(1, "华东供应链", "华东"), (2, "华南制造", "华南"), (3, "华北器材", "华北")]
    cur.executemany("INSERT INTO suppliers VALUES (?,?,?)", suppliers)

    products = [
        (1, "Nimbus 500 无线键盘", 2, 1, 499.00, 240),
        (2, "Orbit 人体工学椅", 2, 3, 1299.00, 80),
        (3, "Meridian 27 显示器", 2, 3, 1899.00, 120),
        (4, "Pulse 降噪耳机", 1, 2, 899.00, 300),
        (5, "Aperture 4K 摄像头", 3, 2, 699.00, 150),
        (6, "Vertex 机械键盘", 1, 1, 599.00, 260),
        (7, "Relay Mesh 路由器", 6, 1, 799.00, 90),
        (8, "Flux 65W 氮化镓充电器", 5, 2, 129.00, 400),
        (9, "Cascade 桌面音箱", 1, 3, 459.00, 110),
        (10, "Drift 电竞鼠标", 1, 1, 329.00, 380),
        (11, "Echo 无线充电板", 5, 2, 149.00, 500),
        (12, "Torque 电动升降桌", 2, 3, 2499.00, 60),
        (13, "Halo 台灯", 6, 1, 199.00, 280),
        (14, "Slate 电容笔", 5, 2, 249.00, 320),
        (15, "Anchor USB-C 扩展坞", 5, 1, 349.00, 130),
        (16, "Latitude 旅行背包", 4, 2, 549.00, 410),
        (17, "Summit 三脚架", 3, 3, 399.00, 70),
        (18, "Ember 恒温杯", 6, 1, 259.00, 190),
        (19, "Parallax 显示器支架", 2, 3, 299.00, 210),
        (20, "Grid 机械式键盘托", 2, 1, 179.00, 230),
    ]
    cur.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", products)

    statuses = ["pending", "paid", "shipped", "cancelled"]
    import random

    rng = random.Random(42)
    orders = []
    for oid in range(1, 41):
        cid = rng.randint(1, len(customers))
        created = f"2026-0{rng.randint(1, 8)}-{rng.randint(1, 28):02d} {rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}"
        camp = rng.randint(1, len(campaigns))
        orders.append((oid, cid, cid, camp, rng.choice(statuses), created, round(rng.uniform(20, 500), 2)))
    cur.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", orders)

    items = []
    iid = 1
    for oid in range(1, 41):
        for _ in range(rng.randint(1, 4)):
            pid = rng.randint(1, len(products))
            qty = rng.randint(1, 5)
            price = products[pid - 1][4]
            items.append((iid, oid, pid, qty, price))
            iid += 1
    cur.executemany("INSERT INTO order_items VALUES (?,?,?,?,?)", items)

    # 付款：paid/shipped 的订单都有支付记录
    payments = []
    pid_row = 1
    for oid, cid, aid, camp, st, created, total in orders:
        if st in ("paid", "shipped"):
            h = rng.randint(9, 23)
            payments.append((pid_row, oid, rng.choice(["alipay", "wechat", "card", "bank"]),
                             round(total * rng.uniform(0.9, 1.0), 2),
                             f"{created[:10]} {h:02d}:{rng.randint(0, 59):02d}"))
            pid_row += 1
    cur.executemany("INSERT INTO payments VALUES (?,?,?,?,?)", payments)

    # 发货：shipped 的订单都有发货记录
    shipments = []
    sid = 1
    for oid, cid, aid, camp, st, created, total in orders:
        if st == "shipped":
            shipments.append((sid, oid, f"SF{100000 + oid}",
                              f"{created[:10]} {rng.randint(9, 23):02d}:{rng.randint(0, 59):02d}",
                              rng.choice(["顺丰", "中通", "圆通", "京东物流"])))
            sid += 1
    cur.executemany("INSERT INTO shipments VALUES (?,?,?,?,?)", shipments)

    # 状态历史：每单至少 pending，已推进的补 paid/shipped 记录
    hist = []
    hid = 1
    for oid, cid, aid, camp, st, created, total in orders:
        hist.append((hid, oid, "pending", created))
        hid += 1
        if st in ("paid", "shipped"):
            hist.append((hid, oid, "paid",
                         f"{created[:10]} {rng.randint(9, 23):02d}:{rng.randint(0, 59):02d}"))
            hid += 1
        if st == "shipped":
            hist.append((hid, oid, "shipped",
                         f"{created[:10]} {rng.randint(9, 23):02d}:{rng.randint(0, 59):02d}"))
            hid += 1
    cur.executemany("INSERT INTO order_status_history VALUES (?,?,?,?)", hist)

    returns_rows = []
    rid = 1
    for it in items:
        if rng.random() < 0.12:
            returns_rows.append((rid, it[0], f"2026-07-{rng.randint(1, 28):02d}", rng.choice(
                ["quality", "wrong_item", "changed_mind", "damaged"])))
            rid += 1
    cur.executemany("INSERT INTO returns VALUES (?,?,?,?)", returns_rows)

    # 商品评价：部分商品有评分
    reviews = []
    rvid = 1
    for pid in range(1, len(products) + 1):
        for _ in range(rng.randint(0, 4)):
            reviews.append((rvid, pid, rng.randint(1, 5), rng.choice(
                ["很好用", "性价比高", "一般般", "做工不错", "物流很快", "颜色好看"])))
            rvid += 1
    cur.executemany("INSERT INTO reviews VALUES (?,?,?,?)", reviews)

    for pid in range(1, len(products) + 1):
        cur.execute("INSERT INTO inventory VALUES (?,?,?,?)", (
            pid, pid, rng.randint(0, 500), f"2026-07-{rng.randint(1, 28):02d}"))

    # 超大字段 mock（验证前端截断预览 / JSON 格式化 / 大数精度）
    long_text = (
        "这是一段超长的产品备注文本，用于验证前端单元格的收敛与预览功能。"
        "在真实业务中，此类字段常见于日志详情、用户反馈、快递备注、合同条款等场景。"
        "字段可能包含多行内容、特殊字符、URL 链接（https://example.com/docs/2026/spec?v=2&lang=zh）、"
        "以及很长的数字序列 123456789012345678901234567890 等。"
        "用户需要在不展开整行的情况下，快速查看完整内容并复制。"
    )
    big_values = [
        (1, "对象 JSON", '{"order_id": 9007199254740993, "user": {"name": "陈嘉禾", "level": 3}, "items": ["A-1", "B-2"], "note": "含超长 id 的对象"}', None, None, 9007199254740993, 999999999.123456),
        (2, "数组 JSON", None, '["华东", "华南", "华北", {"region": "西南", "count": 42}, [1, 2, 3]]', None, 9007199254740995, 888888888.654321),
        (3, "超长文本", None, None, long_text, 123456, 1234.567890),
        (4, "超大整数", None, None, None, 9223372036854775807, 0.000001),
        (5, "组合：JSON+大数", '{"ts": 1752600000, "payload": [{"k": "v1"}, {"k": "v2"}]}', None, "简短备注", 9007199254740997, 7654321.098765),
        (6, "非标准JSON", '{"a": 1, "b": 2, "note": "这是一段故意写错的 JSON 文本，缺少必要的逗号与闭合结构，用于验证格式化功能对非法 JSON 的容错提示", }', None, "这是伪装成 JSON 的非法文本，格式化时应提示非 JSON", 42, 10.5),
        (7, "长JSON数组", None, '[{"id": 1, "name": "Nimbus 500"}, {"id": 2, "name": "Orbit 椅"}, {"id": 3, "name": "Meridian 27"}, {"id": 4, "name": "Pulse 耳机"}, {"id": 5, "name": "Aperture 摄像头"}]', None, 7, 77.77),
    ]
    cur.executemany("INSERT INTO big_values VALUES (?,?,?,?,?,?,?)", big_values)
    conn.commit()
    conn.close()
    return path


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else get_env().data_dir / "demo.db"
    p = build_demo_db(out)
    print(f"demo db written: {p}")
