"""出网样本列裁剪器 llm_safe_samples 单元测试。"""
from app.knowledge.ddl_context import is_noise_column, llm_safe_samples

SCHEMA_COLS = [
    {"table": "t", "name": "id", "type": "INTEGER"},
    {"table": "t", "name": "status", "type": "VARCHAR"},
    {"table": "t", "name": "created_by", "type": "VARCHAR"},
    {"table": "t", "name": "created_at", "type": "DATETIME"},
    {"table": "t", "name": "remark", "type": "TEXT"},
]


def test_llm_safe_samples_drops_noise_and_longtext():
    samples = {
        "t": {
            "id": [1, 2],
            "status": ["P", "R"],
            "created_by": ["a"],
            "created_at": ["2026-01-01"],
            "remark": ["x" * 100],
        }
    }
    out = llm_safe_samples(samples, SCHEMA_COLS)
    assert set(out["t"].keys()) == {"id", "status"}
    # 原始 samples 不被修改（仅作用于出网副本）
    assert "remark" in samples["t"]


def test_llm_safe_samples_unknown_table_kept():
    """schema 未覆盖的表没有类型/名单依据，保守起见原样保留（宁可多看不可错杀）。"""
    out = llm_safe_samples({"other": {"a": [1]}}, SCHEMA_COLS)
    assert out == {"other": {"a": [1]}}


def test_llm_safe_samples_drop_longtext_false_keeps_text_drops_noise_names():
    """枚举场景（drop_longtext=False）：豁免长文本类型过滤——TEXT 低基数列恰是
    枚举字典主目标（SQLite/PG 的 status 常为 TEXT）；噪声列名模式仍然剔除，
    误发风险由枚举基数护栏 [2,50] 与值级截断兜底。"""
    samples = {
        "t": {
            "id": [1, 2],
            "status": ["P", "R"],
            "created_by": ["a"],
            "remark": ["x" * 100],
        }
    }
    out = llm_safe_samples(samples, SCHEMA_COLS, drop_longtext=False)
    assert set(out["t"].keys()) == {"id", "status", "remark"}


def test_llm_safe_samples_empty_input():
    assert llm_safe_samples({}, SCHEMA_COLS) == {}


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
