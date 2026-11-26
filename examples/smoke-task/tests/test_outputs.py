import hashlib
import json
import sys

sys.path.insert(0, "/app")

from stats.summary import summarize  # noqa: E402

# Fingerprints of the expected answers, never the answers themselves (docs/hash-rules.md).
EXPECTED_SHA256 = {
    "odd": "fec545aac3d2720cf0558f66a1e450bda6c113f0e15260d4ae76b9bf89d68119",
    "even_median": "b8736b999909049671d0ea075a42b308a5fbe2df1854899123fe09eb0ee9de61",
    "even_count": "fbd04e1aae9ce0b11a8946e2c9ac2619f7428a64d32d01eff61d809dcb70ee8e",
    "even_mean": "b8736b999909049671d0ea075a42b308a5fbe2df1854899123fe09eb0ee9de61",
    "even_range": "46138845f82164fbca1b6b25041128f93bc21e65519a3aaf4e3b84d6b04232c3",
    "floats_median": "fbd04e1aae9ce0b11a8946e2c9ac2619f7428a64d32d01eff61d809dcb70ee8e",
    "single": "18ddb3338633c92530d37f271e27366561278f5c61d8afe9584f589c461c7976",
    "empty": "823bcefa5e02331dacdcf8dd396032ca2b9e02c1720572d4013a1db21cf43a27",
}


def digest(value):
    """SHA-256 of the value in one form: every number as a float, so 2 and 2.0 agree."""
    def norm(v):
        if isinstance(v, dict):
            return {k: norm(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [norm(x) for x in v]
        return None if v is None else float(v)
    return hashlib.sha256(json.dumps(norm(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_odd_length():
    assert digest(summarize([3, 1, 2])) == EXPECTED_SHA256["odd"]


def test_even_length_median_is_average_of_middle_values():
    result = summarize([4, 1, 3, 2])
    assert digest(result["median"]) == EXPECTED_SHA256["even_median"]
    assert digest(result["count"]) == EXPECTED_SHA256["even_count"]
    assert digest(result["mean"]) == EXPECTED_SHA256["even_mean"]
    assert digest((result["min"], result["max"])) == EXPECTED_SHA256["even_range"]


def test_unsorted_even_length_with_floats():
    assert digest(summarize([10.0, -2.0, 7.5, 0.5])["median"]) == EXPECTED_SHA256["floats_median"]


def test_single_value():
    assert digest(summarize([5])) == EXPECTED_SHA256["single"]


def test_empty_list():
    assert digest(summarize([])) == EXPECTED_SHA256["empty"]


def test_input_not_mutated():
    values = [3, 1, 2]
    summarize(values)
    assert values == [3, 1, 2]
