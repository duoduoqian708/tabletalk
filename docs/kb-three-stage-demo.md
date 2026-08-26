# KB 构建三阶段示例测试（新画像 + 无 FK）

- 日期: 2026-08-25
- 连接: northwind-demo (`b9ee0e703c74`), dialect=sqlite, mode=REAL
- 表集: orders, order_items, products, customers, returns
- model: deepseek-v4-flash
- FK: 已清空（模拟无外键库，上下文不写 FK）
- kb_sample_rows: 10（授权 → 描述含取值示例）


## 阶段一 · 逐表注释


### orders · 有采样（取数据）

**入参（完整 prompt）：**
```text
你是数据库语义分析专家。请根据建表 DDL 和样本取值，为每列生成准确的中文业务语义注释。
注释应简洁（一句话），说明该字段的业务用途，不要复述列名或类型。
若样本显示某列是低基数离散取值（如状态/类型/标志位），请在该列的 values 字段给出取值对照，格式为「代码=含义」并用分号分隔（如 P=待付款；S=已发货）；主键、金额、时间等高基数列省略 values。

【样本取值】
- id: [40, 39, 38, 37, 36, 35, 34, 33, 32, 31]
- customer_id: [7, 4, 9, 9, 9, 3, 4, 9, 3, 6]
- address_id: [7, 4, 9, 9, 9, 3, 4, 9, 3, 6]
- campaign_id: [3, 5, 2, 1, 2, 4, 2, 1, 1, 5]
- status: ['paid', 'pending', 'pending', 'cancelled', 'pending', 'shipped', 'cancelled', 'pending', 'cancelled', 'cancelled']
- created_at: ['2026-06-09 06:42', '2026-07-04 18:15', '2026-03-02 16:05', '2026-01-24 10:03', '2026-08-05 06:18', '2026-07-01 12:16', '2026-03-14 15:30', '2026-02-02 20:34', '2026-05-15 07:55', '2026-02-08 06:12']
- total_amount: [147.49, 317.3, 305.62, 261.37, 298.01, 223.04, 453.17, 464.66, 407.85, 87.29]
【建表 DDL】
CREATE TABLE orders (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER,
  address_id INTEGER,
  campaign_id INTEGER,
  status TEXT,
  created_at TIMESTAMP,
  total_amount NUMERIC(10,2)
);

请为每列生成注释；并额外为整张表生成一条表级描述（column 省略）。返回 JSON 数组，元素形如 {"table": "表名", "column": "列名", "comment": "一句话中文注释", "values": "可选。代码=含义，分号分隔"}，表级形如 {"table": "表名", "comment": "整表业务定位一句话"}。
只返回 JSON，不要多余文字。
```

**返回：**
```json
[
  {"table": "orders", "comment": "订单主表，记录客户下单及订单状态信息"},
  {"table": "orders", "column": "id", "comment": "订单唯一标识，主键"},
  {"table": "orders", "column": "customer_id", "comment": "下单客户标识，关联客户表"},
  {"table": "orders", "column": "address_id", "comment": "收货地址标识，关联地址表"},
  {"table": "orders", "column": "campaign_id", "comment": "营销活动标识，标识订单来源活动"},
  {"table": "orders", "column": "status", "comment": "订单处理状态", "values": "paid=已支付；pending=待处理；cancelled=已取消；shipped=已发货"},
  {"table": "orders", "column": "created_at", "comment": "订单创建时间"},
  {"table": "orders", "column": "total_amount", "comment": "订单应付总金额"}
]
```

**usage:** `{"completion_tokens": 318, "prompt_tokens": 704, "total_tokens": 1022, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 95}}`


### orders · 无采样（不取数据）

**入参（完整 prompt）：**
```text
你是数据库语义分析专家。请根据建表 DDL 为每列生成准确的中文业务语义注释。
注释应简洁（一句话），说明该字段的业务用途，不要复述列名或类型。
仅依据字段名、类型与约束等结构信息判断；对于状态/标志位等低基数离散字段，仅当能从字段名或注释可靠推断时再简述其取值含义，否则给出保守的业务描述，不要编造具体取值。

【建表 DDL】
CREATE TABLE orders (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER,
  address_id INTEGER,
  campaign_id INTEGER,
  status TEXT,
  created_at TIMESTAMP,
  total_amount NUMERIC(10,2)
);

请为每列生成注释；并额外为整张表生成一条表级描述（column 省略）。返回 JSON 数组，元素形如 {"table": "表名", "column": "列名", "comment": "一句话中文注释"}，表级形如 {"table": "表名", "comment": "整表业务定位一句话"}。
只返回 JSON，不要多余文字。
```

**返回：**
```json
[
  {"table": "orders", "comment": "订单业务事实表，记录客户订单及关联维度信息"},
  {"table": "orders", "column": "id", "comment": "订单唯一标识"},
  {"table": "orders", "column": "customer_id", "comment": "下单客户标识"},
  {"table": "orders", "column": "address_id", "comment": "订单配送地址标识"},
  {"table": "orders", "column": "campaign_id", "comment": "关联的营销活动标识"},
  {"table": "orders", "column": "status", "comment": "订单当前状态"},
  {"table": "orders", "column": "created_at", "comment": "订单创建时间"},
  {"table": "orders", "column": "total_amount", "comment": "订单应付总金额"}
]
```

**usage:** `{"completion_tokens": 424, "prompt_tokens": 315, "total_tokens": 739, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 240}}`


### order_items · 有采样（取数据）

