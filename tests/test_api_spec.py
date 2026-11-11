"""The clean API must satisfy every business rule."""
import pytest

import checks


@pytest.mark.parametrize("check", checks.ALL, ids=lambda f: f.__name__)
def test_clean_api_follows_rule(make_client, check):
    check(make_client())


def test_openapi_spec_publishes_the_rules(make_client):
    spec = make_client().get("/openapi.json").json()
    for rule in ("R1", "R5", "R10"):
        assert f"**{rule} " in spec["info"]["description"]
    assert "/v1/payments/{payment_id}/refunds" in spec["paths"]
