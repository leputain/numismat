from finbot.adapters.http.openapi_contract import build_openapi_document


def test_budget_progress_openapi_exposes_string_minor_forecast_and_closed_state() -> None:
    document = build_openapi_document()
    components = document["components"]
    schema = components["schemas"]["BudgetProgressResponse"]
    properties = schema["properties"]

    for name in (
        "known_recurring_minor",
        "safe_daily_minor",
        "forecast_minor",
    ):
        assert properties[name]["type"] == "string"
        assert properties[name]["pattern"] == r"^(?:0|[1-9][0-9]{0,63})$"
    assert properties["state"] == {
        "enum": ["on_track", "watch", "over"],
        "title": "State",
        "type": "string",
    }
    assert set(schema["required"]) >= {
        "known_recurring_minor",
        "safe_daily_minor",
        "forecast_minor",
        "state",
    }