**入参（完整 prompt）：**
```text
你是数据库语义分析专家。请根据建表 DDL 和样本取值，为每列生成准确的中文业务语义注释。
注释应简洁（一句话），说明该字段的业务用途，不要复述列名或类型。
若样本显示某列是低基数离散取值（如状态/类型/标志位），请在该列的 values 字段给出取值对照，格式为「代码=含义」并用分号分隔（如 P=待付款；S=已发货）；主键、金额、时间等高基数列省略 values。

【样本取值】
- id: [99, 98, 97, 96, 95, 94, 93, 92, 91, 90]
- order_id: [40, 40, 40, 39, 39, 38, 38, 38, 37, 37]
- product_id: [15, 8, 17, 9, 16, 18, 17, 15, 15, 15]
- quantity: [1, 3, 4, 2, 4, 4, 4, 5, 5, 2]
- unit_price: [349, 129, 399, 459, 549, 259, 399, 349, 349, 349]
【建表 DDL】
CREATE TABLE order_items (
  id INTEGER PRIMARY KEY,
  order_id INTEGER,
  product_id INTEGER,
  quantity INTEGER,
  unit_price NUMERIC(10,2)
);

请为每列生成注释；并额外为整张表生成一条表级描述（column 省略）。返回 JSON 数组，元素形如 {"table": "表名", "column": "列名", "comment": "一句话中文注释", "values": "可选。代码=含义，分号分隔"}，表级形如 {"table": "表名", "comment": "整表业务定位一句话"}。
只返回 JSON，不要多余文字。
```

**返回：**
```json
[
  {"table": "order_items", "comment": "订单明细表，记录每个订单中商品的购买数量与成交单价"},
  {"table": "order_items", "column": "id", "comment": "订单明细记录唯一标识"},
  {"table": "order_items", "column": "order_id", "comment": "所属订单的标识，关联订单主表"},
  {"table": "order_items", "column": "product_id", "comment": "所购商品的标识，关联商品主表"},
  {"table": "order_items", "column": "quantity", "comment": "该商品在订单中的购买数量"},
  {"table": "order_items", "column": "unit_price", "comment": "该商品在订单中的成交单价"}
]
```

**usage:** `{"completion_tokens": 346, "prompt_tokens": 507, "total_tokens": 853, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 181}}`


### order_items · 无采样（不取数据）

**入参（完整 prompt）：**
```text
你是数据库语义分析专家。请根据建表 DDL 为每列生成准确的中文业务语义注释。
注释应简洁（一句话），说明该字段的业务用途，不要复述列名或类型。
仅依据字段名、类型与约束等结构信息判断；对于状态/标志位等低基数离散字段，仅当能从字段名或注释可靠推断时再简述其取值含义，否则给出保守的业务描述，不要编造具体取值。

【建表 DDL】
CREATE TABLE order_items (
  id INTEGER PRIMARY KEY,
  order_id INTEGER,
  product_id INTEGER,
  quantity INTEGER,
  unit_price NUMERIC(10,2)
);

请为每列生成注释；并额外为整张表生成一条表级描述（column 省略）。返回 JSON 数组，元素形如 {"table": "表名", "column": "列名", "comment": "一句话中文注释"}，表级形如 {"table": "表名", "comment": "整表业务定位一句话"}。
只返回 JSON，不要多余文字。
```

**返回：**
```json
[
  {"table": "order_items", "comment": "订单明细表，记录每个订单包含的商品及购买信息"},
  {"table": "order_items", "column": "id", "comment": "订单明细记录的唯一标识"},
  {"table": "order_items", "column": "order_id", "comment": "所属订单的标识，关联订单主表"},
  {"table": "order_items", "column": "product_id", "comment": "所购商品的标识，关联商品主数据"},
  {"table": "order_items", "column": "quantity", "comment": "该商品在订单中的购买数量"},
  {"table": "order_items", "column": "unit_price", "comment": "商品在订单中的成交单价，保留两位小数"}
]
```

**usage:** `{"completion_tokens": 292, "prompt_tokens": 305, "total_tokens": 597, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 126}}`


### products · 有采样（取数据）

**入参（完整 prompt）：**
```text
你是数据库语义分析专家。请根据建表 DDL 和样本取值，为每列生成准确的中文业务语义注释。
注释应简洁（一句话），说明该字段的业务用途，不要复述列名或类型。
若样本显示某列是低基数离散取值（如状态/类型/标志位），请在该列的 values 字段给出取值对照，格式为「代码=含义」并用分号分隔（如 P=待付款；S=已发货）；主键、金额、时间等高基数列省略 values。

【样本取值】
- id: [20, 19, 18, 17, 16, 15, 14, 13, 12, 11]
- product_name: ['Grid 机械式键盘托', 'Parallax 显示器支架', 'Ember 恒温杯', 'Summit 三脚架', 'Latitude 旅行背包', 'Anchor USB-C 扩展坞', 'Slate 电容笔', 'Halo 台灯', 'Torque 电动升降桌', 'Echo 无线充电板']
- category_id: [2, 2, 6, 3, 4, 5, 5, 6, 2, 5]
- supplier_id: [1, 3, 1, 3, 2, 1, 2, 1, 3, 2]
- price: [179, 299, 259, 399, 549, 349, 249, 199, 2499, 149]
- stock: [230, 210, 190, 70, 410, 130, 320, 280, 60, 500]
【建表 DDL】
CREATE TABLE products (
  id INTEGER PRIMARY KEY,
  product_name TEXT,
  category_id INTEGER,
  supplier_id INTEGER,
  price NUMERIC(10,2),
  stock INTEGER
);

请为每列生成注释；并额外为整张表生成一条表级描述（column 省略）。返回 JSON 数组，元素形如 {"table": "表名", "column": "列名", "comment": "一句话中文注释", "values": "可选。代码=含义，分号分隔"}，表级形如 {"table": "表名", "comment": "整表业务定位一句话"}。
只返回 JSON，不要多余文字。
```

