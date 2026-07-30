from quota_monitor.cost_service import fetch_costs


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, params.copy()))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_costs_pagination_sums_each_page_once():
    client = FakeClient(
        [
            {
                "data": [{"results": [{"amount": {"value": 1.25, "currency": "usd"}}]}],
                "next_page": "cost-page-2",
            },
            {
                "data": [{"results": [{"amount": {"value": 0.75, "currency": "usd"}}]}],
                "next_page": None,
            },
        ]
    )
    result = fetch_costs(client, 100, 200)
    assert result == {"actual_usd": 2.0, "available": True, "error": None}
    assert [call[1].get("page") for call in client.calls] == [None, "cost-page-2"]
    assert client.calls[0][1]["end_time"] == 200


def test_costs_zero_length_range_returns_zero_without_api_request():
    client = FakeClient([])
    assert fetch_costs(client, 100, 100) == {"actual_usd": 0.0, "available": True, "error": None}
    assert client.calls == []


def test_costs_failure_returns_safe_partial_result():
    client = FakeClient([RuntimeError("secret upstream response")])
    result = fetch_costs(client, 100, 200)
    assert result["available"] is False
    assert result["actual_usd"] is None
    assert result["error"] == {
        "code": "costs_unavailable",
        "message": "Costs API data is temporarily unavailable.",
    }
    assert "secret" not in str(result)


def test_costs_invalid_required_fields_return_safe_partial_result():
    result = fetch_costs(FakeClient([{}]), 100, 200)
    assert result["available"] is False
    assert result["error"]["code"] == "costs_response_invalid"
