"""truncate_samples 值级截断单元测试 + is_noise_column 谓词回归。

用户决策：授权样本出网不做任何列级过滤（不按列名、不按类型），整行样本直发 LLM，
唯一防护是值级截断。is_noise_column 不再参与样本出网，但仍服务
build_graph_overview(filter_noise=True)，其谓词级回归测试保留于此。
"""
from __future__ import annotations

from app.knowledge.ddl_context import is_noise_column, truncate_samples


def test_truncate_samples_boundary_60():
    v60 = "a" * 60
    out = truncate_samples({"t": {"c": [v60, "b" * 61]}})
    assert out["t"]["c"] == [v60, "b" * 60]


def test_truncate_samples_custom_max_len():
    out = truncate_samples({"t": {"c": ["x" * 10]}}, max_len=5)
    assert out["t"]["c"] == ["xxxxx"]


def test_truncate_samples_pure_and_none_preserved():
    samples = {"t": {"long_col": ["y" * 100], "mixed": [1, 2.5, None]}}
    out = truncate_samples(samples)
    # 纯函数：入参不被修改
    assert len(samples["t"]["long_col"][0]) == 100
    # 非字符串 str() 归一；None 原样保留（下游 _distinct/注释均跳过 None）
    assert out["t"]["mixed"] == ["1", "2.5", None]
    assert out["t"]["long_col"] == ["y" * 60]


def test_truncate_samples_structure_and_empty():
    assert truncate_samples({}) == {}
    out = truncate_samples({"t1": {"a": ["z" * 70]}, "t2": {"b": []}})
    assert set(out.keys()) == {"t1", "t2"}
    assert out["t2"] == {"b": []}


# ---------- is_noise_column 谓词回归（build_graph_overview 在用，与样本出网无关） ----------


def test_is_noise_column_case_insensitive_and_patterns():
    assert is_noise_column("CREATED_BY")           # 审计人模式
    assert is_noise_column("tenant_id")            # 租户模式
    assert not is_noise_column("user_name")
    assert not is_noise_column("status")


def test_is_noise_column_version_token_level():
    """version 仅词元级命中，防止误伤业务列。"""
    assert is_noise_column("row_version")          # _version 结尾
    assert is_noise_column("version")              # 整词
    assert is_noise_column("version_flag")         # version_ 开头词元
    assert is_noise_column("schema_version_log")   # 含 _version_ 词元
    assert is_noise_column("Row_Version")          # 大小写归一
    assert not is_noise_column("conversion_rate")  # 子串误伤回归样例
    assert not is_noise_column("diversion_flag")   # 子串误伤回归样例


def test_is_noise_column_legacy_exact_set_still_hits():
    """原 NOISE_COLUMNS 精确集合规则不得丢失（如 gmt_create 不命中新模式）。"""
    assert is_noise_column("gmt_create")
    assert is_noise_column("create_time")
    assert is_noise_column("del_flag")


def test_is_noise_column_long_text_types():
    assert is_noise_column("payload", "LONGTEXT")
    assert is_noise_column("payload", "text")       # 小写类型
    assert is_noise_column("payload", "BYTEA")
    assert not is_noise_column("payload", "VARCHAR(200)")
