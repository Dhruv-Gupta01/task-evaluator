import sys

sys.path.insert(0, "/app")

from stats.summary import summarize  # noqa: E402


def test_odd_length():
    assert summarize([3, 1, 2]) == {"count": 3, "mean": 2, "median": 2, "min": 1, "max": 3}


def test_even_length_median_is_average_of_middle_values():
    result = summarize([4, 1, 3, 2])
    assert result["median"] == 2.5
    assert result["count"] == 4
    assert result["mean"] == 2.5
    assert (result["min"], result["max"]) == (1, 4)


def test_unsorted_even_length_with_floats():
    assert summarize([10.0, -2.0, 7.5, 0.5])["median"] == 4.0


def test_single_value():
    assert summarize([5]) == {"count": 1, "mean": 5, "median": 5, "min": 5, "max": 5}


def test_empty_list():
    assert summarize([]) == {"count": 0, "mean": None, "median": None, "min": None, "max": None}


def test_input_not_mutated():
    values = [3, 1, 2]
    summarize(values)
    assert values == [3, 1, 2]
