"""Tests for the key_contributors.cum_share setting."""


def test_key_contributors_cum_share_is_a_valid_fraction():
    from src.common.params import KEY_CONTRIBUTORS_CUM_SHARE
    assert 0 < KEY_CONTRIBUTORS_CUM_SHARE <= 1
