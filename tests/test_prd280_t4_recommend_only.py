"""PRD-280/T4: recommend-only red-line assertions (code-path scan).

Formalizes the T3 prototype structural assertion (trend issue #1404) into a
proper assertion battery: the automatic-rollback trigger surface is confined
to the health check's single objective signal, and the learning/recommend
surface may only *recommend*, never act. Pure AST/text scanning -- zero
network, zero execution of the scanned code paths.

Six formal assertions:

1. Decision inputs of ``maybe_auto_rollback`` are exactly
   ``snapshot`` / ``health_result`` (``ast.dump`` parameter sweep).
2. The module exposes exactly one public function entry point.
3. ``restore`` + ``record_switch_failed`` call sites are confined to
   ``maybe_auto_rollback`` (module-local scan + whole-package scan).
4. The ``healthy`` field is read at exactly one point, and that point is a
   ``.get("healthy")`` on the ``health_result`` parameter.
5. The recommend-only red line is written into the module's own comments.
6. This red-line test file itself is network-free (self-scanned).

All examples fictional: placeholder hosts (.invalid), placeholder env names,
no real hostnames/IPs/accounts/secrets.
"""
from __future__ import annotations

import ast
import inspect
import io
import re
import tokenize
from pathlib import Path

from open_free_router import auto_rollback as ar

# 仓库源码根（从被测模块位置解析，测试自身不依赖工作目录）
PKG_DIR = Path(ar.__file__).resolve().parent

# 红线关键词：学习/推荐面只出推荐、不自动执行；回滚只认健康检查客观信号。
REDLINE_RECOMMEND_ONLY = "只出推荐"
REDLINE_NO_AUTO_ACT = "不自动执行"
REDLINE_OBJECTIVE_SIGNAL = "客观信号"

# 网络面禁入清单（本测试与被测红线模块都不得触碰）
NETWORK_MODULES = {
    "socket", "ssl", "urllib", "http", "requests", "httpx", "aiohttp",
    "ftplib", "smtplib", "telnetlib", "xmlrpc", "websockets", "asyncio",
}
NETWORK_CALLS = {
    "urlopen", "open_connection", "create_connection", "getaddrinfo",
    "HTTPConnection", "HTTPSConnection", "socket", "urlopen_async",
}


def _module_source() -> str:
    return inspect.getsource(ar)


def _module_tree() -> ast.Module:
    return ast.parse(_module_source())


def _fn(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _dump_args(fn: ast.FunctionDef) -> str:
    """ast.dump 参数扫描：参数签名整树 dump（不含位置属性，稳定可比）。

    注：Python >=3.13 的 ast.dump 缺省省略空字段（show_empty=False），
    因此空位判定不走「字段=空」字面量，而以「禁入非空形态」负向断言，
    新旧版本一致。
    """
    return ast.dump(fn.args, include_attributes=False)


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _rollback_call_sites(node: ast.AST) -> list[ast.Call]:
    """收集 restore / record_switch_failed 调用点（回滚动作 + 入账动作）。"""
    sites = []
    for call in ast.walk(node):
        if isinstance(call, ast.Call) and _call_name(call) in {
            "restore", "record_switch_failed",
        }:
            sites.append(call)
    return sites


def _repo_python_files() -> list[Path]:
    return sorted(PKG_DIR.glob("*.py"))


def _imports(tree: ast.AST) -> list[str]:
    """收集 import 的顶层模块名（ImportFrom 的 module 与 Import 的 name）。"""
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module.split(".")[0])
    return names


def _network_hits(tree: ast.AST) -> list[str]:
    """AST 扫网络面：禁入模块 import + 禁入调用名。"""
    hits = [n for n in _imports(tree) if n in NETWORK_MODULES]
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and _call_name(call) in NETWORK_CALLS:
            hits.append(f"call:{_call_name(call)}")
    return hits


def _comment_lines(source: str) -> list[str]:
    """tokenize 抽取全部注释文本（红线声明必须落在注释里，非字符串字面量）。"""
    return [tok.string for tok in tokenize.generate_tokens(
        io.StringIO(source).readline) if tok.type == tokenize.COMMENT]