**返回：**
```json
[
  {"table": "products", "comment": "商品主数据表，存储商品的核心信息"},
  {"table": "products", "column": "id", "comment": "商品唯一标识"},
  {"table": "products", "column": "product_name", "comment": "商品展示名称"},
  {"table": "products", "column": "category_id", "comment": "商品所属分类", "values": "2=电脑外设; 3=摄影器材; 4=箱包; 5=数码配件; 6=家居生活"},
  {"table": "products", "column": "supplier_id", "comment": "商品供应商标识"},
  {"table": "products", "column": "price", "comment": "商品销售单价"},
  {"table": "products", "column": "stock", "comment": "当前库存数量"}
]
```

**usage:** `{"completion_tokens": 1619, "prompt_tokens": 591, "total_tokens": 2210, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 1431}}`


### products · 无采样（不取数据）

**入参（完整 prompt）：**
```text
你是数据库语义分析专家。请根据建表 DDL 为每列生成准确的中文业务语义注释。
注释应简洁（一句话），说明该字段的业务用途，不要复述列名或类型。
仅依据字段名、类型与约束等结构信息判断；对于状态/标志位等低基数离散字段，仅当能从字段名或注释可靠推断时再简述其取值含义，否则给出保守的业务描述，不要编造具体取值。

【建表 DDL】
CREATE TABLE products (
  id INTEGER PRIMARY KEY,
  product_name TEXT,
  category_id INTEGER,
  supplier_id INTEGER,
  price NUMERIC(10,2),
  stock INTEGER
);

请为每列生成注释；并额外为整张表生成一条表级描述（column 省略）。返回 JSON 数组，元素形如 {"table": "表名", "column": "列名", "comment": "一句话中文注释"}，表级形如 {"table": "表名", "comment": "整表业务定位一句话"}。
只返回 JSON，不要多余文字。
```

**返回：**
```json
[
  {"table": "products", "comment": "商品基础信息表，记录商品的核心属性与库存情况"},
  {"table": "products", "column": "id", "comment": "商品唯一标识"},
  {"table": "products", "column": "product_name", "comment": "商品名称"},
  {"table": "products", "column": "category_id", "comment": "商品所属分类的标识"},
  {"table": "products", "column": "supplier_id", "comment": "商品供应商的标识"},
  {"table": "products", "column": "price", "comment": "商品销售单价"},
  {"table": "products", "column": "stock", "comment": "商品当前库存数量"}
]
```

**usage:** `{"completion_tokens": 284, "prompt_tokens": 308, "total_tokens": 592, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 124}}`


### customers · 有采样（取数据）

**入参（完整 prompt）：**
```text
你是数据库语义分析专家。请根据建表 DDL 和样本取值，为每列生成准确的中文业务语义注释。
注释应简洁（一句话），说明该字段的业务用途，不要复述列名或类型。
若样本显示某列是低基数离散取值（如状态/类型/标志位），请在该列的 values 字段给出取值对照，格式为「代码=含义」并用分号分隔（如 P=待付款；S=已发货）；主键、金额、时间等高基数列省略 values。

【样本取值】
- id: [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
- name: ['黄诗琪', '郑一帆', '吴雨桐', '孙晨曦', '周子墨', '刘安琪', '赵晓彤', '王振宇', '林可欣', '陈嘉禾']
- email: ['黄诗琪@example.com', '郑一帆@example.com', '吴雨桐@example.com', '孙晨曦@example.com', '周子墨@example.com', '刘安琪@example.com', '赵晓彤@example.com', '王振宇@example.com', '林可欣@example.com', '陈嘉禾@example.com']
- phone: ['13800138110', '13800138109', '13800138108', '13800138107', '13800138106', '13800138105', '13800138104', '13800138103', '13800138102', '13800138101']
- id_card: ['110101199001011010', '110101199001011009', '110101199001011008', '110101199001011007', '110101199001011006', '110101199001011005', '110101199001011004', '110101199001011003', '110101199001011002', '110101199001011001']
- region: ['西南', '华北', '华南', '东北', '华中', '华东', '西南', '华北', '华南', '华东']
【建表 DDL】
CREATE TABLE customers (
  id INTEGER PRIMARY KEY,
  name TEXT,
  email TEXT,
  phone TEXT,
  id_card TEXT,
  region TEXT
);

请为每列生成注释；并额外为整张表生成一条表级描述（column 省略）。返回 JSON 数组，元素形如 {"table": "表名", "column": "列名", "comment": "一句话中文注释", "values": "可选。代码=含义，分号分隔"}，表级形如 {"table": "表名", "comment": "整表业务定位一句话"}。
只返回 JSON，不要多余文字。
```

**返回：**
```json
[
  {"table": "customers", "comment": "客户基本信息表"},
  {"table": "customers", "column": "id", "comment": "客户唯一标识"},
  {"table": "customers", "column": "name", "comment": "客户姓名"},
  {"table": "customers", "column": "email", "comment": "客户电子邮箱"},
  {"table": "customers", "column": "phone", "comment": "客户联系电话"},
  {"table": "customers", "column": "id_card", "comment": "客户身份证号码"},
  {"table": "customers", "column": "region", "comment": "客户所属大区"}
]
```

**usage:** `{"completion_tokens": 605, "prompt_tokens": 680, "total_tokens": 1285, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 454}}`


### customers · 无采样（不取数据）

**入参（完整 prompt）：**
```text
你是数据库语义分析专家。请根据建表 DDL 为每列生成准确的中文业务语义注释。
注释应简洁（一句话），说明该字段的业务用途，不要复述列名或类型。
仅依据字段名、类型与约束等结构信息判断；对于状态/标志位等低基数离散字段，仅当能从字段名或注释可靠推断时再简述其取值含义，否则给出保守的业务描述，不要编造具体取值。

【建表 DDL】
CREATE TABLE customers (
  id INTEGER PRIMARY KEY,
  name TEXT,
  email TEXT,
  phone TEXT,
  id_card TEXT,
  region TEXT
);

请为每列生成注释；并额外为整张表生成一条表级描述（column 省略）。返回 JSON 数组，元素形如 {"table": "表名", "column": "列名", "comment": "一句话中文注释"}，表级形如 {"table": "表名", "comment": "整表业务定位一句话"}。
只返回 JSON，不要多余文字。
```

