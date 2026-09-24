"""设计归档：规范化 JSON 清单、SHA-256 摘要与归档后差异定位。

清单格式（v1）是一个纯数据 JSON 对象，只包含**设计参数**与**审计结果**
（不含 id、created_at 等入库元数据，因此同一批设计无论何时归档都得到
同一摘要）：

    {
      "format": "design-archive-v1",
      "design_ids": [1, 2, 3],      // 升序、去重
      "design_count": 3,
      "items": [
        {
          "design_id": 1,
          "parameters": { name, cyclic, max_float, shafts, treadles,
                          warp_ends, weft_picks, threading, tie_up, treadling },
          "audit": { exceeds, interlacing, violations }
        }
      ]
    }

规范化（canonical）序列化规则固定为：UTF-8、键按字典序（sort_keys）、
无多余空白（separators=(",", ":")）、不转义非 ASCII（ensure_ascii=False）。
SHA-256 摘要即规范化字节流的十六进制摘要；摘要相同当且仅当清单内容相同，
因此相同 ID 集合（顺序无关）必然幂等复用同一条归档记录。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

MANIFEST_FORMAT = "design-archive-v1"

# json.loads 解析失败的哨兵（合法清单永远是 JSON 对象，不会是它）。
_UNPARSEABLE = object()


def canonical_bytes(payload: Any) -> bytes:
    """把清单对象序列化为规范化字节流（摘要与持久化都基于这串字节）。"""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_text(payload: Any) -> str:
    """规范化字节流的 UTF-8 文本形式（入库的 manifest 列即此文本）。"""
    return canonical_bytes(payload).decode("utf-8")


def digest_of(payload: Any) -> str:
    """清单对象的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def build_manifest(designs: list[dict]) -> dict:
    """按 ID 升序读取到的设计详情（db.get_designs_many 的返回）构建清单。

    调用方保证 designs 已按 ID 升序排列且全部存在。
    """
    design_ids = [d["id"] for d in designs]
    return {
        "format": MANIFEST_FORMAT,
        "design_ids": design_ids,
        "design_count": len(design_ids),
        "items": [_build_item(d) for d in designs],
    }


def _build_item(design: dict) -> dict:
    audit = design["audit"]
    return {
        "design_id": design["id"],
        "parameters": {
            "name": design["name"],
            "cyclic": design["cyclic"],
            "max_float": design["max_float"],
            "shafts": design["shafts"],
            "treadles": design["treadles"],
            "warp_ends": design["warp_ends"],
            "weft_picks": design["weft_picks"],
            "threading": design["threading"],
            "tie_up": design["tie_up"],
            "treadling": design["treadling"],
        },
        "audit": {
            "exceeds": audit["exceeds"],
            "interlacing": audit["interlacing"],
            "violations": audit["violations"],
        },
    }


def first_diff_path(stored: Any, current: Any) -> str | None:
    """定位两份清单之间的**首个**差异路径（JSON Pointer 风格，根为 ""）。

    遍历顺序与规范化序列化一致：对象按键名字典序、数组按下标升序；
    数组等长前逐项比较，长度不同且公共前缀全等时指向第一个缺失下标；
    对象键集合不同时指向首个缺失 / 多出的键；类型或标量值不同时指向当前
    位置。两份内容完全一致（摘要必然一致）时返回 None。
    """
    parts = _first_diff(stored, current, [])
    if parts is None:
        return None
    return "" if not parts else "/" + "/".join(str(p) for p in parts)


def _first_diff(stored: Any, current: Any, path: list) -> list | None:
    if isinstance(stored, dict) and isinstance(current, dict):
        for key in sorted(set(stored) | set(current)):
            if key not in stored or key not in current:
                return path + [key]
            nested = _first_diff(stored[key], current[key], path + [key])
            if nested is not None:
                return nested
        return None
    if isinstance(stored, list) and isinstance(current, list):
        for index in range(min(len(stored), len(current))):
            nested = _first_diff(
                stored[index], current[index], path + [index]
            )
            if nested is not None:
                return nested
        if len(stored) != len(current):
            return path + [min(len(stored), len(current))]
        return None
    if type(stored) is not type(current) or stored != current:
        return path
    return None


