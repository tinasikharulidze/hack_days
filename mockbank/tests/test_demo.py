"""The demo script is a test too: if it breaks, the whole journey broke."""

from decimal import Decimal

from demo.run_flow import run


def test_the_demo_story_runs_from_start_to_end(client):
    result = run(client, quiet=True)
    assert result["balance_before"] - result["balance_after"] == Decimal("25.50")
    # One normal payment, then the same 40.00 payment twice: three completed, one duplicate.
    assert result["completed_count"] == 3
    assert len(result["duplicates"]) == 1 and result["duplicates"][0]["times_paid"] == 2