**返回：**
```json
[
  {"table": "customers", "comment": "客户基本信息表"},
  {"table": "customers", "column": "id", "comment": "客户唯一标识"},
  {"table": "customers", "column": "name", "comment": "客户姓名"},
  {"table": "customers", "column": "email", "comment": "客户电子邮箱"},
  {"table": "customers", "column": "phone", "comment": "客户联系电话"},
  {"table": "customers", "column": "id_card", "comment": "客户身份证号码"},
  {"table": "customers", "column": "region", "comment": "客户所在地区"}
]
```

**usage:** `{"completion_tokens": 305, "prompt_tokens": 301, "total_tokens": 606, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 155}}`


### returns · 有采样（取数据）

**入参（完整 prompt）：**
```text
你是数据库语义分析专家。请根据建表 DDL 和样本取值，为每列生成准确的中文业务语义注释。
注释应简洁（一句话），说明该字段的业务用途，不要复述列名或类型。
若样本显示某列是低基数离散取值（如状态/类型/标志位），请在该列的 values 字段给出取值对照，格式为「代码=含义」并用分号分隔（如 P=待付款；S=已发货）；主键、金额、时间等高基数列省略 values。

【样本取值】
- id: [9, 8, 7, 6, 5, 4, 3, 2, 1]
- order_item_id: [69, 46, 32, 31, 30, 28, 13, 11, 5]
- returned_at: ['2026-07-10', '2026-07-15', '2026-07-12', '2026-07-14', '2026-07-21', '2026-07-18', '2026-07-01', '2026-07-27', '2026-07-07']
- reason: ['wrong_item', 'wrong_item', 'damaged', 'damaged', 'changed_mind', 'damaged', 'changed_mind', 'wrong_item', 'damaged']
【建表 DDL】
CREATE TABLE returns (
  id INTEGER PRIMARY KEY,
  order_item_id INTEGER,
  returned_at TIMESTAMP,
  reason TEXT
);

请为每列生成注释；并额外为整张表生成一条表级描述（column 省略）。返回 JSON 数组，元素形如 {"table": "表名", "column": "列名", "comment": "一句话中文注释", "values": "可选。代码=含义，分号分隔"}，表级形如 {"table": "表名", "comment": "整表业务定位一句话"}。
只返回 JSON，不要多余文字。
```

**返回：**
```json
[
  {"table": "returns", "comment": "退货记录表，记录订单项的退货申请与处理信息"},
  {"table": "returns", "column": "id", "comment": "退货记录唯一标识，主键"},
  {"table": "returns", "column": "order_item_id", "comment": "关联的订单项标识，指明退货对应的商品明细"},
  {"table": "returns", "column": "returned_at", "comment": "退货发生或登记的时间"},
  {"table": "returns", "column": "reason", "comment": "退货原因分类", "values": "wrong_item=商品错误；damaged=商品损坏；changed_mind=改变主意"}
]
```

**usage:** `{"completion_tokens": 272, "prompt_tokens": 509, "total_tokens": 781, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 120}}`


### returns · 无采样（不取数据）

**入参（完整 prompt）：**
```text
你是数据库语义分析专家。请根据建表 DDL 为每列生成准确的中文业务语义注释。
注释应简洁（一句话），说明该字段的业务用途，不要复述列名或类型。
仅依据字段名、类型与约束等结构信息判断；对于状态/标志位等低基数离散字段，仅当能从字段名或注释可靠推断时再简述其取值含义，否则给出保守的业务描述，不要编造具体取值。

【建表 DDL】
CREATE TABLE returns (
  id INTEGER PRIMARY KEY,
  order_item_id INTEGER,
  returned_at TIMESTAMP,
  reason TEXT
);

请为每列生成注释；并额外为整张表生成一条表级描述（column 省略）。返回 JSON 数组，元素形如 {"table": "表名", "column": "列名", "comment": "一句话中文注释"}，表级形如 {"table": "表名", "comment": "整表业务定位一句话"}。
只返回 JSON，不要多余文字。
```

**返回：**
```json
[
  {"table": "returns", "comment": "退货记录表，记录订单项的退货信息"},
  {"table": "returns", "column": "id", "comment": "退货记录唯一标识"},
  {"table": "returns", "column": "order_item_id", "comment": "关联的订单项标识"},
  {"table": "returns", "column": "returned_at", "comment": "退货发生的时间"},
  {"table": "returns", "column": "reason", "comment": "退货原因说明"}
]
```

**usage:** `{"completion_tokens": 238, "prompt_tokens": 296, "total_tokens": 534, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 123}}`


## 阶段二 · 领域标签


### 画像（新格式，阶段二上下文）

