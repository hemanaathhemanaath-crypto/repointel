import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import quote

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
        "GITHUB_TOKEN is missing. Add it to Render Environment Variables."
    )

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
GROQ_API = "https://api.groq.com/openai/v1/chat/completions"

app = FastAPI(title="RepoIntel API", version="2.0.0")

# The frontend is a local HTML file during the college/demo workflow, so
# CORS must allow POST as well as GET. This is intentionally broad for demo use.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
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


def parse_link_header(value: str | None) -> dict[str, str]:
    """Parse GitHub's RFC5988-style Link header into rel -> URL."""
    links: dict[str, str] = {}
    if not value:
        return links
    for part in value.split(","):
        match = re.search(r"<([^>]+)>\s*;\s*rel=\"?([^\";]+)\"?", part.strip())
        if match:
            links[match.group(2)] = match.group(1)
    return links


async def github_get(path: str, params: dict | None = None) -> Any:
    response = await client.get(path, params=params)

    remaining = response.headers.get("x-ratelimit-remaining")
    if response.status_code in (403, 429):
        if remaining == "0":
            raise HTTPException(
                status_code=429,
                detail="GitHub API rate limit reached. Wait until the reset time before trying again.",
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
        raise HTTPException(status_code=502, detail="GitHub returned an invalid response.")


async def github_get_with_headers(path: str, params: dict | None = None):
    """Same as github_get, but also returns response headers for pagination."""
    response = await client.get(path, params=params)

    remaining = response.headers.get("x-ratelimit-remaining")
    if response.status_code in (403, 429):
        if remaining == "0":
            raise HTTPException(status_code=429, detail="GitHub API rate limit reached.")
        raise HTTPException(status_code=response.status_code, detail="GitHub temporarily rejected the request.")

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Repository or GitHub account not found, or it is private.")

    if response.status_code >= 400:
        detail = "GitHub API request failed."
        try:
            detail = response.json().get("message", detail)
        except Exception:
            pass
        raise HTTPException(status_code=response.status_code, detail=detail)

    try:
        data = response.json() if response.text.strip() else []
    except Exception:
        raise HTTPException(status_code=502, detail="GitHub returned an invalid response.")

    return data, response.headers


async def paginated_total(path: str, params: dict | None = None, fallback_count: int | None = None) -> int:
    """
    Get an exact GitHub collection total without downloading the collection.
    GitHub exposes the final page in the Link header when pagination exists.
    """
    base_params = dict(params or {})
    base_params["per_page"] = 1

    data, headers = await github_get_with_headers(path, base_params)
    links = parse_link_header(headers.get("link"))
    last_url = links.get("last")

    if last_url:
        match = re.search(r"[?&]page=(\d+)", last_url)
        if match:
            return int(match.group(1))

    if isinstance(data, list):
        return len(data)

    return int(fallback_count or 0)


async def get_contributors(owner: str, repo: str) -> list:
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
    """
    Prefer GitHub's 52-week stats endpoint. If GitHub has not generated the
    statistics yet (or returns an empty result), derive the recent 12-week
    chart from the actual commits endpoint so the dashboard still shows data.
    """
    for _ in range(4):
        response = await client.get(f"/repos/{owner}/{repo}/stats/commit_activity")
        if response.status_code == 202:
            await __import__("asyncio").sleep(1.2)
            continue
        if response.status_code == 200:
            try:
                weeks = response.json()
            except Exception:
                weeks = []
            if isinstance(weeks, list) and weeks:
                return [
                    {
                        "label": time.strftime("%b %d", time.gmtime(w.get("week", 0))),
                        "count": int(w.get("total", 0) or 0),
                    }
                    for w in weeks[-12:]
                ]
        break

    # Fallback: fetch recent commits and bucket them by week.
    since = datetime.now(timezone.utc) - timedelta(weeks=12)
    recent = []
    for page in range(1, 4):
        batch = await github_get(
            f"/repos/{owner}/{repo}/commits",
            {
                "per_page": 100,
                "page": page,
                "since": since.isoformat().replace("+00:00", "Z"),
            },
        )
        if not isinstance(batch, list):
            break
        recent.extend(batch)
        if len(batch) < 100:
            break

    buckets = {}
    for item in recent:
        date_text = (
            item.get("commit", {}).get("author", {}).get("date")
            or item.get("commit", {}).get("committer", {}).get("date")
        )
        if not date_text:
            continue
        try:
            dt = datetime.fromisoformat(date_text.replace("Z", "+00:00"))
        except Exception:
            continue
        # Monday-based week.
        monday = (dt - timedelta(days=dt.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        key = int(monday.timestamp())
        buckets[key] = buckets.get(key, 0) + 1

    start = datetime.now(timezone.utc) - timedelta(weeks=11)
    start_monday = (start - timedelta(days=start.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    result = []
    for i in range(12):
        dt = start_monday + timedelta(weeks=i)
        key = int(dt.timestamp())
        result.append(
            {"label": dt.strftime("%b %d"), "count": buckets.get(key, 0)}
        )
    return result


async def get_all_time_commit_count(owner: str, repo: str) -> int:
    """
    Exact all-time commit count for the repository's default branch, matching
    GitHub's commit history pagination rather than summing only recent weeks.
    """
    return await paginated_total(
        f"/repos/{owner}/{repo}/commits",
        {"sha": None},
    )


async def get_issue_counts(owner: str, repo: str) -> dict:
    """
    Exact counts using GitHub pagination metadata:
      - open_issues_count = open issues + open PRs
      - subtract exact open PR total
      - closed /issues includes closed issues + closed PRs
      - subtract exact closed PR total
      - all PRs = open + closed
    """
    repo_data = await github_get(f"/repos/{owner}/{repo}")
    open_combined = int(repo_data.get("open_issues_count", 0) or 0)

    open_prs = await paginated_total(
        f"/repos/{owner}/{repo}/pulls", {"state": "open"}
    )
    closed_prs = await paginated_total(
        f"/repos/{owner}/{repo}/pulls", {"state": "closed"}
    )
    closed_combined = await paginated_total(
        f"/repos/{owner}/{repo}/issues", {"state": "closed"}
    )

    open_issues = max(open_combined - open_prs, 0)
    closed_issues = max(closed_combined - closed_prs, 0)

    return {
        "open_issues": open_issues,
        "closed_issues": closed_issues,
        "pull_requests": open_prs + closed_prs,
        "open_pull_requests": open_prs,
        "closed_pull_requests": closed_prs,
    }


def score_health(repo: dict, contributors: list, issues: dict, activity: list) -> dict:
    stars = repo.get("stargazers_count", 0) or 0
    forks = repo.get("forks_count", 0) or 0
    contributor_count = len(contributors)

    recent_commits = sum(int(x.get("count", 0) or 0) for x in activity)
    commit_component = min(recent_commits / 250 * 60, 60)
    contributor_component = min(contributor_count / 20 * 25, 25)
    fork_component = min(forks / 1000 * 15, 15)
    activity_score = round(min(commit_component + contributor_component + fork_component, 100))

    updated = repo.get("updated_at")
    if updated:
        try:
            dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        except Exception:
            days = 999
    else:
        days = 999

    recency = (
        100 if days <= 7
        else 90 if days <= 30
        else 75 if days <= 90
        else 55 if days <= 180
        else 25
    )
    maintenance = round(min(
        recency * 0.70
        + (15 if repo.get("has_issues") else 5)
        + (15 if not repo.get("archived") else 0),
        100,
    ))

    top = contributors[0].get("contributions", 0) if contributors else 0
    total = sum(x.get("contributions", 0) for x in contributors)
    top_share = top / total if total else 1
    diversity = min(max((1 - top_share) * 125, 0), 100)
    contributor_score = round(
        min(max(min(contributor_count / 25 * 55, 55) + diversity * 0.45, 0), 100)
    )

    open_issues = issues.get("open_issues", 0)
    closed_issues = issues.get("closed_issues", 0)
    total_issues = open_issues + closed_issues
    closure = closed_issues / total_issues if total_issues else 1
    load_penalty = min(open_issues / max(500, stars / 50) * 35, 100)
    issue_score = round(
        min(max(closure * 70 + (100 - load_penalty) * 0.30, 0), 100)
    )

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


async def build_repo_context(owner: str, repo: str) -> dict:
    repository = await github_get(f"/repos/{owner}/{repo}")
    contributors = await get_contributors(owner, repo)
    languages = await github_get(f"/repos/{owner}/{repo}/languages")
    activity = await get_commit_activity(owner, repo)
    commit_total = await get_all_time_commit_count(owner, repo)
    issues = await get_issue_counts(owner, repo)
    health_score = score_health(repository, contributors, issues, activity)

    return {
        "repository": repository,
        "contributors": [
            {
                "login": c.get("login"),
                "contributions": c.get("contributions", 0),
            }
            for c in contributors[:50]
        ],
        "contributor_count": len(contributors),
        "languages": languages,
        "commit_activity": activity,
        "commit_total": commit_total,
        "commit_total_source": "GitHub commits pagination",
        "issues": issues,
        "health": health_score,
    }


async def groq_chat(system_prompt: str, user_prompt: str) -> str:
    if not GROQ_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="AI is not configured. Add GROQ_API_KEY to Render Environment Variables.",
        )

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    async with httpx.AsyncClient(timeout=90) as ai_client:
        response = await ai_client.post(GROQ_API, headers=headers, json=payload)

    if response.status_code >= 400:
        try:
            detail = response.json().get("error", {}).get("message", "Groq request failed.")
        except Exception:
            detail = "Groq request failed."
        raise HTTPException(status_code=502, detail=detail)

    try:
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        raise HTTPException(status_code=502, detail="AI returned an invalid response.")


@app.get("/")
async def root():
    return {"service": "RepoIntel API", "status": "online"}


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "github_authenticated": bool(GITHUB_TOKEN),
        "ai_configured": bool(GROQ_API_KEY),
        "ai_model": GROQ_MODEL if GROQ_API_KEY else None,
    }


@app.get("/api/profile/{owner}/repos")
async def profile_repos(owner: str):
    return await github_get(
        f"/users/{owner}/repos",
        {"type": "public", "sort": "updated", "per_page": 100},
    )


@app.get("/api/profile/{username}/repos")
async def github_profile_repos(username: str):
    repos = await github_get(
        f"/users/{username}/repos",
        {"type": "all", "sort": "updated", "per_page": 100},
    )
    return {
        "profile": username,
        "repositories": [
            {
                "name": x.get("name"),
                "full_name": x.get("full_name"),
                "description": x.get("description") or "",
                "html_url": x.get("html_url"),
                "language": x.get("language"),
                "stargazers_count": x.get("stargazers_count", 0),
                "forks_count": x.get("forks_count", 0),
                "updated_at": x.get("updated_at"),
                "private": x.get("private", False),
            }
            for x in repos
            if not x.get("private", False)
        ],
    }


@app.get("/api/github/search")
async def github_search(q: str = ""):
    q = q.strip()
    if not q:
        return {"items": []}

    data = await github_get("/search/users", {"q": q, "per_page": 8})
    return {
        "items": [
            {
                "login": x.get("login"),
                "avatar_url": x.get("avatar_url"),
                "html_url": x.get("html_url"),
                "type": x.get("type", "User"),
            }
            for x in data.get("items", [])
        ]
    }


@app.get("/api/analyze")
async def analyze(owner: str, repo: str):
    # Keep these calls server-side so the browser never exposes the GitHub token.
    repository = await github_get(f"/repos/{owner}/{repo}")
    contributors = await get_contributors(owner, repo)
    languages = await github_get(f"/repos/{owner}/{repo}/languages")
    activity = await get_commit_activity(owner, repo)
    commit_total = await get_all_time_commit_count(owner, repo)
    issues = await get_issue_counts(owner, repo)
    health_score = score_health(repository, contributors, issues, activity)

    return {
        "repo": repository,
        "contributors": contributors,
        "languages": languages,
        "commit_activity": activity,
        "commit_total": commit_total,
        "commit_total_source": "GitHub commits pagination",
        "issues": issues,
        "health": health_score,
    }


@app.post("/api/ai/insights")
async def ai_insights(payload: dict):
    owner = str(payload.get("owner", "")).strip()
    repo = str(payload.get("repo", "")).strip()

    if owner and repo:
        context = await build_repo_context(owner, repo)
    else:
        context = payload.get("analysis") or payload.get("context")
        if not isinstance(context, dict):
            raise HTTPException(
                status_code=400,
                detail="Repository analysis context is required.",
            )

    system = """You are RepoIntel AI, a software repository analyst.
Use ONLY the repository data supplied in the prompt.
Do not invent files, bugs, contributors, technologies, or metrics.
Interpret the numbers rather than merely repeating them.
Be detailed enough for a college software-project presentation, but remain factual.
Return clear sections:
Overall assessment
Strengths
Weaknesses / risks
Maintenance
Priority recommendations
"""

    user = f"""Analyze this repository using the live RepoIntel data below.

Repository context:
{context}

Explain what the metrics mean, identify the strongest and weakest signals,
and give practical improvements. If a metric is unavailable, say so instead
of guessing."""
    answer = await groq_chat(system, user)
    return {"ok": True, "answer": answer, "model": GROQ_MODEL}


@app.post("/api/ai/ask")
async def ai_ask(payload: dict):
    question = str(
        payload.get("question")
        or payload.get("message")
        or payload.get("prompt")
        or ""
    ).strip()

    if not question:
        raise HTTPException(status_code=400, detail="question is required.")

    owner = str(payload.get("owner", "")).strip()
    repo = str(payload.get("repo", "")).strip()

    if owner and repo:
        context = await build_repo_context(owner, repo)
    else:
        context = payload.get("analysis") or payload.get("context") or {}

    system = """You are RepoIntel AI, a private assistant inside RepoIntel.
Answer ONLY from the repository context supplied.
Do not claim to browse outside the supplied context.
If the context does not contain enough information, say that clearly.
Give a direct, useful answer suitable for a software developer or student presentation."""

    user = f"""Repository context:
{context}

User question:
{question}

Answer specifically using the repository context."""
    answer = await groq_chat(system, user)
    return {"ok": True, "answer": answer, "model": GROQ_MODEL}


@app.post("/api/ai/report")
async def ai_report(payload: dict):
    """
    Long-form AI report endpoint used by the Reports page.
    The frontend sends the already-loaded live analysis so the report and
    dashboard are guaranteed to describe the same repository snapshot.
    """
    owner = str(payload.get("owner", "")).strip()
    repo = str(payload.get("repo", "")).strip()

    if owner and repo:
        context = await build_repo_context(owner, repo)
    else:
        context = payload.get("analysis") or payload.get("context")
        if not isinstance(context, dict):
            raise HTTPException(status_code=400, detail="Repository analysis context is required.")

    system = """You are RepoIntel AI generating a detailed repository report.

Use ONLY the supplied GitHub/RepoIntel data. Never fabricate metrics.
Write a professional, presentation-ready report with these sections:

1. Executive Summary
2. Repository Profile
3. Development Activity
4. Commit Analysis
5. Contributors and Collaboration
6. Technology / Language Analysis
7. Issues and Pull Requests
8. Repository Health Assessment
9. Strengths
10. Weaknesses and Risks
11. Recommended Improvements
12. Conclusion

Interpret the data in detail. Distinguish all-time commits from recent 12-week activity.
Mention when a metric is zero because GitHub actually reports zero.
Do not claim a problem merely because a metric is small; explain the context.
Use concise paragraphs and bullet points where useful."""

    user = f"""Prepare the detailed report from this live repository snapshot:

{context}

The report must be data-backed and suitable for a college project demonstration.
Improve the wording and interpretation, but do not change the underlying facts."""
    answer = await groq_chat(system, user)
    return {"ok": True, "answer": answer, "model": GROQ_MODEL}


@app.on_event("shutdown")
async def shutdown():
    await client.aclose()
