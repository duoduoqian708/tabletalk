"""redact.py 双结构名单 + 自定义档（auto_rules=False）主流程断言。"""
from app.safety.redact import _is_sensitive_col, redact_rows

SALT = b"x" * 32


def test_dict_entry_matches_exact_column():
    """dict{table, columns[]}：精确表+列命中；其他表/列不误伤；不再崩溃（回归）。"""
    sens = [{"table": "users", "columns": ["phone", "email"]}]
    assert _is_sensitive_col("users", "phone", sens)
    assert _is_sensitive_col("USERS", "EMAIL", sens)  # 大小写不敏感
    assert not _is_sensitive_col("orders", "phone", sens)
    assert not _is_sensitive_col("users", "address", sens)


def test_dict_entry_whole_table():
    """dict{table}（columns 缺/空）= 整表敏感。"""
    sens = [{"table": "secret"}, {"table": "half", "columns": []}]
    assert _is_sensitive_col("secret", "anything", sens)
    assert _is_sensitive_col("half", "col", sens)
    assert not _is_sensitive_col("other", "col", sens)


def test_str_glob_entry_still_works():
    """str glob 条目行为保持不变，且可与 dict 条目混用。"""
    sens = ["users.phone", {"table": "logs", "columns": ["ip"]}]
    assert _is_sensitive_col("users", "phone", sens)
    assert _is_sensitive_col("logs", "ip", sens)
    assert not _is_sensitive_col("users", "email", sens)


def test_custom_mode_only_explicit_columns():
    """auto_rules=False（自定义档）：仅名单列 token 化；其他列即使含手机号/列名含 email 也原样。"""
    sens = [{"table": "users", "columns": ["phone"]}]
    rows = [["13812345678", "a@b.com"], ["13812345678", "c@d.com"]]
    out, mp = redact_rows(rows, ["phone", "email"], "users", SALT, sens, auto_rules=False)
    assert out[0][0].startswith("[PHONE_") or out[0][0].startswith("[SENSITIVE_")
    # 同值同 token（确定性）
    assert out[0][0] == out[1][0]
    # 未选中的 email 列：原样（值正则也不扫）
    assert out[0][1] == "a@b.com"
    assert out[1][1] == "c@d.com"


def test_custom_mode_no_selection_passthrough():
    """自定义档零选中：整批原样直通（等价开放）。"""
    rows = [["13812345678", "a@b.com"]]
    out, mp = redact_rows(rows, ["phone", "email"], "users", SALT, [], auto_rules=False)
    assert out == rows
    assert mp == {}


def test_standard_mode_auto_rules_still_on():
    """auto_rules=True（标准档）：列名语义生效；不再做值正则逐格扫描，非命中列原样透传。"""
    rows = [["13812345678", "hello"], ["x", "zhang san"]]
    out, mp = redact_rows(rows, ["tel", "name"], "t", SALT, [])
    # tel 列命中列名语义 → 整列 token 化
    assert out[0][0].startswith("[PHONE_")
    # name 列命中列名语义 → 整列 token 化
    assert out[1][1].startswith("[NAME_")
    # 不再扫值：非敏感列（note）里的邮箱原样保留
    out2, mp2 = redact_rows([["see a@b.com"]], ["note"], "t", SALT, [])
    assert out2 == [["see a@b.com"]]
    assert mp2 == {}