```sql
-- orders：订单主表，记录客户下单及订单状态信息
CREATE TABLE orders (
  id                       INTEGER    PRIMARY KEY  -- 描述：订单唯一标识，主键。取值示例：40,
  customer_id              INTEGER     -- 描述：下单客户标识，关联客户表。取值示例：7,
  address_id               INTEGER     -- 描述：收货地址标识，关联地址表。取值示例：7,
  campaign_id              INTEGER     -- 描述：营销活动标识，标识订单来源活动。取值示例：3,
  status                   TEXT        -- 描述：订单处理状态。取值示例：paid，pending，cancelled，shipped,
  total_amount             NUMERIC     -- 描述：订单应付总金额。取值示例：147.49
);

-- order_items：订单明细表，记录每个订单中商品的购买数量与成交单价
CREATE TABLE order_items (
  id                       INTEGER    PRIMARY KEY  -- 描述：订单明细记录唯一标识。取值示例：99,
  order_id                 INTEGER     -- 描述：所属订单的标识，关联订单主表。取值示例：40,
  product_id               INTEGER     -- 描述：所购商品的标识，关联商品主表。取值示例：15,
  quantity                 INTEGER     -- 描述：该商品在订单中的购买数量。取值示例：1,
  unit_price               NUMERIC     -- 描述：该商品在订单中的成交单价。取值示例：349
);

-- products：商品主数据表，存储商品的核心信息
CREATE TABLE products (
  id                       INTEGER    PRIMARY KEY  -- 描述：商品唯一标识。取值示例：20,
  product_name             TEXT        -- 描述：商品展示名称。取值示例：Grid 机械式键盘托，Parallax 显示器支架，Ember 恒温杯，Summit 三脚架，Latitude 旅行背包，Anchor USB-C 扩展坞，Slate 电容笔，Halo 台灯,
  category_id              INTEGER     -- 描述：商品所属分类。取值示例：2,
  supplier_id              INTEGER     -- 描述：商品供应商标识。取值示例：1,
  price                    NUMERIC     -- 描述：商品销售单价。取值示例：179,
  stock                    INTEGER     -- 描述：当前库存数量。取值示例：230
);

-- customers：客户基本信息表
CREATE TABLE customers (
  id                       INTEGER    PRIMARY KEY  -- 描述：客户唯一标识。取值示例：10,
  name                     TEXT        -- 描述：客户姓名。取值示例：黄诗琪，郑一帆，吴雨桐，孙晨曦，周子墨，刘安琪，赵晓彤，王振宇,
  email                    TEXT        -- 描述：客户电子邮箱。取值示例：黄诗琪@example.com，郑一帆@example.com，吴雨桐@example.com，孙晨曦@example.com，周子墨@example.com，刘安琪@example.com，赵晓彤@example.com，王振宇@example.com,
  phone                    TEXT        -- 描述：客户联系电话。取值示例：13800138110，13800138109，13800138108，13800138107，13800138106，13800138105，13800138104，13800138103,
  id_card                  TEXT        -- 描述：客户身份证号码。取值示例：110101199001011010，110101199001011009，110101199001011008，110101199001011007，110101199001011006，110101199001011005，110101199001011004，110101199001011003,
  region                   TEXT        -- 描述：客户所属大区。取值示例：西南，华北，华南，东北，华中，华东
);

-- returns：退货记录表，记录订单项的退货申请与处理信息
CREATE TABLE returns (
  id                       INTEGER    PRIMARY KEY  -- 描述：退货记录唯一标识，主键。取值示例：9,
  order_item_id            INTEGER     -- 描述：关联的订单项标识，指明退货对应的商品明细。取值示例：69,
  returned_at              TIMESTAMP   -- 描述：退货发生或登记的时间。取值示例：2026-07-10,
  reason                   TEXT        -- 描述：退货原因分类。取值示例：wrong_item，damaged，changed_mind
);
```


### 第一轮 · 批量划分

**入参（完整 prompt）：**
```text
你是数据库领域分析专家。请将下列表按业务语义划分为若干领域。
要求：
- 领域数量控制在约 2~4 个（库共 5 张表，规模越大领域可越多）；
- 每张表至少属于 1 个领域；一张表可挂多个领域（复合实体同时属于多个域）；
- 领域名简短、语义清晰、彼此不重叠；领域内表业务高度相关；
- 优先复用【已有标签库】的名称，确有新领域才提新名（新名会走人工确认）；
- 每项给出一句话「依据」（为什么这些表同属一个域）。
【已有标签库】
（暂无）

【表画像】
-- orders：订单主表，记录客户下单及订单状态信息
CREATE TABLE orders (
  id                       INTEGER    PRIMARY KEY  -- 描述：订单唯一标识，主键。取值示例：40,
  customer_id              INTEGER     -- 描述：下单客户标识，关联客户表。取值示例：7,
  address_id               INTEGER     -- 描述：收货地址标识，关联地址表。取值示例：7,
  campaign_id              INTEGER     -- 描述：营销活动标识，标识订单来源活动。取值示例：3,
  status                   TEXT        -- 描述：订单处理状态。取值示例：paid，pending，cancelled，shipped,
  total_amount             NUMERIC     -- 描述：订单应付总金额。取值示例：147.49
);

-- order_items：订单明细表，记录每个订单中商品的购买数量与成交单价
CREATE TABLE order_items (
  id                       INTEGER    PRIMARY KEY  -- 描述：订单明细记录唯一标识。取值示例：99,
  order_id                 INTEGER     -- 描述：所属订单的标识，关联订单主表。取值示例：40,
  product_id               INTEGER     -- 描述：所购商品的标识，关联商品主表。取值示例：15,
  quantity                 INTEGER     -- 描述：该商品在订单中的购买数量。取值示例：1,
  unit_price               NUMERIC     -- 描述：该商品在订单中的成交单价。取值示例：349
);

-- products：商品主数据表，存储商品的核心信息
CREATE TABLE products (
  id                       INTEGER    PRIMARY KEY  -- 描述：商品唯一标识。取值示例：20,
  product_name             TEXT        -- 描述：商品展示名称。取值示例：Grid 机械式键盘托，Parallax 显示器支架，Ember 恒温杯，Summit 三脚架，Latitude 旅行背包，Anchor USB-C 扩展坞，Slate 电容笔，Halo 台灯,
  category_id              INTEGER     -- 描述：商品所属分类。取值示例：2,
  supplier_id              INTEGER     -- 描述：商品供应商标识。取值示例：1,
  price                    NUMERIC     -- 描述：商品销售单价。取值示例：179,
  stock                    INTEGER     -- 描述：当前库存数量。取值示例：230
);

-- customers：客户基本信息表
CREATE TABLE customers (
  id                       INTEGER    PRIMARY KEY  -- 描述：客户唯一标识。取值示例：10,
  name                     TEXT        -- 描述：客户姓名。取值示例：黄诗琪，郑一帆，吴雨桐，孙晨曦，周子墨，刘安琪，赵晓彤，王振宇,
  email                    TEXT        -- 描述：客户电子邮箱。取值示例：黄诗琪@example.com，郑一帆@example.com，吴雨桐@example.com，孙晨曦@example.com，周子墨@example.com，刘安琪@example.com，赵晓彤@example.com，王振宇@example.com,
  phone                    TEXT        -- 描述：客户联系电话。取值示例：13800138110，13800138109，13800138108，13800138107，13800138106，13800138105，13800138104，13800138103,
  id_card                  TEXT        -- 描述：客户身份证号码。取值示例：110101199001011010，110101199001011009，110101199001011008，110101199001011007，110101199001011006，110101199001011005，110101199001011004，110101199001011003,
  region                   TEXT        -- 描述：客户所属大区。取值示例：西南，华北，华南，东北，华中，华东
);

-- returns：退货记录表，记录订单项的退货申请与处理信息
CREATE TABLE returns (
  id                       INTEGER    PRIMARY KEY  -- 描述：退货记录唯一标识，主键。取值示例：9,
  order_item_id            INTEGER     -- 描述：关联的订单项标识，指明退货对应的商品明细。取值示例：69,
  returned_at              TIMESTAMP   -- 描述：退货发生或登记的时间。取值示例：2026-07-10,
  reason                   TEXT        -- 描述：退货原因分类。取值示例：wrong_item，damaged，changed_mind
);

返回 JSON 数组，元素形如 {"name":"领域名","description":"一句话描述","tables":["表1","表2"],"reason":"依据"}。
只返回 JSON，不要多余文字。
```

