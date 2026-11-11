"""Each seeded bug must break exactly the rule it targets, and nothing else."""
import pytest

import checks
from onboarding.api import bugs


def test_every_bug_has_a_check():
    assert set(checks.DETECTED_BY) == set(bugs.CATALOG)


@pytest.mark.parametrize("bug_id", sorted(bugs.CATALOG))
def test_bug_breaks_only_its_own_rule(make_client, bug_id):
    target = checks.DETECTED_BY[bug_id]
    with pytest.raises(AssertionError):
        target(make_client([bug_id]))
    for check in checks.ALL:
        if check is not target:
            check(make_client([bug_id]))


def test_unknown_bug_id_is_rejected():
    with pytest.raises(ValueError):
        bugs.parse("B99")
