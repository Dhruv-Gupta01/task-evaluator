def summarize(values):
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "mean": sum(ordered) / len(ordered),
        "median": ordered[len(ordered) // 2],
        "min": ordered[0],
        "max": ordered[-1],
    }