**返回：**
```json
[
  {
    "name": "销售订单域",
    "description": "涵盖订单、订单明细及退货记录，反映交易履约与售后流程。",
    "tables": ["orders", "order_items", "returns"],
    "reason": "订单主表、订单明细表和退货记录表共同构成订单交易与售后链路，业务高度相关。"
  },
  {
    "name": "商品域",
    "description": "存储商品核心信息及商品在订单中的成交明细。",
    "tables": ["products", "order_items"],
    "reason": "商品主数据通过订单明细与销售行为关联，order_items 中的 product_id 和 unit_price 体现商品维度交易信息。"
  },
  {
    "name": "客户域",
    "description": "管理客户基本信息及其下单行为。",
    "tables": ["customers", "orders"],
    "reason": "客户表是订单表的下单主体来源，orders.customer_id 关联 customers.id，两者共同描述客户与订单的关系。"
  }
]
```

**usage:** `{"completion_tokens": 585, "prompt_tokens": 1200, "total_tokens": 1785, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 358}}`


### 第二轮 · 审校式自检

**入参（完整 prompt）：**
```text
你是数据库领域划分审校专家。请审查以下初版划分，重点检查：
- 单表成域：某域只有 1 张表且与相邻域可合并 → 建议合并；
- 语义重叠：两个域边界不清/高度重叠 → 合并或理清边界；
- 孤立表：任何表都必须至少属于一个域；
- 命名：域名应简短、语义清晰、互不重叠；
- 领域数量仍应控制在约 2~4 个。
【表画像】
-- orders：订单主表，记录客户下单及订单状态信息
CREATE TABLE orders (
  id                       INTEGER    PRIMARY KEY  -- 描述：订单唯一标识，主键。取值示例：40,
  customer_id              INTEGER     -- 描述：下单客户标识，关联客户表。取值示例：7,
  address_id               INTEGER     -- 描述：收货地址标识，关联地址表。取值示例：7,
  campaign_id              INTEGER     -- 描述：营销活动标识，标识订单来源活动。取值示例：3,
  status                   TEXT        -- 描述：订单处理状态。取值示例：paid，pending，cancelled，shipped,
  total_amount             NUMERIC     -- 描述：订单应付总金额。取值示例：147.49
);

-- order_items：订单明细表，记录每个订单中商品的购买数量与成交单价
CREATE TABLE order_items (
  id                       INTEGER    PRIMARY KEY  -- 描述：订单明细记录唯一标识。取值示例：99,
  order_id                 INTEGER     -- 描述：所属订单的标识，关联订单主表。取值示例：40,
  product_id               INTEGER     -- 描述：所购商品的标识，关联商品主表。取值示例：15,
  quantity                 INTEGER     -- 描述：该商品在订单中的购买数量。取值示例：1,
  unit_price               NUMERIC     -- 描述：该商品在订单中的成交单价。取值示例：349
);

-- products：商品主数据表，存储商品的核心信息
CREATE TABLE products (
  id                       INTEGER    PRIMARY KEY  -- 描述：商品唯一标识。取值示例：20,
  product_name             TEXT        -- 描述：商品展示名称。取值示例：Grid 机械式键盘托，Parallax 显示器支架，Ember 恒温杯，Summit 三脚架，Latitude 旅行背包，Anchor USB-C 扩展坞，Slate 电容笔，Halo 台灯,
  category_id              INTEGER     -- 描述：商品所属分类。取值示例：2,
  supplier_id              INTEGER     -- 描述：商品供应商标识。取值示例：1,
  price                    NUMERIC     -- 描述：商品销售单价。取值示例：179,
  stock                    INTEGER     -- 描述：当前库存数量。取值示例：230
);

-- customers：客户基本信息表
CREATE TABLE customers (
  id                       INTEGER    PRIMARY KEY  -- 描述：客户唯一标识。取值示例：10,
  name                     TEXT        -- 描述：客户姓名。取值示例：黄诗琪，郑一帆，吴雨桐，孙晨曦，周子墨，刘安琪，赵晓彤，王振宇,
  email                    TEXT        -- 描述：客户电子邮箱。取值示例：黄诗琪@example.com，郑一帆@example.com，吴雨桐@example.com，孙晨曦@example.com，周子墨@example.com，刘安琪@example.com，赵晓彤@example.com，王振宇@example.com,
  phone                    TEXT        -- 描述：客户联系电话。取值示例：13800138110，13800138109，13800138108，13800138107，13800138106，13800138105，13800138104，13800138103,
  id_card                  TEXT        -- 描述：客户身份证号码。取值示例：110101199001011010，110101199001011009，110101199001011008，110101199001011007，110101199001011006，110101199001011005，110101199001011004，110101199001011003,
  region                   TEXT        -- 描述：客户所属大区。取值示例：西南，华北，华南，东北，华中，华东
);

-- returns：退货记录表，记录订单项的退货申请与处理信息
CREATE TABLE returns (
  id                       INTEGER    PRIMARY KEY  -- 描述：退货记录唯一标识，主键。取值示例：9,
  order_item_id            INTEGER     -- 描述：关联的订单项标识，指明退货对应的商品明细。取值示例：69,
  returned_at              TIMESTAMP   -- 描述：退货发生或登记的时间。取值示例：2026-07-10,
  reason                   TEXT        -- 描述：退货原因分类。取值示例：wrong_item，damaged，changed_mind
);

【初版划分】
- 销售订单域（涵盖订单、订单明细及退货记录，反映交易履约与售后流程。）: orders, order_items, returns
- 商品域（存储商品核心信息及商品在订单中的成交明细。）: products, order_items
- 客户域（管理客户基本信息及其下单行为。）: customers, orders

若初版已合理，返回 {"unchanged": true}；
若有改进，返回完整修正后的领域 JSON 数组（格式、字段同第一轮）。
只返回 JSON，不要多余文字。
```

