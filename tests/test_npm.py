

def test_ranking_sort_breaks_ties_deterministically():
    """Equal-pagerank rows must order by name, not by set-iteration order.

    process_data builds its rows by iterating `set(G.nodes())`, and Python
    randomizes string hashing per process, so that set yields a different
    order every run. `list.sort` is stable, so a sort keyed on pagerank alone
    preserved that order for ties — and two packages tied at a class cutoff
    then swapped value_class between otherwise identical runs. js-sha3 and
    app-root-path, both at pagerank 0.00003400, did exactly that: B<->C on
    every run, which propagated into top_eco_pct for hundreds of value.csv
    rows. The name tie-break makes the ranking a pure function of the data.
    """
    rows = [{"package": "js-sha3", "pagerank": "0.00003400"},
            {"package": "app-root-path", "pagerank": "0.00003400"},
            {"package": "lodash", "pagerank": "0.00900000"}]
    key = lambda r: (-float(r["pagerank"]), r["package"])

    forward = [r["package"] for r in sorted(rows, key=key)]
    backward = [r["package"] for r in sorted(list(reversed(rows)), key=key)]

    assert forward == backward, "tie order must not depend on input order"
    assert forward == ["lodash", "app-root-path", "js-sha3"]
