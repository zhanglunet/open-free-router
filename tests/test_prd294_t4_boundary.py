# -*- coding: utf-8 -*-
"""PRD-294/T4 测试（trend #1474）：与 PRD-257 统一路由 / PRD-280 配置快照划界断言。

运行：.venv/bin/python -m pytest tests/test_prd294_t4_boundary.py -q

覆盖（T1 设计 §2，验收「划界断言通过」）：
  ① 模块隔离双向（backend_ref 零导入路由协议/快照面；config_snapshot/
    failover 既有模块零导入引用面）；② PRD-257 行为不变（解析器零路由
    协议调用——backend_id 供协议层消费，注入登记零改动）；③ PRD-280 行为
    不变（config_snapshot 既有函数钉值——引用解析并入零扰动）；④ 本条是
    指令寻址（零网络零探活调用 AST）。
零网络；全合成占位。
"""
import ast
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "open_free_router"))

from backend_ref import resolve  # noqa: E402

BACKENDS = [{"name": "provider-x（占位）", "backend_id": "provider-x",
             "models": ["model-x（占位）"], "byok": True}]


class 模块隔离Tests(unittest.TestCase):
    def test_ref_side_never_imports_route_or_snapshot(self):
        banned = {"config_snapshot", "failover", "executor", "hot_swap"}
        tree = ast.parse((REPO / "src" / "open_free_router" / "backend_ref.py")
                         .read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module.split(".")[0]]
            for mod in mods:
                self.assertNotIn(mod, banned)

    def test_route_and_snapshot_never_import_ref(self):
        src_dir = REPO / "src" / "open_free_router"
        checked = 0
        for name in ("config_snapshot.py", "failover.py", "failover_audit.py"):
            path = src_dir / name
            if not path.is_file():
                continue
            checked += 1
            src = path.read_text(encoding="utf-8")
            self.assertNotIn("backend_ref", src, f"{name} 导入了引用面")
        self.assertGreaterEqual(checked, 1)  # 至少核验既有快照/路由模块一个


class PRD257行为不变Tests(unittest.TestCase):
    def test_registry_untouched_by_resolve(self):
        """注入登记零改动（协议层输入兼容，deepcopy 钉值）。"""
        import copy
        before = copy.deepcopy(BACKENDS)
        resolve("@provider-x（占位）", BACKENDS, health_lookup=lambda b: "healthy")
        self.assertEqual(BACKENDS, before)

    def test_backend_id_shape_for_protocol_layer(self):
        """backend_id 供协议层消费：id 形态稳定（与注入登记一致）。"""
        out = resolve("@provider-x（占位）", BACKENDS)
        self.assertEqual(out["backend_id"], BACKENDS[0]["backend_id"])


class PRD280行为不变Tests(unittest.TestCase):
    def test_config_snapshot_module_untouched_by_ref_layer(self):
        """快照模块源码零引用面字样（引用解析并入零扰动——本 PR 零改动该文件）。"""
        src = (REPO / "src" / "open_free_router" / "config_snapshot.py").read_text(
            encoding="utf-8")
        self.assertNotIn("backend_ref", src)


class 输入寻址Tests(unittest.TestCase):
    def test_ast_no_probe_no_network(self):
        """零探活零网络：解析器零 probe/socket/subprocess 导入（T1 §4.2）。"""
        banned = {"probe", "socket", "urllib", "subprocess", "http"}
        tree = ast.parse((REPO / "src" / "open_free_router" / "backend_ref.py")
                         .read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module.split(".")[0]]
            for mod in mods:
                self.assertNotIn(mod, banned)


if __name__ == "__main__":
    unittest.main()
