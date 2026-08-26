# KB 逐表标注最小验证 demo v2

- 日期: 2026-08-25
- 连接: northwind-demo (`b9ee0e703c74`), dialect=sqlite, mode=REAL
- 表: `shipments`  (5 列)
- provider: deepseek-v4-flash @ https://ark.cn-beijing.volces.com/api/plan/v3
- kb_sample_rows: 10

## ① 新画像（DDL 结构 + description + 取值示例，common→description）

**有采样（取数据）版本：**

```sql
CREATE TABLE shipments (
  id                     INTEGER       PRIMARY KEY  取值示例：3,
  order_id               INTEGER       [FK->orders.id],
  tracking               TEXT          取值示例：SF100035，SF100018，SF100007,
  shipped_at             TIMESTAMP     取值示例：2026-07-01 11:03,
  carrier                TEXT          取值示例：圆通，京东物流,
  CONSTRAINT fk_shipments_order_id FOREIGN KEY (order_id) REFERENCES orders(id)
);
```

**无采样（不取数据）版本：**

```sql
CREATE TABLE shipments (
  id                     INTEGER       PRIMARY KEY,
  order_id               INTEGER       [FK->orders.id],
  tracking               TEXT,
  shipped_at             TIMESTAMP,
  carrier                TEXT,
  CONSTRAINT fk_shipments_order_id FOREIGN KEY (order_id) REFERENCES orders(id)
);
```


## ② 有采样标注

**发送 prompt（逐字）：**
```text
你是数据库语义分析专家。请为下表每一列生成准确的中文业务注释（一句话说清业务用途，不要复述列名或类型）。
【建表 DDL】
CREATE TABLE shipments (
  id INTEGER PRIMARY KEY,
  order_id INTEGER,
  tracking TEXT,
  shipped_at TIMESTAMP,
  carrier TEXT,
  CONSTRAINT fk_shipments_order_id FOREIGN KEY (order_id) REFERENCES orders (id)
);

【样本取值（真实数据；用于辅助理解字段含义）】
- id: [3, 2, 1]
- order_id: [35, 18, 7]
- tracking: ['SF100035', 'SF100018', 'SF100007']
- shipped_at: ['2026-07-01 11:03', '2026-05-25 11:55', '2026-02-18 09:05']
- carrier: ['圆通', '京东物流', '京东物流']
【准则】
1. DDL 中的 COMMENT 注释可能过时或不准确，仅供参考，以你的独立复核为准。
2. 每列产出一条注释。有样本值时：若该列值形态可辨识（编号/代码/标识/枚举/状态等，如订单编号形如 o_123），请在注释中附真实取值示例（如「订单编号，示例 o_123」），便于今后识别同类值；示例一律取自样本，不要编造。
3. 低基数离散取值（状态/类型/标志位等）另附加 values=「代码=含义」分号分隔（如 P=待付款；S=已发货）。
4. 额外为整张表写一条表级描述。
【输出】
返回 JSON 数组，字段级 {"table":"表名","column":"列名","comment":"一句话（可含取值示例）","values":"可选"}，表级 {"table":"表名","comment":"整表定位一句话"}。
只返回 JSON，不要多余文字。
```

**返回 content：**

```json
[
  {"table":"shipments","column":"id","comment":"发货记录唯一标识，示例 3"},
  {"table":"shipments","column":"order_id","comment":"关联的订单ID，示例 35"},
  {"table":"shipments","column":"tracking","comment":"物流追踪单号，示例 SF100035"},
  {"table":"shipments","column":"shipped_at","comment":"发货时间，示例 2026-07-01 11:03"},
  {"table":"shipments","column":"carrier","comment":"物流承运商名称，示例 圆通","values":"圆通=圆通速递；京东物流=京东物流"},
  {"table":"shipments","comment":"发货表，记录每个订单的发货物流信息（物流单号、承运商、发货时间）"}
]
```

**usage:** `{"completion_tokens": 967, "prompt_tokens": 498, "total_tokens": 1465, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 793}}`


## ③ 无采样标注

**发送 prompt（逐字）：**
```text
你是数据库语义分析专家。请为下表每一列生成准确的中文业务注释（一句话说清业务用途，不要复述列名或类型）。
【建表 DDL】
CREATE TABLE shipments (
  id INTEGER PRIMARY KEY,
  order_id INTEGER,
  tracking TEXT,
  shipped_at TIMESTAMP,
  carrier TEXT,
  CONSTRAINT fk_shipments_order_id FOREIGN KEY (order_id) REFERENCES orders (id)
);

【准则】
1. DDL 中的 COMMENT 注释可能过时或不准确，仅供参考，以你的独立复核为准。
2. 每列产出一条注释；对低基数离散字段（状态/标志位等）仅当能从字段名/类型/注释可靠推断时才简述取值含义，否则保守描述，不编造具体取值。
3. 额外为整张表写一条表级描述。
【输出】
返回 JSON 数组，字段级 {"table":"表名","column":"列名","comment":"一句话"}，表级 {"table":"表名","comment":"整表定位一句话"}。
只返回 JSON，不要多余文字。
```

**返回 content：**

```json
[
  {"table":"shipments","column":"id","comment":"发货记录的唯一主键标识"},
  {"table":"shipments","column":"order_id","comment":"关联订单表的外键，标识本次发货对应的订单"},
  {"table":"shipments","column":"tracking","comment":"物流追踪单号，用于查询包裹运输状态"},
  {"table":"shipments","column":"shipped_at","comment":"订单实际发货的时间戳"},
  {"table":"shipments","column":"carrier","comment":"负责运输该包裹的物流承运商"},
  {"table":"shipments","comment":"记录每个订单的发货信息，包括物流单号、发货时间和承运商"}
]
```

**usage:** `{"completion_tokens": 247, "prompt_tokens": 308, "total_tokens": 555, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 103}}`
