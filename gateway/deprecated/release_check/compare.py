"""Compare one check's result with its approved result. Used by release_check.run and release_check.report."""


def find_what_got_worse(result: dict, approved: dict) -> dict:
    """Return only what is worse than the approved result: failures we have not seen before,
    and metrics that moved past their allowed tolerance.

    approved["compare"][metric] = {"better": "lower"|"higher", "rel": 0.5, "abs": 20}
      lower is better  -> worse if result > approved * (1 + rel) + abs
      higher is better -> worse if result < approved * (1 - rel) - abs
    """
    known = {f["id"] for f in approved.get("failures", [])}
    new_failures = [f for f in result.get("failures", []) if f["id"] not in known]
    worse_metrics = []
    for name, rule in approved.get("compare", {}).items():
        was, now = approved["metrics"].get(name), result.get("metrics", {}).get(name)
        if was is None or now is None:
            continue
        rel, abs_ = rule.get("rel", 0.0), rule.get("abs", 0.0)
        if rule["better"] == "lower":
            limit = was * (1 + rel) + abs_
            worse = now > limit + 1e-9
        else:
            limit = was * (1 - rel) - abs_
            worse = now < limit - 1e-9
        if worse:
            worse_metrics.append({"metric": name, "approved": was, "now": now, "limit": round(limit, 4),
                                  "evidence": result.get("evidence", {}).get(name)})
    return {"new_failures": new_failures, "worse_metrics": worse_metrics}
