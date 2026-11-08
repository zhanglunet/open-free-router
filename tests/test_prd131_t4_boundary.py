import ast, unittest
from pathlib import Path
MODULE = Path(__file__).resolve().parents[1] / "src/open_free_router/replay_gate.py"
ALLOWED = {"__future__", "hashlib", "json", "pathlib", "typing"}

class BoundaryTests(unittest.TestCase):
    def test_ast_whitelist(self):
        tree = ast.parse(MODULE.read_text())
        roots = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import): roots |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module: roots.add(n.module.split(".")[0])
        self.assertTrue(roots <= ALLOWED, f"越界: {roots - ALLOWED}")

if __name__ == "__main__":
    unittest.main()
