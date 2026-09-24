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


def is_canonical_text(manifest_text: str) -> bool:
    """文本是否恰好为其 JSON 内容的规范化序列化。

    用于校验持久化的 manifest 列未被改写：能解析但带多余空白、键序不同或
    非紧凑分隔符的文本（如 ``{"a": 1}``、缩进排版等）都判为已被改写。
    """
    try:
        payload = json.loads(manifest_text)
    except (json.JSONDecodeError, ValueError):
        return False
    return canonical_text(payload) == manifest_text


def _as_id_list(value: Any) -> list[int] | None:
    """把 JSON 值窄化为「正整数列表」（bool 是 int 子类，需显式排除）。"""
    if not isinstance(value, list):
        return None
    ids: list[int] = []
    for item in value:
        if type(item) is not int or isinstance(item, bool) or item < 1:
            return None
        ids.append(item)
    return ids


def valid_manifest_shape(payload: Any) -> bool:
    """已解析清单是否具备 v1 清单的基本结构与内部自洽性。

    要求顶层为对象，``format``/``design_ids``/``design_count``/``items``
    四个键齐备且类型正确，design_ids 为升序去重的正整数列表，
    design_count 与其长度一致，items 数量相同且各项的 design_id 按同一顺序
    对齐。其余字段（parameters/audit 的叶子）是否与设计一致由与当前快照
    的逐路径比较负责，不在此检查。
    """
    if not isinstance(payload, dict):
        return False
    design_ids = _as_id_list(payload.get("design_ids"))
    if design_ids is None:
        return False
    design_count = payload.get("design_count")
    if type(design_count) is not int or isinstance(design_count, bool):
        return False
    items = payload.get("items")
    if not isinstance(items, list):
        return False
    if payload.get("format") != MANIFEST_FORMAT:
        return False
    if design_count != len(design_ids) or len(items) != len(design_ids):
        return False
    if len(set(design_ids)) != len(design_ids):
        return False
    if design_ids != sorted(design_ids):
        return False
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return False
        design_id = item.get("design_id")
        if type(design_id) is not int or isinstance(design_id, bool):
            return False
        if design_id != design_ids[index]:
            return False
        if not isinstance(item.get("parameters"), dict):
            return False
        if not isinstance(item.get("audit"), dict):
            return False
    return True


@dataclass(frozen=True)
class StoredArchiveCheck:
    """归档记录**自身**的校验结果（不涉及当前设计快照）。

    - manifest：manifest 列解析出的对象；无法解析时为 None
    - design_ids：design_ids 列解析出的正整数列表；无法解析时为 None
    - structurally_ok：manifest 可解析且具备自洽的 v1 清单结构
    - self_consistent：manifest 可解析、为规范化文本、SHA-256 等于 digest
      列，且清单内 design_ids/design_count 与对应列值一致
    """

    manifest: Any
    design_ids: list[int] | None
    structurally_ok: bool
    self_consistent: bool


def inspect_stored_archive(
    *,
    manifest_text: str,
    digest: str,
    design_ids_text: str,
    design_count: Any,
) -> StoredArchiveCheck:
    """读取归档列的**原始文本**做自身完整性校验。

    全过程对非法 JSON 容错（返回 self_consistent=False 而非抛异常），依次
    检查：manifest 文本可解析、是规范化 JSON、其 SHA-256 等于 digest 列、
    清单结构自洽，以及清单内 design_ids/design_count 与列值一致。
    """
    try:
        manifest = json.loads(manifest_text)
    except (json.JSONDecodeError, ValueError):
        manifest = None
    try:
        column_ids = _as_id_list(json.loads(design_ids_text))
    except (json.JSONDecodeError, ValueError):
        column_ids = None

    structurally_ok = manifest is not None and valid_manifest_shape(manifest)
    self_consistent = False
    if structurally_ok:
        digest_match = (
            hashlib.sha256(manifest_text.encode("utf-8")).hexdigest() == digest
        )
        self_consistent = (
            digest_match
            and is_canonical_text(manifest_text)
            and column_ids == manifest["design_ids"]
            and type(design_count) is int
            and not isinstance(design_count, bool)
            and design_count == manifest["design_count"]
        )
    return StoredArchiveCheck(
        manifest=manifest,
        design_ids=column_ids,
        structurally_ok=structurally_ok,
        self_consistent=self_consistent,
    )


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
