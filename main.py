import os
import re
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_API = "https://api.github.com"
API_VERSION = "2022-11-28"

if not GITHUB_TOKEN:
    raise RuntimeError(
        "GITHUB_TOKEN is missing. Copy .env.example to .env and add your GitHub token."
    )

app = FastAPI(title="RepoIntel API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For college/demo use. Restrict this in production.
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

client = httpx.AsyncClient(
    base_url=GITHUB_API,
    timeout=30.0,
    headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "RepoIntel-College-Project",
    },
)


async def github_get(path: str, params: dict | None = None) -> Any:
    response = await client.get(path, params=params)

    remaining = response.headers.get("x-ratelimit-remaining")
    reset = response.headers.get("x-ratelimit-reset")

    if response.status_code in (403, 429):
        if remaining == "0":
            raise HTTPException(
                status_code=429,
                detail=(
                    "GitHub API rate limit reached on the RepoIntel backend. "
                    "Wait until the reset time before trying again."
                ),
            )
        raise HTTPException(
            status_code=response.status_code,
            detail="GitHub temporarily rejected the request. Please try again shortly.",
        )

    if response.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail="Repository or GitHub account not found, or it is private.",
        )

    if response.status_code >= 400:
        detail = "GitHub API request failed."
        try:
            detail = response.json().get("message", detail)
        except Exception:
            pass
        raise HTTPException(status_code=response.status_code, detail=detail)

    if response.status_code == 204 or not response.text.strip():
        return []

    try:
        return response.json()
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="GitHub returned an invalid response.",
        )


def score_health(repo: dict, contributors: list, issues: dict, activity: list) -> dict:
    stars = repo.get("stargazers_count", 0) or 0
    forks = repo.get("forks_count", 0) or 0
    contributor_count = len(contributors)
    recent_commits = sum(x.get("count", 0) for x in activity)

    commit_component = min(recent_commits / 250 * 60, 60)
    contributor_component = min(contributor_count / 20 * 25, 25)
    fork_component = min(forks / 1000 * 15, 15)
    activity_score = round(min(commit_component + contributor_component + fork_component, 100))

    updated = repo.get("updated_at")
    if updated:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        except Exception:
            days = 999
    else:
        days = 999

    recency = 100 if days <= 7 else 90 if days <= 30 else 75 if days <= 90 else 55 if days <= 180 else 25
    maintenance = round(min(recency * 0.70 + (15 if repo.get("has_issues") else 5) + (15 if not repo.get("archived") else 0), 100))

    top = contributors[0].get("contributions", 0) if contributors else 0
    total = sum(x.get("contributions", 0) for x in contributors)
    top_share = top / total if total else 1
    diversity = min(max((1 - top_share) * 125, 0), 100)
    contributor_score = round(min(max(min(contributor_count / 25 * 55, 55) + diversity * 0.45, 0), 100))

    open_issues = issues.get("open_issues", 0)
    closed_issues = issues.get("closed_issues", 0)
    total_issues = open_issues + closed_issues
    closure = closed_issues / total_issues if total_issues else 1
    load_penalty = min(open_issues / max(500, stars / 50) * 35, 100)
    issue_score = round(min(max(closure * 70 + (100 - load_penalty) * 0.30, 0), 100))

    overall = round(
        activity_score * 0.30
        + maintenance * 0.25
        + contributor_score * 0.20
        + issue_score * 0.25
    )

    return {
        "overall": overall,
        "activity": activity_score,
        "maintenance": maintenance,
        "contributorScore": contributor_score,
        "issueScore": issue_score,
    }


async def get_contributors(owner: str, repo: str) -> list:
    # Paginate contributors, but stop at 1,000 to keep analysis bounded.
    result = []
    for page in range(1, 11):
        batch = await github_get(
            f"/repos/{owner}/{repo}/contributors",
            {"per_page": 100, "page": page},
        )
        if not isinstance(batch, list):
            break
        result.extend(batch)
        if len(batch) < 100:
            break
    return result


async def get_commit_activity(owner: str, repo: str) -> list:
    for _ in range(3):
        response = await client.get(
            f"/repos/{owner}/{repo}/stats/commit_activity"
        )
        if response.status_code == 202:
            await __import__("asyncio").sleep(1.2)
            continue
        if response.status_code != 200:
            return []
        try:
            weeks = response.json()
        except Exception:
            return []
        if not isinstance(weeks, list):
            return []
        return [
            {
                "label": time.strftime("%b %d", time.gmtime(w.get("week", 0))),
                "count": w.get("total", 0),
            }
            for w in weeks[-12:]
        ]
    return []


async def get_commit_total(activity: list) -> int:
    return sum(x.get("count", 0) for x in activity)


async def get_issue_counts(owner: str, repo: str) -> dict:
    # Avoid the search API because its rate limit is much tighter than core REST.
    # GitHub's repository field counts open issues + PRs, so we expose that
    # transparently and obtain a bounded issue/PR sample for the dashboard.
    repo_data = await github_get(f"/repos/{owner}/{repo}")
    open_combined = repo_data.get("open_issues_count", 0) or 0

    open_items = await github_get(
        f"/repos/{owner}/{repo}/issues",
        {"state": "open", "per_page": 100},
    )
    closed_items = await github_get(
        f"/repos/{owner}/{repo}/issues",
        {"state": "closed", "per_page": 100},
    )
    pulls = await github_get(
        f"/repos/{owner}/{repo}/pulls",
        {"state": "all", "per_page": 100},
    )

    open_pr_sample = sum(1 for x in open_items if "pull_request" in x)
    open_issues = max(open_combined - open_pr_sample, 0)

    # Exact closed issue totals are not exposed by the repository endpoint.
    # Use the closed page sample as the dashboard value and label it in the UI.
    closed_issues = sum(1 for x in closed_items if "pull_request" not in x)

    return {
        "open_issues": open_issues,
        "closed_issues": closed_issues,
        "pull_requests": len(pulls),
    }


@app.get("/api/health")
async def health():
    return {"ok": True, "github_authenticated": bool(GITHUB_TOKEN)}


@app.get("/api/profile/{owner}/repos")
async def profile_repos(owner: str):
    repos = await github_get(
        f"/users/{owner}/repos",
        {"type": "public", "sort": "updated", "per_page": 100},
    )
    return repos


@app.get("/api/analyze")
async def analyze(owner: str, repo: str):
    # Serial calls intentionally reduce secondary-rate-limit pressure.
    repository = await github_get(f"/repos/{owner}/{repo}")
    contributors = await get_contributors(owner, repo)
    languages = await github_get(f"/repos/{owner}/{repo}/languages")
    activity = await get_commit_activity(owner, repo)
    issues = await get_issue_counts(owner, repo)
    commit_total = await get_commit_total(activity)
    health_score = score_health(repository, contributors, issues, activity)

    return {
        "repo": repository,
        "contributors": contributors,
        "languages": languages,
        "commit_activity": activity,
        "commit_total": commit_total,
        "issues": issues,
        "health": health_score,
    }


@app.on_event("shutdown")
async def shutdown():
    await client.aclose()
