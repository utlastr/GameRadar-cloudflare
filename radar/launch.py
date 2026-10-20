"""Release dates come only from records identified by their native game ID."""

from . import config


def fill(cards, allow_network=True):
    # group_info already attaches dates from the identified source records.
    # Missing dates stay unknown instead of borrowing a name-matched result.
    return 0


def age_weight(age_days):
    for days, weight in config.AGE_WEIGHTS:
        if age_days <= days:
            return weight
    return 0.0