def _parse_json(text: Any) -> Any:
    """把列文本解析为 JSON；文本不是字符串或无法解析时返回哨兵
    `_UNPARSEABLE`（不抛异常，损坏行也能被校验）。"""
    if not isinstance(text, str):
        return _UNPARSEABLE
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return _UNPARSEABLE


def _is_int(value: Any) -> bool:
    """严格整数（拒绝 bool——Python 中 True/False 也是 int）。"""
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class StoredArchiveCheck:
    """归档行**自身**完整性校验的结果（与当前设计数据无关的部分）。

    - stored_manifest：原始 manifest 文本可解析时为解析对象，否则为哨兵；
    - manifest_parseable：manifest 文本是否为合法 JSON；
    - manifest_canonical：manifest 文本是否等于其内容的规范化序列化；
    - digest_matches：原始文本字节流的 SHA-256 是否等于 digest 列；
    - design_ids_parseable：design_ids 列是否为合法 JSON；
    - design_ids：解析后的 ID 列表（无法解析时为 None）；
    - manifest_matches_columns：清单内 design_ids / design_count 是否与
      design_ids / design_count 列一致；
    - self_consistent：以上全部成立（归档行自身可信）。
    """

    stored_manifest: Any
    manifest_parseable: bool
    manifest_canonical: bool
    digest_matches: bool
    design_ids_parseable: bool
    design_ids: list[int] | None
    manifest_matches_columns: bool
    self_consistent: bool


def verify_stored_archive(
    *,
    manifest_text: Any,
    digest: Any,
    design_ids_text: Any,
    design_count: Any,
) -> StoredArchiveCheck:
    """只针对归档行自身做完整性校验（读取**原始列值**，不触碰设计表）。

    依次检查：manifest 文本可解析为 JSON；该文本即规范化序列化；其
    SHA-256 等于 digest 列；design_ids 列可解析为整数列表；清单内的
    design_ids / design_count 与两个列值一致。任何一项不成立（含列被改写成
    非法 JSON）都标记 `self_consistent=False`，且全程不抛异常。
    """
    stored = _parse_json(manifest_text)
    manifest_parseable = stored is not _UNPARSEABLE

    manifest_canonical = (
        manifest_parseable
        and isinstance(manifest_text, str)
        and canonical_text(stored) == manifest_text
    )

    digest_matches = (
        manifest_parseable
        and isinstance(digest, str)
        and digest_of(stored) == digest
    )

    design_ids = _parse_json(design_ids_text)
    design_ids_parseable = (
        design_ids is not _UNPARSEABLE
        and isinstance(design_ids, list)
        and all(_is_int(value) for value in design_ids)
    )
    design_ids_value: list[int] | None = (
        design_ids if design_ids_parseable else None
    )

    manifest_matches_columns = False
    if manifest_parseable and isinstance(stored, dict) and design_ids_parseable:
        manifest_ids = stored.get("design_ids")
        manifest_count = stored.get("design_count")
        manifest_matches_columns = (
            isinstance(manifest_ids, list)
            and manifest_ids == design_ids_value
            and _is_int(manifest_count)
            and manifest_count == design_count
            and len(design_ids_value) == manifest_count
        )

    self_consistent = (
        manifest_parseable
        and manifest_canonical
        and digest_matches
        and design_ids_parseable
        and manifest_matches_columns
    )
    return StoredArchiveCheck(
        stored_manifest=stored if manifest_parseable else _UNPARSEABLE,
        manifest_parseable=manifest_parseable,
        manifest_canonical=manifest_canonical,
        digest_matches=digest_matches,
        design_ids_parseable=design_ids_parseable,
        design_ids=design_ids_value,
        manifest_matches_columns=manifest_matches_columns,
        self_consistent=self_consistent,
    )
