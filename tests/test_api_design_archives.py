"""POST /design-archives 与 GET /design-archives/{id}/verify 接口集成测试。

使用临时 SQLite 文件与 FastAPI TestClient，不依赖运行中的服务，也不触碰
仓库默认的 data/weave.db。直接执行：python3 tests/test_api_design_archives.py
（也可被 pytest 收集）。
"""

import hashlib
import json
import os
import sqlite3
import sys
import tempfile

_TMP_DB = tempfile.NamedTemporaryFile(prefix="weave-test-", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["DATABASE_PATH"] = _TMP_DB.name

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.archives import MANIFEST_FORMAT, canonical_bytes  # noqa: E402
from app.main import app  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


TWILL = {
    "name": "2/2 twill",
    "cyclic": True,
    "max_float": 2,
    "threading": [0, 1, 2, 3, 0, 1, 2, 3],
    "tie_up": [
        [1, 1, 0, 0],
        [0, 1, 1, 0],
        [0, 0, 1, 1],
        [1, 0, 0, 1],
    ],
    "treadling": [0, 1, 2, 3, 0, 1, 2, 3],
}

# 与 TWILL 交织矩阵尺寸不同（2 经 4 纬）的平纹
PLAIN = {
    "name": "plain",
    "cyclic": False,
    "max_float": 1,
    "threading": [0, 1],
    "tie_up": [[1, 0], [0, 1]],
    "treadling": [0, 1, 0, 1],
}


def _create(client, payload):
    return client.post("/designs", json=payload).json()


def _archive_rows():
    """直接数 design_archives 表行数（用于断言失败不留半成品）。"""
    with sqlite3.connect(_TMP_DB.name) as conn:
        return conn.execute("SELECT COUNT(*) FROM design_archives").fetchone()[0]


def _corrupt_archive(archive_id, **columns):
    """直接在 SQLite 中改写归档行的列（模拟归档落盘后被外部进程篡改）。"""
    if not columns:
        return
    assignments = ", ".join(f"{name} = ?" for name in columns)
    with sqlite3.connect(_TMP_DB.name) as conn:
        conn.execute(
            f"UPDATE design_archives SET {assignments} WHERE id = ?",
            (*columns.values(), archive_id),
        )


def _archive_row_raw(archive_id):
    with sqlite3.connect(_TMP_DB.name) as conn:
        row = conn.execute(
            "SELECT digest, design_ids, design_count, manifest "
            "FROM design_archives WHERE id = ?",
            (archive_id,),
        ).fetchone()
    return {"digest": row[0], "design_ids": row[1],
            "design_count": row[2], "manifest": row[3]}


def test_create_archive_manifest_and_digest():
    with TestClient(app) as client:
        a = _create(client, TWILL)
        b = _create(client, PLAIN)

        # 乱序提交：清单仍按 ID 升序
        resp = client.post("/design-archives", json={"design_ids": [b["id"], a["id"]]})
        check(resp.status_code == 201, "first archive returns 201")
        data = resp.json()
        check(data["reused"] is False, "new archive reports reused=false")
        check(data["design_ids"] == [a["id"], b["id"]],
              "manifest design_ids sorted ascending")
        check(data["design_count"] == 2, "design_count is 2")
        check(isinstance(data["id"], int) and data["id"] >= 1, "archive has an id")
        check(len(data["digest"]) == 64, "digest is 64 hex chars")

        manifest = data["manifest"]
        check(manifest["format"] == MANIFEST_FORMAT, "manifest format marker")
        check(manifest["design_ids"] == [a["id"], b["id"]],
              "manifest payload design_ids sorted")
        check(manifest["design_count"] == 2, "manifest payload design_count")
        check([it["design_id"] for it in manifest["items"]] == [a["id"], b["id"]],
              "items ordered by ascending design id")

        item = manifest["items"][0]
        params = item["parameters"]
        for field in ("name", "cyclic", "max_float", "shafts", "treadles",
                      "warp_ends", "weft_picks", "threading", "tie_up",
                      "treadling"):
            check(params[field] == a[field],
                  f"archived parameter {field} matches stored design")
        check("id" not in params and "created_at" not in params,
              "item parameters exclude storage metadata (id/created_at)")
        check(item["audit"] == a["audit"], "archived audit matches stored audit")

        # digest 可独立复算：对同样的 manifest 规范化字节做 SHA-256
        expected = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        check(data["digest"] == expected,
              "digest equals sha256 of canonical manifest bytes")

        # 规范化：键排序、无多余结构空白（数据值内空格，如设计名，保留）
        with sqlite3.connect(_TMP_DB.name) as conn:
            stored_text = conn.execute(
                "SELECT manifest FROM design_archives WHERE id = ?",
                (data["id"],),
            ).fetchone()[0]
        check('": ' not in stored_text and ', ' not in stored_text and "\n" not in stored_text,
              "canonical text uses compact separators (no structural whitespace)")
        parsed = json.loads(stored_text)
        check(parsed == manifest, "persisted canonical text parses back to manifest")
        # 键按字典序：顶层键 design_count 排在 format 之前
        check(stored_text.startswith('{"design_count":2,'),
              f"canonical text keys sorted lexicographically: {stored_text[:40]!r}")
        # 清单内条目按 ID 升序：交换两个条目会改变摘要（顺序有意义）
        shuffled = json.loads(json.dumps(manifest))
        shuffled["items"][0], shuffled["items"][1] = (
            shuffled["items"][1], shuffled["items"][0]
        )
        check(hashlib.sha256(canonical_bytes(shuffled)).hexdigest() != expected,
              "sanity: swapped item order changes digest (order is significant)")


def test_digest_idempotent_and_order_independent():
    with TestClient(app) as client:
        a = _create(client, TWILL)
        b = _create(client, PLAIN)

        first = client.post(
            "/design-archives", json={"design_ids": [a["id"], b["id"]]}
        ).json()
        rows_after_create = _archive_rows()

        # 同一集合、不同顺序 -> 同 digest，复用同一记录，HTTP 200
        resp = client.post(
            "/design-archives", json={"design_ids": [b["id"], a["id"]]}
        )
        check(resp.status_code == 200, "identical content re-archived returns 200")
        second = resp.json()
        check(second["reused"] is True, "reuse reports reused=true")
        check(second["id"] == first["id"], "reuse returns same archive id")
        check(second["digest"] == first["digest"], "reuse returns same digest")
        check(second["created_at"] == first["created_at"],
              "reused archive keeps original created_at")
        check(second["manifest"] == first["manifest"],
              "reused archive keeps identical manifest")
        check(_archive_rows() == rows_after_create,
              "idempotent reuse inserts no new row")

        # 不同集合（单子集）-> 不同 digest，独立新记录
        third = client.post(
            "/design-archives", json={"design_ids": [a["id"]]}
        ).json()
        check(third["digest"] != first["digest"],
              "different id set produces a different digest")
        check(third["id"] != first["id"], "different content gets a new archive")

        # 重复提交单个 ID 仍然复用
        again = client.post(
            "/design-archives", json={"design_ids": [a["id"]]}
        )
        check(again.status_code == 200 and again.json()["id"] == third["id"],
              "repeated single-id archive is idempotent too")


def test_duplicate_ids_rejected():
    with TestClient(app) as client:
        a = _create(client, TWILL)["id"]
        rows_before = _archive_rows()

        resp = client.post(
            "/design-archives", json={"design_ids": [a, a]}
        )
        check(resp.status_code == 400, "duplicate ids return 400")
        check("duplicate" in resp.json()["detail"], "400 detail mentions duplicates")

        # 多次重复（如 [x, y, x]）同样拒绝，不写库
        b = _create(client, PLAIN)["id"]
        resp = client.post(
            "/design-archives", json={"design_ids": [a, b, a]}
        )
        check(resp.status_code == 400, "triple list with duplicate returns 400")
        check(_archive_rows() == rows_before,
              "rejected duplicate request writes no archive row")


def test_missing_designs_rejected():
    with TestClient(app) as client:
        a = _create(client, TWILL)["id"]
        rows_before = _archive_rows()

        resp = client.post(
            "/design-archives", json={"design_ids": [a, 99999]}
        )
        check(resp.status_code == 404, "unknown design id returns 404")
        check("99999" in resp.json()["detail"], "404 detail lists missing id")

        resp = client.post(
            "/design-archives", json={"design_ids": [88888, 99999]}
        )
        check(resp.status_code == 404, "all-unknown design ids return 404")
        detail = resp.json()["detail"]
        check("88888" in detail and "99999" in detail,
              "404 detail lists every missing id in ascending order")

        check(_archive_rows() == rows_before,
              "failed archive (missing designs) leaves no half-finished row")


def test_pydantic_validation_422():
    with TestClient(app) as client:
        a = _create(client, TWILL)["id"]
        rows_before = _archive_rows()

        for label, body in (
            ("empty list", {"design_ids": []}),
            ("51 ids", {"design_ids": [a] + list(range(1000, 1050))}),
            ("missing field", {}),
            ("string id", {"design_ids": ["1"]}),
            ("float id", {"design_ids": [1.0]}),
            ("zero id", {"design_ids": [0]}),
            ("negative id", {"design_ids": [-3]}),
            ("boolean id", {"design_ids": [True]}),
            ("null element", {"design_ids": [None]}),
        ):
            resp = client.post("/design-archives", json=body)
            check(resp.status_code == 422, f"{label} -> 422 (got {resp.status_code})")

        check(_archive_rows() == rows_before,
              "422 rejections never persist an archive")

    # 边界：恰好 50 个**不同**的已入库 ID 合法
    with TestClient(app) as client:
        ids = [_create(client, TWILL)["id"] for _ in range(50)]
        resp = client.post("/design-archives", json={"design_ids": ids})
        check(resp.status_code == 201, "exactly 50 distinct ids accepted (201)")
        check(resp.json()["design_count"] == 50, "50-item archive count")


def test_verify_fresh_archive_consistent():
    with TestClient(app) as client:
        a = _create(client, TWILL)
        b = _create(client, PLAIN)
        created = client.post(
            "/design-archives", json={"design_ids": [a["id"], b["id"]]}
        ).json()

        resp = client.get(f"/design-archives/{created['id']}/verify")
        check(resp.status_code == 200, "verify returns 200")
        report = resp.json()
        check(report["archive_id"] == created["id"], "verify echoes archive id")
        check(report["digest"] == created["digest"], "verify reports stored digest")
        check(report["recomputed_digest"] == created["digest"],
              "fresh archive recomputes identical digest")
        check(report["consistent"] is True, "fresh archive is consistent")
        check(report["missing_design_ids"] == [], "no missing designs")
        check(report["first_diff_path"] is None, "no diff path when identical")

        check(client.get("/design-archives/99999/verify").status_code == 404,
              "verify of unknown archive returns 404")


def test_verify_detects_deleted_design():
    with TestClient(app) as client:
        a = _create(client, TWILL)
        b = _create(client, PLAIN)
        created = client.post(
            "/design-archives", json={"design_ids": [a["id"], b["id"]]}
        ).json()

        # API 不开放删除设计：直接在 SQLite 中删除 a（audits 外键级联，
        # 归档记录独立保留），模拟归档后设计项缺失。
        with sqlite3.connect(_TMP_DB.name) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM designs WHERE id = ?", (a["id"],))

        report = client.get(f"/design-archives/{created['id']}/verify").json()
        check(report["consistent"] is False, "archive with deleted design inconsistent")
        check(report["missing_design_ids"] == [a["id"]],
              "verify reports the deleted design id")
        check(report["recomputed_digest"] != report["digest"],
              "recomputed digest differs after design removed")
        check(report["first_diff_path"] is not None,
              "a structural diff path is reported")
        # 规范键序下 design_count（2->1）先于 items 出现
        check(report["first_diff_path"] == "/design_count",
              f"first diff points at design_count, got {report['first_diff_path']!r}")

        # 归档记录本身仍然存在且清单原文未被改动
        stored = db.get_archive(created["id"])
        check(stored is not None, "archive row survives deletion of a member design")
        check(stored["digest"] == created["digest"],
              "verify is read-only: stored digest untouched")


def test_verify_detects_drift_first_diff_path():
    with TestClient(app) as client:
        a = _create(client, TWILL)
        created = client.post(
            "/design-archives", json={"design_ids": [a["id"]]}
        ).json()

        # 直接改库模拟归档后设计参数漂移（max_float 2 -> 5）。
        with sqlite3.connect(_TMP_DB.name) as conn:
            conn.execute(
                "UPDATE designs SET max_float = 5 WHERE id = ?", (a["id"],)
            )

        report = client.get(f"/design-archives/{created['id']}/verify").json()
        check(report["consistent"] is False, "drifted design breaks consistency")
        check(report["missing_design_ids"] == [], "no designs missing")
        check(report["first_diff_path"] == "/items/0/parameters/max_float",
              f"first diff drills to the changed parameter, "
              f"got {report['first_diff_path']!r}")

        # 再改审计字段：差异仍定位到首个不同的叶子（参数已先不同）。
        # 恢复参数后差异应移动到 audit。
        with sqlite3.connect(_TMP_DB.name) as conn:
            conn.execute(
                "UPDATE designs SET max_float = 2 WHERE id = ?", (a["id"],)
            )
            conn.execute(
                "UPDATE audits SET exceeds = 1 WHERE design_id = ?", (a["id"],)
            )
        report = client.get(f"/design-archives/{created['id']}/verify").json()
        check(report["first_diff_path"] == "/items/0/audit/exceeds",
              f"first diff moves into audit leaf, got {report['first_diff_path']!r}")

        # 校验本身只读：再跑一次结果稳定
        again = client.get(f"/design-archives/{created['id']}/verify").json()
        check(again["first_diff_path"] == report["first_diff_path"]
              and again["consistent"] == report["consistent"],
              "verify is a pure read: repeated reports identical")


def test_verify_detects_corrupted_manifest_json():
    """manifest 列被改写成非法 JSON 时不得 500：返回 200、consistent=false、
    first_diff_path=null。修复前 json.loads 在 db 层直接抛异常 -> 500。"""
    with TestClient(app) as client:
        a = _create(client, TWILL)
        created = client.post(
            "/design-archives", json={"design_ids": [a["id"]]}
        ).json()

        _corrupt_archive(created["id"], manifest="{not valid json")
        snapshot = _archive_row_raw(created["id"])
        resp = client.get(f"/design-archives/{created['id']}/verify")
        check(resp.status_code == 200,
              f"invalid manifest JSON returns 200, not 500 (got {resp.status_code})")
        report = resp.json()
        check(report["consistent"] is False,
              "unparseable manifest cannot be consistent")
        check(report["first_diff_path"] is None,
              "no diff path when manifest cannot be parsed")
        # design_ids 列仍完好：仍可按当前数据重算（且当前设计未变）
        check(report["missing_design_ids"] == [], "designs still present")
        check(report["recomputed_digest"] == created["digest"],
              "recomputed digest still available from design_ids column")
        check(report["digest"] == created["digest"], "stored digest echoed")

        # 校验只读：verify 之后归档行（含被篡改的内容）原样保留
        check(_archive_row_raw(created["id"]) == snapshot,
              "verify is read-only against a corrupt archive")


def test_verify_detects_rewritten_manifest_without_design_change():
    """manifest 文本被改写（digest 列同步改成新文本的摘要）但设计未变时，
    旧逻辑只比较「重算 digest == digest 列」会误报 consistent=true；新逻辑
    额外校验原始文本规范化与摘要，并与当前快照清单逐路径比较。"""
    with TestClient(app) as client:
        a = _create(client, TWILL)
        created = client.post(
            "/design-archives", json={"design_ids": [a["id"]]}
        ).json()

        # 值级篡改：把清单内 max_float 2 -> 5，保持紧凑规范格式，digest 列
        # 同步改成新文本的摘要（design_ids / design_count 列不动）。
        forged = json.loads(json.dumps(created["manifest"]))
        forged["items"][0]["parameters"]["max_float"] = 5
        forged_text = canonical_bytes(forged).decode("utf-8")
        forged_digest = hashlib.sha256(forged_text.encode("utf-8")).hexdigest()
        check(forged_digest != created["digest"], "forgery changes the digest")
        _corrupt_archive(
            created["id"], digest=forged_digest, manifest=forged_text
        )

        report = client.get(
            f"/design-archives/{created['id']}/verify"
        ).json()
        check(report["consistent"] is False,
              "rewritten manifest is inconsistent even when designs unchanged")
        check(report["digest"] == forged_digest,
              "endpoint reports the tampered stored digest")
        check(report["recomputed_digest"] == created["digest"],
              "recomputed digest from current designs is the original one")
        check(report["missing_design_ids"] == [], "no designs missing")
        check(report["first_diff_path"]
              == "/items/0/parameters/max_float",
              f"content diff located at changed leaf, got {report['first_diff_path']!r}")

        # 纯格式篡改：同样的清单内容重排成非规范化文本（带缩进空白），
        # digest 同步改成新文本的摘要——语义相同但落盘字节已不是 canonical。
        created2 = client.post(
            "/design-archives", json={"design_ids": [a["id"]]}
        ).json()
        pretty = json.dumps(created2["manifest"], indent=2, sort_keys=True)
        _corrupt_archive(
            created2["id"],
            digest=hashlib.sha256(pretty.encode("utf-8")).hexdigest(),
            manifest=pretty,
        )
        report = client.get(
            f"/design-archives/{created2['id']}/verify"
        ).json()
        check(report["consistent"] is False,
              "non-canonical manifest text breaks consistency")
        check(report["first_diff_path"] is None,
              "semantically identical text yields no content diff path")
        check(report["recomputed_digest"] == created2["digest"],
              "recomputed digest equals the canonical content digest")


def test_verify_detects_digest_column_tampering():
    """仅 digest 列被改写（manifest 原样、设计未变）时必须判不一致。"""
    with TestClient(app) as client:
        a = _create(client, TWILL)
        created = client.post(
            "/design-archives", json={"design_ids": [a["id"]]}
        ).json()

        _corrupt_archive(created["id"], digest="0" * 64)
        report = client.get(
            f"/design-archives/{created['id']}/verify"
        ).json()
        check(report["consistent"] is False,
              "digest column mismatch -> inconsistent")
        check(report["recomputed_digest"] == created["digest"],
              "recomputed digest still equals the real manifest digest")
        check(report["missing_design_ids"] == [], "no designs missing")
        check(report["first_diff_path"] is None,
              "manifest content unchanged -> no content diff")


def test_verify_detects_design_count_column_mismatch():
    """design_count 列与清单实际数量不符（清单未变、设计未变）时不得误报。"""
    with TestClient(app) as client:
        a = _create(client, TWILL)
        b = _create(client, PLAIN)
        created = client.post(
            "/design-archives", json={"design_ids": [a["id"], b["id"]]}
        ).json()

        _corrupt_archive(created["id"], design_count=1)
        report = client.get(
            f"/design-archives/{created['id']}/verify"
        ).json()
        check(report["consistent"] is False,
              "design_count column lying about count -> inconsistent")
        check(report["first_diff_path"] is None,
              "manifest itself intact -> no manifest-vs-current diff path")
        check(report["recomputed_digest"] == created["digest"],
              "recomputation unaffected: manifest and ids still intact")

        # design_ids 列与清单不一致同理
        _corrupt_archive(
            created["id"], design_count=2, design_ids=json.dumps([a["id"]])
        )
        report = client.get(
            f"/design-archives/{created['id']}/verify"
        ).json()
        check(report["consistent"] is False,
              "design_ids column mismatch -> inconsistent")
        check(report["missing_design_ids"] == [],
              "tampered id list still refers to an existing design")


def test_verify_detects_manifest_internal_count_mismatch():
    """manifest 内部 design_count / items 数量与 design_ids 不符时判不一致；
    结构虽可解析，差异仍可逐路径定位到当前清单。"""
    with TestClient(app) as client:
        a = _create(client, TWILL)
        created = client.post(
            "/design-archives", json={"design_ids": [a["id"]]}
        ).json()

        forged = json.loads(json.dumps(created["manifest"]))
        forged["design_count"] = 2  # design_ids/items 仍是 1 项
        forged_text = canonical_bytes(forged).decode("utf-8")
        _corrupt_archive(
            created["id"],
            digest=hashlib.sha256(forged_text.encode("utf-8")).hexdigest(),
            manifest=forged_text,
        )
        report = client.get(
            f"/design-archives/{created['id']}/verify"
        ).json()
        check(report["consistent"] is False,
              "manifest with internal count mismatch -> inconsistent")
        # 规范键序下 design_count 先于 design_ids/items
        check(report["first_diff_path"] == "/design_count",
              f"diff located at /design_count, got {report['first_diff_path']!r}")


def test_verify_survives_unparseable_design_ids_column():
    """design_ids 列也是非法 JSON 时仍不得 500：manifest 结构可用则退回用
    清单内 ID 重算；连 manifest 都无法解析时 recomputed_digest=null。"""
    with TestClient(app) as client:
        a = _create(client, TWILL)
        created = client.post(
            "/design-archives", json={"design_ids": [a["id"]]}
        ).json()

        _corrupt_archive(created["id"], design_ids="??not json??")
        resp = client.get(f"/design-archives/{created['id']}/verify")
        check(resp.status_code == 200,
              "broken design_ids column does not cause 500")
        report = resp.json()
        check(report["consistent"] is False,
              "archive with broken design_ids column is inconsistent")
        check(report["recomputed_digest"] == created["digest"],
              "falls back to design_ids inside the intact manifest")
        check(report["missing_design_ids"] == [],
              "fallback id list still resolves to the present design")

        # 两列皆毁：安全降级，不抛异常
        _corrupt_archive(created["id"], manifest="###")
        resp = client.get(f"/design-archives/{created['id']}/verify")
        check(resp.status_code == 200,
              "both manifest and design_ids broken -> still 200, not 500")
        report = resp.json()
        check(report["consistent"] is False and report["first_diff_path"] is None,
              "fully corrupted row -> inconsistent without diff path")
        check(report["recomputed_digest"] is None,
              "recomputed_digest null when no id list can be recovered")
        check(report["missing_design_ids"] == [],
              "missing list empty when it cannot be computed")


def test_batch_read_pinned_to_single_sqlite_snapshot():
    """并发回归：另一连接（模拟另一服务实例 / 维护进程）在批量读取的两条查询
    之间提交「改设计 + 改审计 + 删设计」时，POST 归档的整批读取必须固定在
    同一个 SQLite 读取快照上——参数与审计同属旧版本、缺失判定也基于旧快照，
    绝不拼出新旧混合的清单或误报缺失；GET .../verify 共用同一语义。

    用 WAL 才能在服务端读事务打开期间让写者真正提交（DELETE journal 模式下
    写者会被阻塞到读事务结束，同样安全但无法演示中途提交）。
    """
    with sqlite3.connect(_TMP_DB.name) as conn:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    check(mode.lower() == "wal", "test database switched to WAL for the race window")

    try:
        with TestClient(app) as client:
            a = _create(client, TWILL)
            b = _create(client, PLAIN)

            fired = {"count": 0}
            real_connect = sqlite3.connect

            def maintenance_commit():
                """模拟维护进程：在批量读取中途提交一揽子修改 / 删除。"""
                writer = sqlite3.connect(_TMP_DB.name, timeout=10)
                writer.execute("PRAGMA foreign_keys = ON")
                writer.execute(
                    "UPDATE designs SET max_float = 5 WHERE id = ?", (a["id"],)
                )
                writer.execute(
                    "UPDATE audits SET exceeds = 1 WHERE design_id = ?", (a["id"],)
                )
                writer.execute("DELETE FROM designs WHERE id = ?", (b["id"],))
                writer.commit()
                writer.close()
                fired["count"] += 1

            def patched_connect(path, *args, **kwargs):
                conn = real_connect(path, *args, **kwargs)
                if str(path) == _TMP_DB.name and fired["count"] == 0:
                    def trace(sql):
                        # 在批量读取的「审计查询」即将执行时触发：此时设计行已按
                        # 旧快照读出，审计行与缺失判定若不共享快照就会读到新版本。
                        if fired["count"] == 0 and " FROM audits" in sql:
                            maintenance_commit()
                    conn.set_trace_callback(trace)
                return conn

            sqlite3.connect = patched_connect
            try:
                resp = client.post(
                    "/design-archives", json={"design_ids": [a["id"], b["id"]]}
                )
            finally:
                sqlite3.connect = real_connect

            check(fired["count"] == 1, "external writer committed during the batch read")
            check(resp.status_code == 201,
                  f"snapshot still contains both designs -> 201, got {resp.status_code} "
                  f"{resp.text}")
            data = resp.json()
            check(data["design_ids"] == [a["id"], b["id"]],
                  "missing-id judgement pinned to the pre-change snapshot (both ids kept)")
            item_a = next(it for it in data["manifest"]["items"] if it["design_id"] == a["id"])
            check(item_a["parameters"]["max_float"] == a["max_float"],
                  "parameters read from the pre-change snapshot (not spliced)")
            check(item_a["audit"]["exceeds"] == a["audit"]["exceeds"],
                  "audit row read from the SAME pre-change snapshot (not spliced)")
            check(len(data["manifest"]["items"]) == 2,
                  "manifest keeps both items from one coherent snapshot")

            # 外部提交此刻早已落盘：verify 在自己的一个快照内重读当前数据，
            # 报告必须自洽——a 漂移、b 缺失，首个差异为 design_count（2->1），
            # 而不是审计 / 参数忽新忽旧造成的假不一致定位。
            report = client.get(
                f"/design-archives/{data['id']}/verify"
            ).json()
            check(report["consistent"] is False, "verify flags post-snapshot drift")
            check(report["missing_design_ids"] == [b["id"]],
                  "verify snapshot sees the deleted design as missing")
            check(report["first_diff_path"] == "/design_count",
                  f"verify compares two coherent snapshots, got {report['first_diff_path']!r}")
            again = client.get(f"/design-archives/{data['id']}/verify").json()
            check(again["consistent"] == report["consistent"]
                  and again["first_diff_path"] == report["first_diff_path"],
                  "verify stays stable on repeat (pure read)")
    finally:
        # 还原日志模式，避免给同库后续测试留下 -wal/-shm 状态
        with sqlite3.connect(_TMP_DB.name) as conn:
            conn.execute("PRAGMA journal_mode=DELETE")


def test_openapi_documents_archive_routes():
    schema = app.openapi()
    paths = schema["paths"]
    check("/design-archives" in paths, "OpenAPI documents POST /design-archives")
    check("post" in paths["/design-archives"], "archive POST method documented")
    verify_path = "/design-archives/{archive_id}/verify"
    check(verify_path in paths and "get" in paths[verify_path],
          "OpenAPI documents GET .../verify")
    post_responses = paths["/design-archives"]["post"]["responses"]
    for code in ("200", "201", "400", "404"):
        check(code in post_responses, f"POST documents {code} response")
    check("DesignArchiveIn" in schema["components"]["schemas"],
          "DesignArchiveIn schema published")
    check("DesignArchiveVerifyOut" in schema["components"]["schemas"],
          "DesignArchiveVerifyOut schema published")


if __name__ == "__main__":
    test_create_archive_manifest_and_digest()
    test_digest_idempotent_and_order_independent()
    test_duplicate_ids_rejected()
    test_missing_designs_rejected()
    test_pydantic_validation_422()
    test_verify_fresh_archive_consistent()
    test_verify_detects_deleted_design()
    test_verify_detects_drift_first_diff_path()
    test_verify_detects_corrupted_manifest_json()
    test_verify_detects_rewritten_manifest_without_design_change()
    test_verify_detects_digest_column_tampering()
    test_verify_detects_design_count_column_mismatch()
    test_verify_detects_manifest_internal_count_mismatch()
    test_verify_survives_unparseable_design_ids_column()
    test_batch_read_pinned_to_single_sqlite_snapshot()
    test_openapi_documents_archive_routes()
    try:
        os.unlink(_TMP_DB.name)
    except OSError:
        pass
    for suffix in ("-wal", "-shm"):
        try:
            os.unlink(_TMP_DB.name + suffix)
        except OSError:
            pass
    print("all design-archives API tests passed")
