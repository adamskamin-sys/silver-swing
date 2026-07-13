"""Enforce the twitter_scanner shadow-mode invariant.

Adam's ask: "give it a try but don't execute any trades with it. I want to
see if it works first." This test guards that promise mechanically.

If EXECUTE_TRADES is ever flipped True, or if any order-placing symbol from
the broker layer gets imported into twitter_scanner, this test fails. That
forces an explicit code review before the shadow harness could ever become
an execution path.
"""

import ast
import pathlib


SCANNER_PATH = pathlib.Path(__file__).parent.parent / "twitter_scanner.py"


def test_execute_trades_flag_is_false():
    """The literal `EXECUTE_TRADES = False` line must exist in the module."""
    import twitter_scanner
    assert hasattr(twitter_scanner, "EXECUTE_TRADES"), \
        "twitter_scanner.EXECUTE_TRADES sentinel is missing"
    assert twitter_scanner.EXECUTE_TRADES is False, \
        "shadow-mode invariant violated — EXECUTE_TRADES must be False"


def test_no_broker_place_order_imports():
    """The scanner must not import any order-placing symbol from broker.
    Static AST check — catches accidental additions before runtime."""
    src = SCANNER_PATH.read_text()
    tree = ast.parse(src)
    forbidden_names = {
        "place_limit", "place_market", "place_order",
        "submit_order", "CoinbaseBroker", "PaperBroker",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name not in forbidden_names, (
                    f"twitter_scanner imports {alias.name} from {node.module} — "
                    f"shadow-mode invariant broken"
                )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "broker", \
                    "twitter_scanner imports broker module — shadow-mode broken"


def test_no_order_placing_calls_in_ast():
    """AST walk — no attribute call like `x.place_limit(...)` anywhere in
    the executable module (docstrings + comments naturally excluded since
    they don't appear in the AST)."""
    tree = ast.parse(SCANNER_PATH.read_text())
    forbidden = {"place_limit", "place_market", "place_order", "submit_order"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden, (
                f"twitter_scanner AST has a call to .{node.func.attr}(...) — "
                f"shadow-mode invariant broken"
            )
