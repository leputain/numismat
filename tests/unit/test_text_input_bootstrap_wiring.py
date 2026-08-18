import ast
import inspect
import textwrap

from finbot.adapters.telegram.routers.inputs import TextInputRouter
from finbot.bootstrap import build_dispatcher


def _text_input_handler() -> ast.AsyncFunctionDef:
    source = textwrap.dedent(inspect.getsource(TextInputRouter.text_input))
    tree = ast.parse(source)
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "text_input"
    )


def _handler_source() -> str:
    return textwrap.dedent(inspect.getsource(TextInputRouter.text_input))


def test_text_input_routes_every_supported_text_owner_before_quick_ingress() -> None:
    source = _handler_source()
    ordered_calls = (
        "self._finance_input",
        "self._transaction_input",
        "self._settings_input",
        "self._quick_input",
    )

    positions = tuple(source.index(call) for call in ordered_calls)

    assert positions == tuple(sorted(positions))


def test_text_input_unknown_state_fallback_has_no_legacy_database_mutation() -> None:
    source = textwrap.dedent(inspect.getsource(TextInputRouter))
    forbidden_legacy_calls = (
        "claim_update(",
        "get_draft(",
        "put_draft(",
        "create_or_get_account(",
        "create_or_get_category(",
        "edit_transaction(",
        "clear_draft(",
        "parse_local_datetime(",
        "parse_minor(",
        ".commit(",
    )

    assert all(call not in source for call in forbidden_legacy_calls)


def test_text_input_unknown_state_ends_with_a_narrow_fail_closed_answer() -> None:
    handler = _text_input_handler()
    final_statement = handler.body[-1]

    assert isinstance(final_statement, ast.Expr)
    assert isinstance(final_statement.value, ast.Await)
    assert isinstance(final_statement.value.value, ast.Call)
    assert ast.unparse(final_statement.value.value.func) == "message.answer"
    assert "не принимает текстовый ввод" in ast.unparse(final_statement)
    assert "сбросьте черновик" in ast.unparse(final_statement)


def test_bootstrap_only_constructs_and_registers_text_input_router() -> None:
    source = textwrap.dedent(inspect.getsource(build_dispatcher))

    assert "TextInputRouter(" in source
    assert "text_input_router.register(dp)" in source
    assert "async def text_input(" not in source