**返回：**
```json
[
  {
    "name": "销售订单域",
    "tables": ["orders", "order_items", "returns"]
  },
  {
    "name": "主数据域",
    "tables": ["customers", "products"]
  }
]
```

**usage:** `{"completion_tokens": 1260, "prompt_tokens": 1235, "total_tokens": 2495, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 1204}}`


**最终领域（采纳修正版）：**

```json
[
 {
  "name": "销售订单域",
  "description": "",
  "tables": [
   "orders",
   "order_items",
   "returns"
  ],
  "reason": ""
 },
 {
  "name": "主数据域",
  "description": "",
  "tables": [
   "customers",
   "products"
  ],
  "reason": ""
 }
]
```


## 阶段三 · 图谱


### 第一轮 · 全局扫描

**入参（完整 prompt）：**
```text
你是数据库关系分析专家。以下是某个数据库的全部表结构，请判断哪些表之间存在业务关联。
注意：
- 不限于同名列匹配，注意语义关联
- 可能存在复合关联（如 order_items 同时关联 orders 和 products）
- 注意区分：真正的业务关联 vs 仅仅是同名字段
- 返回 confident（高确信）和 uncertain（低确信）两类
方向规则：from_table 必须是多侧（明细/子表一侧，持有引用字段），to_table 是一侧（主表/父表）；from_col 为 from_table 中引用 to_table 的字段。
基数规则：cardinality 只能取 n:1 或 1:1（不确定取 n:1；from_col 为 from_table 主键时只能 1:1）。
禁止 n:m 直连边：多对多必须通过中间表拆成两条 n:1 边。

【表结构】
-- orders：订单主表，记录客户下单及订单状态信息
CREATE TABLE orders (
  id                       INTEGER    PRIMARY KEY  -- 描述：订单唯一标识，主键。取值示例：40,
  customer_id              INTEGER     -- 描述：下单客户标识，关联客户表。取值示例：7,
  address_id               INTEGER     -- 描述：收货地址标识，关联地址表。取值示例：7,
  campaign_id              INTEGER     -- 描述：营销活动标识，标识订单来源活动。取值示例：3,
  status                   TEXT        -- 描述：订单处理状态。取值示例：paid，pending，cancelled，shipped,
  total_amount             NUMERIC     -- 描述：订单应付总金额。取值示例：147.49
);

-- order_items：订单明细表，记录每个订单中商品的购买数量与成交单价
CREATE TABLE order_items (
  id                       INTEGER    PRIMARY KEY  -- 描述：订单明细记录唯一标识。取值示例：99,
  order_id                 INTEGER     -- 描述：所属订单的标识，关联订单主表。取值示例：40,
  product_id               INTEGER     -- 描述：所购商品的标识，关联商品主表。取值示例：15,
  quantity                 INTEGER     -- 描述：该商品在订单中的购买数量。取值示例：1,
  unit_price               NUMERIC     -- 描述：该商品在订单中的成交单价。取值示例：349
);

-- products：商品主数据表，存储商品的核心信息
CREATE TABLE products (
  id                       INTEGER    PRIMARY KEY  -- 描述：商品唯一标识。取值示例：20,
  product_name             TEXT        -- 描述：商品展示名称。取值示例：Grid 机械式键盘托，Parallax 显示器支架，Ember 恒温杯，Summit 三脚架，Latitude 旅行背包，Anchor USB-C 扩展坞，Slate 电容笔，Halo 台灯,
  category_id              INTEGER     -- 描述：商品所属分类。取值示例：2,
  supplier_id              INTEGER     -- 描述：商品供应商标识。取值示例：1,
  price                    NUMERIC     -- 描述：商品销售单价。取值示例：179,
  stock                    INTEGER     -- 描述：当前库存数量。取值示例：230
);

-- customers：客户基本信息表
CREATE TABLE customers (
  id                       INTEGER    PRIMARY KEY  -- 描述：客户唯一标识。取值示例：10,
  name                     TEXT        -- 描述：客户姓名。取值示例：黄诗琪，郑一帆，吴雨桐，孙晨曦，周子墨，刘安琪，赵晓彤，王振宇,
  email                    TEXT        -- 描述：客户电子邮箱。取值示例：黄诗琪@example.com，郑一帆@example.com，吴雨桐@example.com，孙晨曦@example.com，周子墨@example.com，刘安琪@example.com，赵晓彤@example.com，王振宇@example.com,
  phone                    TEXT        -- 描述：客户联系电话。取值示例：13800138110，13800138109，13800138108，13800138107，13800138106，13800138105，13800138104，13800138103,
  id_card                  TEXT        -- 描述：客户身份证号码。取值示例：110101199001011010，110101199001011009，110101199001011008，110101199001011007，110101199001011006，110101199001011005，110101199001011004，110101199001011003,
  region                   TEXT        -- 描述：客户所属大区。取值示例：西南，华北，华南，东北，华中，华东
);

-- returns：退货记录表，记录订单项的退货申请与处理信息
CREATE TABLE returns (
  id                       INTEGER    PRIMARY KEY  -- 描述：退货记录唯一标识，主键。取值示例：9,
  order_item_id            INTEGER     -- 描述：关联的订单项标识，指明退货对应的商品明细。取值示例：69,
  returned_at              TIMESTAMP   -- 描述：退货发生或登记的时间。取值示例：2026-07-10,
  reason                   TEXT        -- 描述：退货原因分类。取值示例：wrong_item，damaged，changed_mind
);

返回 JSON 数组，元素形如 {"from_table":"A","from_col":"a_id","to_table":"B","to_col":"id","cardinality":"n:1","confidence":"high/medium/low","reason":"一句话说明"}。
只返回 JSON，不要多余文字。
```

