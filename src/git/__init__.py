"""Long-format git-snapshot data layer.

`data/git/{tool}.csv` files share one schema:

    repo, repo_id, commit_sha, metric, value, checked_at

One row per metric per snapshot. Key = (repo, commit_sha, metric).
"""
