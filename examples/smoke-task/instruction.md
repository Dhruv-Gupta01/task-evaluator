`/app/stats/summary.py` defines `summarize(values)`, which takes a list of numbers and returns a dict with the keys `count`, `mean`, `median`, `min` and `max`. It has two bugs:

1. The median is wrong for lists with an even number of values. It should be the average of the two middle values of the sorted list.
2. It crashes on an empty list. For an empty list it should return `count` as `0` and `mean`, `median`, `min` and `max` as `None`.

Fix both bugs in `/app/stats/summary.py`. Keep the function name, its signature and the keys of the returned dict unchanged.