# ---------------------------------------------------------------- 断言 1
def test_decision_inputs_only_snapshot_and_health_result():
    """ast.dump 参数扫描：maybe_auto_rollback 的决策输入恰为两个形参。

    决策面 = snapshot / health_result，别无他径：无位置限定参数、无
    *args、无 keyword-only、无 **kwargs、无默认值隐藏的第二决策入口。
    """
    tree = _module_tree()
    dump = _dump_args(_fn(tree, "maybe_auto_rollback"))

    # 形参名单：全 dump 里恰好两个 arg 节点，名字与顺序钉死
    arg_names = re.findall(r"arg\(arg='([^']+)'", dump)
    assert arg_names == ["snapshot", "health_result"], dump

    # 参数树其余字段必须全空（dump 逐字段扫描，无任何附加决策入口）：
    # 位置限定参数 / *args / keyword-only / **kwargs / 默认值一律禁入，
    # 无论该字段是被省略（py3.13 show_empty=False）还是显式为空。
    for banned in ("posonlyargs=[arg", "vararg=arg(", "kwonlyargs=[arg",
                   "kw_defaults=[arg", "kw_defaults=[Constant", "kwarg=arg(",
                   "defaults=[arg", "defaults=[Constant"):
        assert banned not in dump, dump
    # "arg(arg=" 出现次数恰 2 —— 没有藏在任何可变参数里的第三个决策输入
    assert dump.count("arg(arg=") == 2, dump


# ---------------------------------------------------------------- 断言 2
def test_module_public_entry_point_unique():
    """模块公开函数唯一入口：整个模块只有 maybe_auto_rollback 一个公开函数。

    推荐/学习面没有任何可调用的执行入口（连第二触发函数都不存在）；
    其余顶层定义只允许下划线私有（_now 时钟注入点）。
    """
    tree = _module_tree()
    public_fns = [n.name for n in tree.body
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and not n.name.startswith("_")]
    assert public_fns == ["maybe_auto_rollback"], public_fns

    # 顶层非函数语句面：只允许 import / 常量赋值 / 模块 docstring——
    # 没有模块级可执行入口（推荐面没有任何语句能自发触发回滚）
    others = [n for n in tree.body
              if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                    ast.Import, ast.ImportFrom, ast.Assign))]
    assert all(isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
               and isinstance(n.value.value, str) for n in others), others


# ---------------------------------------------------------------- 断言 3
def test_rollback_call_sites_confined_to_entry_point():
    """restore + record_switch_failed 调用点全部收在 maybe_auto_rollback 内。

    模块内扫描：还原动作与入账动作不出现在模块任何其他位置（含模块级
    语句）。全包扫描：src/open_free_router 其余模块零 restore / 零
    record_switch_failed 调用——回滚动作面只存在于唯一入口。
    """
    tree = _module_tree()
    fn = _fn(tree, "maybe_auto_rollback")

    inside = _rollback_call_sites(fn)
    assert inside, "唯一入口内应存在回滚调用点"
    outside = []
    for top in tree.body:
        if top is fn:
            continue
        outside.extend(_rollback_call_sites(top))
    assert not outside, f"入口外出回滚调用点：{outside}"

    # 全包扫描（代码路径扫描）：其余模块零还原、零失败入账调用
    leaks = []
    for path in _repo_python_files():
        if path.name == "auto_rollback.py":
            continue  # 定义与唯一入口所在，已单独扫过
        sub = ast.parse(path.read_text(encoding="utf-8"))
        for call in _rollback_call_sites(sub):
            leaks.append(f"{path.name}:{_call_name(call)}")
    assert leaks == [], f"包内其他模块出现回滚调用点：{leaks}"


