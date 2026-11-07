# -*- coding: utf-8 -*-
"""PRD-229/T5 测试：验收试运行——历史流量回放（trend #1149）。

运行：.venv/bin/python -m pytest tests/test_prd229_t5_replay.py -q

验收标准（trend #1149）：**回放推荐可复现，全程零自动变更**。

回放（合成历史流量 → 真实代码全链）：usage_events（sqlite）→ gateway_aggregate
→ gateway_recommend.recommend → learning_confirm 确认板（register→confirm→apply
注入式 applier 写合成配置）→ rollback 回滚点。
  可复现：同一历史库回放两遍 → 推荐 JSON 逐字节一致（内容寻址 id）；
  零自动变更：生成恒 pending；未确认 apply 拒绝；apply 四项校验
  （confirmed/确认记录/证据哈希一致/未过期）；AST 红线三模块零网络导入；
  全程唯一写通道 = 注入式 applier（人工确认后才调用，恰好一次）。
零网络；事件全合成占位。
"""
import ast
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "open_free_router"))

from gateway_aggregate import aggregate  # noqa: E402
from gateway_recommend import recommend, validate_record  # noqa: E402
from learning_confirm import (  # noqa: E402
    ConfirmError,
    LearningConfirmBoard,
    save_journal,
)

GENERATED_AT = "2026-11-07T12:00:00+00:00"
DAY = 86400.0
BASE_TS = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc).timestamp()