**返回：**
```json
[
  {"from_table":"orders","from_col":"customer_id","to_table":"customers","to_col":"id","cardinality":"n:1","confidence":"high","reason":"订单表通过 customer_id 引用客户表，一个客户对应多个订单"},
  {"from_table":"order_items","from_col":"order_id","to_table":"orders","to_col":"id","cardinality":"n:1","confidence":"high","reason":"订单明细通过 order_id 引用订单主表，一个订单包含多个明细"},
  {"from_table":"order_items","from_col":"product_id","to_table":"products","to_col":"id","cardinality":"n:1","confidence":"high","reason":"订单明细通过 product_id 引用商品表，一个商品可出现在多个明细中"},
  {"from_table":"returns","from_col":"order_item_id","to_table":"order_items","to_col":"id","cardinality":"n:1","confidence":"high","reason":"退货记录通过 order_item_id 引用订单明细，一个订单明细对应多条退货记录"}
]
```

**usage:** `{"completion_tokens": 2407, "prompt_tokens": 1266, "total_tokens": 3673, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 2181}}`


### 第二轮 · 候选验证（待裁决 0 条）

**入参（完整 prompt）：**
```text
你是数据库关系分析专家。以下是候选关系（来自程序启发式列名匹配，以及全局扫描中置信度较低的发现）。
请仅对列出的每一项裁决：成立 → status=confirmed（可修正关联字段/基数）；不成立 → status=rejected。
不得新增列表之外的关系；拒绝无实际业务关联的候选（如仅仅命名巧合）。
方向规则：from_table 必须是多侧，to_table 是一侧。
基数规则：cardinality 只能取 n:1 或 1:1（from_col 为 from_table 主键时只能 1:1）。

【相关表结构】
- orders: id(INTEGER PK), customer_id(INTEGER), address_id(INTEGER), campaign_id(INTEGER), status(TEXT), created_at(TIMESTAMP), total_amount(NUMERIC(10,2))
- order_items: id(INTEGER PK), order_id(INTEGER), product_id(INTEGER), quantity(INTEGER), unit_price(NUMERIC(10,2))
- products: id(INTEGER PK), product_name(TEXT), category_id(INTEGER), supplier_id(INTEGER), price(NUMERIC(10,2)), stock(INTEGER)
- customers: id(INTEGER PK), name(TEXT), email(TEXT), phone(TEXT), id_card(TEXT), region(TEXT)
- returns: id(INTEGER PK), order_item_id(INTEGER), returned_at(TIMESTAMP), reason(TEXT)

【待裁决候选关系】


返回 JSON 数组，元素形如 {"from_table":"A","from_col":"a_id","to_table":"B","to_col":"id","cardinality":"n:1","confidence":"high/medium/low","reason":"说明","status":"confirmed/rejected"}。
只返回 JSON，不要多余文字。
```

**返回：**
```json
[]
```

**usage:** `{"completion_tokens": 131, "prompt_tokens": 465, "total_tokens": 596, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 129}}`


**阶段三最终边（全局高置信 ∪ 验证通过）：**

```json
[
 {
  "from_table": "orders",
  "from_col": "customer_id",
  "to_table": "customers",
  "to_col": "id",
  "cardinality": "n:1",
  "reason": "订单表通过 customer_id 引用客户表，一个客户对应多个订单",
  "confidence": "high",
  "status": "",
  "source": "llm_global"
 },
 {
  "from_table": "order_items",
  "from_col": "order_id",
  "to_table": "orders",
  "to_col": "id",
  "cardinality": "n:1",
  "reason": "订单明细通过 order_id 引用订单主表，一个订单包含多个明细",
  "confidence": "high",
  "status": "",
  "source": "llm_global"
 },
 {
  "from_table": "order_items",
  "from_col": "product_id",
  "to_table": "products",
  "to_col": "id",
  "cardinality": "n:1",
  "reason": "订单明细通过 product_id 引用商品表，一个商品可出现在多个明细中",
  "confidence": "high",
  "status": "",
  "source": "llm_global"
 },
 {
  "from_table": "returns",
  "from_col": "order_item_id",
  "to_table": "order_items",
  "to_col": "id",
  "cardinality": "n:1",
  "reason": "退货记录通过 order_item_id 引用订单明细，一个订单明细对应多条退货记录",
  "confidence": "high",
  "status": "",
  "source": "llm_global"
 }
]
```