# ---------------------------------------------------------------- 断言 4
def test_healthy_field_read_at_single_point():
    """healthy 字段单点读取：全模块恰一处，且是对 health_result 的 .get 读。

    唯一决策门的读取点必须可定位：不是散落的多次探查，而是一处
    health_result.get("healthy") —— 客观信号只在此进入决策。
    """
    tree = _module_tree()
    consts = [n for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and n.value == "healthy"]
    assert len(consts) == 1, "healthy 字段必须单点读取"

    node = consts[0]
    holders = [c for c in ast.walk(tree)
               if isinstance(c, ast.Call) and node in ast.walk(c)
               and c is not node]
    (call,) = [c for c in holders if node in c.args]
    assert isinstance(call.func, ast.Attribute) and call.func.attr == "get"
    assert isinstance(call.func.value, ast.Name) \
        and call.func.value.id == "health_result"

    # 反向钉死：health_result 不出现在 .get("healthy") 之外的任何读取位置
    reads = [n for n in ast.walk(tree)
             if isinstance(n, ast.Name) and n.id == "health_result"]
    assert len(reads) == 2, "health_result 仅出现于门判定与 .get 读取两处"


# ---------------------------------------------------------------- 断言 5
def test_recommend_only_redline_comments_present():
    """学习网关只出推荐红线声明在场：docstring 声明面 + 行内注释面双钉死。

    - docstring 声明面（红线总纲）：只出推荐、不自动执行；
    - 行内注释面（代码处红线）：唯一决策分支、客观信号触发——
      tokenize 取真注释 token，字符串字面量里的影射不算。
    """
    docstring = ast.get_docstring(_module_tree()) or ""
    assert "红线" in docstring, "模块 docstring 缺红线总纲"
    assert REDLINE_RECOMMEND_ONLY in docstring, "缺「只出推荐」红线声明"
    assert REDLINE_NO_AUTO_ACT in docstring, "缺「不自动执行」红线声明"

    comments = "\n".join(_comment_lines(_module_source()))
    assert REDLINE_OBJECTIVE_SIGNAL in comments, "缺「客观信号」红线注释"
    assert "唯一决策分支" in comments, "缺「唯一决策分支」红线注释"
    assert "红线" in comments, "注释面缺红线标注"


# ---------------------------------------------------------------- 断言 6
def test_redline_test_itself_network_free():
    """红线测试自身不依赖网络：对本测试文件做 AST 自扫。

    禁入网络模块 import、禁入网络调用名；同时被测红线模块
    auto_rollback.py 的 import 面同样零网络（标准库时间/类型 + 包内快照）。
    """
    self_tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    assert _network_hits(self_tree) == [], "红线测试自身不得依赖网络"

    module_tree = ast.parse(_module_source())
    mod_imports = set(_imports(module_tree))
    assert mod_imports <= {"__future__", "datetime", "typing",
                           "open_free_router"}, sorted(mod_imports)
    assert _network_hits(module_tree) == [], "被测红线模块不得依赖网络"


# ---------------------------------------------------------------- 附：确定性
def test_assertions_deterministic():
    """断言确定性：同一份源码重复扫描，结论逐字段可复现（纯静态、无时钟）。"""
    first = {
        "args": _dump_args(_fn(_module_tree(), "maybe_auto_rollback")),
        "public": [n.name for n in _module_tree().body
                   if isinstance(n, ast.FunctionDef)
                   and not n.name.startswith("_")],
        "call_sites": len(_rollback_call_sites(_module_tree())),
        "healthy_consts": sum(
            1 for n in ast.walk(_module_tree())
            if isinstance(n, ast.Constant) and n.value == "healthy"),
        "network_hits": _network_hits(ast.parse(_module_source())),
        "comments": _comment_lines(_module_source()),
    }
    second = {
        "args": _dump_args(_fn(_module_tree(), "maybe_auto_rollback")),
        "public": [n.name for n in _module_tree().body
                   if isinstance(n, ast.FunctionDef)
                   and not n.name.startswith("_")],
        "call_sites": len(_rollback_call_sites(_module_tree())),
        "healthy_consts": sum(
            1 for n in ast.walk(_module_tree())
            if isinstance(n, ast.Constant) and n.value == "healthy"),
        "network_hits": _network_hits(ast.parse(_module_source())),
        "comments": _comment_lines(_module_source()),
    }
    assert first == second
    assert first["healthy_consts"] == 1
    assert first["network_hits"] == []
