"""T6 路径序列化 + 图校验测试。"""
from __future__ import annotations

from app.knowledge.graph.model import GraphEdge
from app.knowledge.graph.traverse import path_strings, reachable_tables, validate_join


def _e(src, tgt, cols, confidence=1.0, guard=None, cardinality="n:1") -> GraphEdge:
    return GraphEdge(source_table=src, target_table=tgt, cols=cols,
                     confidence=confidence, guard=guard, cardinality=cardinality)


# ---------- path_strings ----------

def test_parallel_edges_all_output():
    """多条平行边全保留（不按表去重）。"""
    edges = [
        _e("orders", "users", [("user_id", "id")]),
        _e("orders", "users", [("shipped_by", "id")]),
        _e("orders", "users", [("approved_by", "id")]),
    ]
    out = path_strings(edges, {"orders"}, hops=1)
    assert len(out) == 3
    assert any("orders.user_id = users.id" in p for p in out)
    assert any("orders.shipped_by = users.id" in p for p in out)
    assert any("orders.approved_by = users.id" in p for p in out)


def test_composite_key_path():
    e = _e("orders", "users", [("order_id", "id"), ("line_no", "line_no")])
    out = path_strings([e], {"orders"}, hops=1)
    assert len(out) == 1
    assert "orders.order_id = users.id AND orders.line_no = users.line_no" in out[0]


def test_guarded_path():
    e = _e("x", "table1", [("code", "code")], guard="x.type = 1")
    out = path_strings([e], {"x"}, hops=1)
    assert len(out) == 1
    assert "AND x.type = 1" in out[0]


def test_self_loop_no_deadlock():
    edges = [
        _e("employee", "employee", [("manager_id", "id")]),   # 自环
        _e("employee", "department", [("dept_id", "id")]),    # 普通边
    ]
    out = path_strings(edges, {"employee"}, hops=2)
    assert len(out) == 2
    assert any("employee.manager_id = employee.id" in p for p in out)
    assert any("employee.dept_id = department.id" in p for p in out)


def test_hops_bound():
    edges = [
        _e("a", "b", [("b_id", "id")]),
        _e("b", "c", [("c_id", "id")]),
        _e("c", "d", [("d_id", "id")]),
    ]
    out = path_strings(edges, {"a"}, hops=2)
    assert any("a.b_id = b.id" in p for p in out)
    assert any("b.c_id = c.id" in p for p in out)
    assert not any("c.d_id = d.id" in p for p in out)  # 第 3 跳被边界截断


def test_empty_seeds():
    assert path_strings([_e("a", "b", [("b_id", "id")])], set(), hops=2) == []


def test_path_strings_allowed_filter():
    """B3/R15：allowed 表集限定——路径串只输出两端都在允许表内的边。"""
    edges = [
        _e("a", "b", [("b_id", "id")]),
        _e("b", "c", [("c_id", "id")]),     # c 不在 allowed → 排除
        _e("a", "d", [("d_id", "id")]),     # d 不在 allowed → 排除
    ]
    out = path_strings(edges, {"a"}, hops=2, allowed={"a", "b"})
    assert len(out) == 1
    assert "a.b_id = b.id" in out[0]


# ---------- reachable_tables（T6：expand_tables 重写后的表集合语义） ----------

def test_reachable_basic():
    edges = [
        _e("a", "b", [("b_id", "id")]),
        _e("b", "c", [("c_id", "id")]),
    ]
    assert reachable_tables(edges, {"a"}, hops=1) == {"a", "b"}
    assert reachable_tables(edges, {"a"}, hops=2) == {"a", "b", "c"}


def test_reachable_kind_filter():
    edges = [
        _e("a", "b", [("b_id", "id")], confidence=1.0),                    # fk
        GraphEdge(source_table="x", target_table="y", cols=[("id", "id")],
                  relation="value_overlap", confidence=0.5),
    ]
    assert reachable_tables(edges, {"a"}, hops=1, kinds={"fk"}) == {"a", "b"}
    # 无 kind 过滤：全部边类型参与
    assert reachable_tables(edges, {"x"}, hops=1) == {"x", "y"}


def test_reachable_exact_case_no_false_match():
    """大小写错配回归：Users 与 users 是不同表，不得互相扩散（原 _fk_adj lower() 缺陷）。"""
    edges = [_e("orders", "Users", [("user_id", "id")])]
    assert reachable_tables(edges, {"orders"}, hops=1) == {"orders", "Users"}
    assert reachable_tables(edges, {"users"}, hops=1) == {"users"}  # 小写种子不命中大写表


def test_reachable_empty_seeds():
    assert reachable_tables([_e("a", "b", [("b_id", "id")])], set(), hops=2) == set()


def test_confidence_sort():
    """高置信度边优先输出。"""
    edges = [
        _e("orders", "users", [("user_id", "id")], confidence=0.6),
        _e("orders", "users", [("shipped_by", "id")], confidence=1.0),
    ]
    out = path_strings(edges, {"orders"}, hops=1)
    assert "shipped_by" in out[0]  # confidence 1.0 优先


def test_dict_edges_accepted():
    """dict 边（存储格式）也接受。"""
    edges = [{"from": "orders", "from_col": "user_id", "to": "users", "to_col": "id",
              "kind": "fk", "weight": 1.0, "shared": None, "cardinality": "n:1", "reason": ""}]
    out = path_strings(edges, {"orders"}, hops=1)
    assert len(out) == 1


# ---------- validate_join ----------

def test_validate_join_hit():
    edges = [_e("orders", "users", [("user_id", "id")])]
    assert validate_join(edges, "orders", "user_id", "users", "id") is True


def test_validate_join_reverse():
    edges = [_e("orders", "users", [("user_id", "id")])]
    assert validate_join(edges, "users", "id", "orders", "user_id") is True


def test_validate_join_miss():
    edges = [_e("orders", "users", [("user_id", "id")])]
    assert validate_join(edges, "orders", "shipped_by", "users", "id") is False


def test_validate_join_guarded():
    edges = [_e("x", "table1", [("code", "code")], guard="x.type = 1")]
    assert validate_join(edges, "x", "code", "table1", "code") is True


def test_validate_join_composite():
    edges = [_e("orders", "users", [("order_id", "id"), ("line_no", "line_no")])]
    assert validate_join(edges, "orders", "order_id", "users", "id") is True
    assert validate_join(edges, "orders", "line_no", "users", "line_no") is True