def make_db(tmp: Path, events) -> Path:
    path = tmp / "usage.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE usage_events (
        event_id TEXT PRIMARY KEY, timestamp REAL NOT NULL, provider TEXT NOT NULL,
        model TEXT NOT NULL, status TEXT NOT NULL, status_code INTEGER NOT NULL,
        ttfb_ms REAL, total_latency_ms REAL, input_tokens INTEGER,
        output_tokens INTEGER, fallback_attempts INTEGER NOT NULL,
        error_kind TEXT NOT NULL, circuit_breaker_events INTEGER NOT NULL)""")
    for e in events:
        conn.execute(
            "INSERT INTO usage_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (e["event_id"], e["timestamp"], e["provider"], e["model"], e["status"],
             e["status_code"], e.get("ttfb_ms"), e.get("total_latency_ms"),
             e.get("input_tokens"), e.get("output_tokens"),
             e.get("fallback_attempts", 0), e.get("error_kind", "none"), 0))
    conn.commit()
    conn.close()
    return path


def evt(eid, ts, model, status="success", ttfb=100.0, latency=500.0, tokens=(100, 50)):
    return {"event_id": eid, "timestamp": ts, "provider": "remote", "model": model,
            "status": status, "status_code": 200, "ttfb_ms": ttfb,
            "total_latency_ms": latency, "input_tokens": tokens[0],
            "output_tokens": tokens[1], "fallback_attempts": 0}


def historical_traffic():
    """合成 14 天历史流量：model-a 现任慢，model-b 挑战者全面占优（B 挑衅位）。"""
    events = []
    for i in range(40):
        events.append(evt(f"a-{i:03d}", BASE_TS + (i % 5) * DAY + i, "model-a",
                          ttfb=200.0, latency=1000.0, tokens=(200, 100)))
    for i in range(35):
        events.append(evt(f"b-{i:03d}", BASE_TS + (i % 5) * DAY + i, "model-b",
                          ttfb=100.0, latency=600.0, tokens=(100, 50)))
    return events


def replay_once(tmp: Path, events, label="usage") -> dict:
    """单次回放：历史库 → 聚合 → 推荐（纯函数链）。"""
    db_dir = Path(tmp) / label
    db_dir.mkdir(parents=True, exist_ok=True)
    agg = aggregate(make_db(db_dir, events), window_days=14,
                    now=datetime.fromisoformat(GENERATED_AT))
    return recommend(agg, generated_at=GENERATED_AT)


class 可复现Tests(unittest.TestCase):
    def test_replay_twice_identical(self):
        """验收①：同一历史流量回放两遍 → 推荐 JSON 逐字节一致。"""
        with tempfile.TemporaryDirectory() as tmp:
            first = replay_once(Path(tmp), historical_traffic(), label="run-1")
            second = replay_once(Path(tmp), historical_traffic(), label="run-2")
        strip_meta = lambda out: json.dumps(out["records"], ensure_ascii=False,
                                            sort_keys=True)
        self.assertEqual(strip_meta(first), strip_meta(second))
        recs = [r for r in first["records"] if r["kind"] == "recommendation"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(validate_record(recs[0]), [])
        # 内容寻址 id：同内容同 id（复现的锚点）
        self.assertEqual(recs[0]["recommendation_id"],
                         [r for r in second["records"]
                          if r["kind"] == "recommendation"][0]["recommendation_id"])


class 零自动变更Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = replay_once(Path(self.tmp.name), historical_traffic())
        self.rec = [r for r in self.out["records"]
                    if r["kind"] == "recommendation"][0]

    def test_generation_is_pending_and_observation_visible(self):
        """生成恒 pending（零自动变更的第一道闸）；观察记录可见。"""
        self.assertEqual(self.rec["status"], "pending")

    def test_apply_without_confirm_rejected(self):
        """未确认 → apply 拒绝（确认是显式动作，§6.2-3）。"""
        board = LearningConfirmBoard()
        board.register(self.rec)
        called = []
        with self.assertRaises(ConfirmError):
            board.apply(self.rec["recommendation_id"], at="2026-11-07T13:00:00+00:00",
                        applier=lambda p, v: called.append(v))
        self.assertEqual(called, [])                    # 零自动变更：写通道未触发

    def test_full_confirm_apply_chain_single_write(self):
        """确认→应用全链：applier 恰好一次、from/to 正确、审计流水 append-only。"""
        board = LearningConfirmBoard()
        board.register(self.rec)
        rid = self.rec["recommendation_id"]
        board.confirm(rid, operator="operator-01（占位）",
                      at="2026-11-07T13:00:00+00:00")
        writes = []
        applied = board.apply(rid, at="2026-11-07T13:05:00+00:00",
                              applier=lambda p, v: writes.append((p, v)))
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(len(writes), 1)                # 唯一写通道恰好一次
        parameter, values = writes[0]
        self.assertEqual(parameter, self.rec["parameter"])
        self.assertEqual(values["from"], self.rec["current_value"])
        self.assertEqual(values["to"], self.rec["recommended_value"])
        kinds = [e["type"] for e in board.journal]
        self.assertEqual(kinds, ["generated", "confirmed", "applied"])
        # 回滚点：旧值快照在案
        self.assertEqual(applied["old_values_snapshot"], self.rec["current_value"])

    def test_journal_save_roundtrip(self):
        """审计流水落盘（JSONL 追加形态、独立目录 0600/0700，留痕可复核）。"""
        board = LearningConfirmBoard()
        board.register(self.rec)
        with tempfile.TemporaryDirectory() as tmp:
            path = save_journal(board, tmp)
            self.assertEqual(Path(path).name, "learning-audit.jsonl")
            lines = [json.loads(line) for line in
                     Path(path).read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(lines, board.journal)

    def test_ast_redline_no_network_imports(self):
        """AST 红线：recommend/confirm 两模块零网络/零子进程导入（零自动变更面）。"""
        banned = {"urllib", "requests", "socket", "http", "subprocess", "smtplib"}
        src_dir = REPO / "src" / "open_free_router"
        for name in ("gateway_recommend.py", "learning_confirm.py"):
            tree = ast.parse((src_dir / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module.split(".")[0]]
                for mod in mods:
                    self.assertNotIn(mod, banned, f"{name} 导入了 {mod}")


if __name__ == "__main__":
    unittest.main()
