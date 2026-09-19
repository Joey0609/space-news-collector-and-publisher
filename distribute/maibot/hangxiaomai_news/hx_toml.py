# -*- coding: utf-8 -*-
"""hx_toml.py · TOML 读 / 写（中文 key 必须加引号，这是本项目的坑）
================================================================
读：用标准库 tomllib（只读）。
写：自己拼字面量 —— 因为 tomllib 没有写功能，且必须保证
    - 中文 key 用引号："活跃时段" = "09:00-21:00"
    - 值类型明确（bool/int/str/list）
    - 无 BOM UTF-8（麦麦本体对 BOM 敏感，见 switch.ps1 注释）
"""
import tomllib
from pathlib import Path


def read_toml(path: Path) -> dict:
    """读一个 toml，返回 {表名: {key:value}}；不存在或解析失败返回 {}。"""
    try:
        if not path.exists():
            return {}
        with open(path, "rb") as f:
            return dict(tomllib.load(f))
    except Exception:
        return {}


def toml_value(v) -> str:
    """把简单值格式化为 TOML 字面量（支持 bool/int/float/str/list）。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def write_toml(path: Path, tables: dict) -> bool:
    """把 {表名: {key:value}} 写成嵌套 TOML（中文 key 自动加引号）。成功 True。"""
    lines = []
    for table, fields in tables.items():
        if not fields:
            continue
        lines.append(f"[{table}]")
        for k, v in fields.items():
            lines.append(f'"{k}" = {toml_value(v)}')
        lines.append("")
    if not lines:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return True
    except Exception:
        return False
