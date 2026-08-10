import os
import re
import time
from urllib.parse import quote
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

app = FastAPI(title="RepoIntel API", version="1.2.0")

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


async def github_get(path: str, params: dict | None = None) -> Any:
    response = await client.get(path, params=params)
    remaining = response.headers.get("x-ratelimit-remaining")

    if response.status_code in (403, 429):
        if remaining == "0":
            raise HTTPException(
                status_code=429,
                detail="GitHub API rate limit reached. Please wait for the reset time.",
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

    recency = (
        100 if days <= 7 else
        90 if days <= 30 else
        75 if days <= 90 else
        55 if days <= 180 else
        25
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

    contributor_score = round(min(
        max(min(contributor_count / 25 * 55, 55) + diversity * 0.45, 0),
        100,
    ))

    open_issues = issues.get("open_issues", 0)
    closed_issues = issues.get("closed_issues", 0)
    total_issues = open_issues + closed_issues
    closure = closed_issues / total_issues if total_issues else 1

    load_penalty = min(open_issues / max(500, stars / 50) * 35, 100)
    issue_score = round(min(
        max(closure * 70 + (100 - load_penalty) * 0.30, 0),
        100,
    ))

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
    Return an ALL-TIME monthly commit series from the GitHub commits endpoint.

    GitHub's /stats/commit_activity endpoint only represents a limited recent
    window and can also return 202 while statistics are being generated. The
    dashboard is explicitly configured for all-time activity, so build the
    series from the repository's actual commit history instead.

    Each returned item is:
        {"label": "Aug 2026", "count": 70}

    The endpoint is paginated at 100 commits/page. This is deliberately
    bounded to protect the API, while still covering normal project-sized
    repositories. If the repository exceeds the safety limit, the endpoint
    keeps the data it has successfully collected and the exact all-time total
    is still obtained separately from the pagination Link header.
    """
    import asyncio
    from collections import defaultdict
    from datetime import datetime, timezone

    monthly = defaultdict(int)
    page = 1
    max_pages = 100

    while page <= max_pages:
        response = await client.get(
            f"/repos/{owner}/{repo}/commits",
            params={"per_page": 100, "page": page},
        )

        if response.status_code == 409:
            return []

        if response.status_code >= 400:
            detail = "Unable to load repository commit activity."
            try:
                detail = response.json().get("message", detail)
            except Exception:
                pass
            raise HTTPException(status_code=response.status_code, detail=detail)

        try:
            commits = response.json()
        except Exception:
            raise HTTPException(status_code=502, detail="GitHub returned invalid commit data.")

        if not isinstance(commits, list) or not commits:
            break

        for item in commits:
            commit = item.get("commit") or {}
            author = commit.get("author") or {}
            date_value = author.get("date")

            if not date_value:
                # Fall back to committer date when author date is unavailable.
                committer = commit.get("committer") or {}
                date_value = committer.get("date")

            if not date_value:
                continue

            try:
                dt = datetime.fromisoformat(date_value.replace("Z", "+00:00"))
                dt = dt.astimezone(timezone.utc)
                label = dt.strftime("%b %Y")
                monthly[label] += 1
            except Exception:
                continue

        if len(commits) < 100:
            break

        page += 1

    # Return chronological monthly buckets. The frontend fills quiet months
    # between the first and last month, so the chart remains visually useful.
    parsed = []
    for label, count in monthly.items():
        try:
            dt = datetime.strptime(label, "%b %Y")
            parsed.append((dt, label, count))
        except Exception:
            continue

    parsed.sort(key=lambda x: x[0])
    return [{"label": label, "count": count} for _, label, count in parsed]


async def get_actual_commit_total(owner: str, repo: str) -> int:
    """
    Return GitHub's actual all-time commit count.

    A single commit is requested and the Link header's rel="last" page number
    gives the exact total because each page contains exactly one commit.
    """
    response = await client.get(
        f"/repos/{owner}/{repo}/commits",
        params={"per_page": 1},
    )

    if response.status_code == 409:
        return 0

    if response.status_code >= 400:
        detail = "Unable to determine the repository commit count."
        try:
            detail = response.json().get("message", detail)
        except Exception:
            pass
        raise HTTPException(status_code=response.status_code, detail=detail)

    try:
        first_page = response.json()
    except Exception:
        raise HTTPException(status_code=502, detail="GitHub returned invalid commit data.")

    if not isinstance(first_page, list) or not first_page:
        return 0

    link = response.headers.get("link", "")
    match = re.search(
        r'<[^>]*[?&]page=(\d+)[^>]*>\s*;\s*rel="last"',
        link,
        flags=re.IGNORECASE,
    )

    if match:
        return int(match.group(1))

    return 1


async def get_issue_counts(owner: str, repo: str) -> dict:
    """
    Use GitHub Search totals so issue and PR numbers are not limited to the
    first 100 results.
    """
    base = f"repo:{owner}/{repo}"

    async def search_total(query: str) -> int:
        data = await github_get("/search/issues", {"q": query, "per_page": 1})
        return int(data.get("total_count", 0) or 0)

    open_issues, closed_issues, pull_requests = await __import__("asyncio").gather(
        search_total(f"{base} is:issue state:open"),
        search_total(f"{base} is:issue state:closed"),
        search_total(f"{base} is:pr"),
    )

    return {
        "open_issues": open_issues,
        "closed_issues": closed_issues,
        "pull_requests": pull_requests,
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


@app.get("/api/github/profile/{username}/repos")
async def github_profile_repos(username: str):
    repos = await github_get(
        f"/users/{quote(username)}/repos",
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


# -------------------- RepoIntel AI --------------------

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
GROQ_API = "https://api.groq.com/openai/v1/chat/completions"


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
        "max_completion_tokens": 6000,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    async with httpx.AsyncClient(timeout=60) as ai_client:
        response = await ai_client.post(GROQ_API, headers=headers, json=payload)

    if response.status_code >= 400:
        try:
            detail = response.json().get("error", {}).get("message", "Groq request failed.")
        except Exception:
            detail = "Groq request failed."
        raise HTTPException(status_code=502, detail=detail)

    data = response.json()

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        raise HTTPException(status_code=502, detail="AI returned an invalid response.")


async def build_repo_context(owner: str, repo: str) -> dict:
    repository = await github_get(f"/repos/{owner}/{repo}")
    contributors = await get_contributors(owner, repo)
    languages = await github_get(f"/repos/{owner}/{repo}/languages")
    activity = await get_commit_activity(owner, repo)
    commit_total = await get_actual_commit_total(owner, repo)
    issues = await get_issue_counts(owner, repo)
    health_score = score_health(repository, contributors, issues, activity)

    return {
        "repository": repository,
        "languages": languages,
        "contributors": [
            {
                "login": c.get("login"),
                "contributions": c.get("contributions", 0),
            }
            for c in contributors[:20]
        ],
        "commit_activity": activity,
        "commit_total": commit_total,
        "issues": issues,
        "health": health_score,
    }


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "github_authenticated": bool(GITHUB_TOKEN),
        "ai_configured": bool(GROQ_API_KEY),
        "ai_model": GROQ_MODEL if GROQ_API_KEY else None,
        "ai_endpoint": "/api/ai/report",
    }


@app.post("/api/ai/insights")
async def ai_insights(payload: dict):
    owner = str(payload.get("owner", "")).strip()
    repo = str(payload.get("repo", "")).strip()

    if owner and repo:
        context = await build_repo_context(owner, repo)
    else:
        context = payload.get("analysis")
        if not isinstance(context, dict):
            raise HTTPException(status_code=400, detail="owner and repo are required.")

    system = """You are RepoIntel AI, a software repository analyst.
You only discuss repository data supplied in the prompt.
Do not invent files, bugs, contributors, technologies, or metrics.
Give concise, actionable engineering insights suitable for a dashboard.
Return plain text with clear sections."""

    user = f"""Analyze this repository for RepoIntel.

Repository data:
{context}

Produce:
1. Overall assessment
2. Strengths
3. Risks / weaknesses
4. Maintenance concerns
5. Three practical recommendations
6. A short priority statement"""

    answer = await groq_chat(system, user)
    return {"ok": True, "answer": answer, "model": GROQ_MODEL}



@app.post("/api/ai/report")
async def ai_report(payload: dict):
    """Generate a detailed, repository-grounded report for the Reports page."""
    owner = str(payload.get("owner", "")).strip()
    repo = str(payload.get("repo", "")).strip()

    supplied_analysis = payload.get("analysis")
    if owner and repo:
        try:
            context = await build_repo_context(owner, repo)
        except HTTPException:
            if isinstance(supplied_analysis, dict):
                context = supplied_analysis
            else:
                raise
    else:
        context = supplied_analysis
        if not isinstance(context, dict):
            raise HTTPException(status_code=400, detail="Repository analysis is required.")

    system = """You are RepoIntel AI, the report-writing analyst inside a software repository analytics website.
You may ONLY use the repository data supplied in the context.
Do not invent files, bugs, technologies, contributors, activity, dates, metrics, or project goals.
Do not claim to inspect source code unless source-code/file data is explicitly supplied.
Turn the available metrics into a professional, detailed repository health report.
The report is intended for a college project presentation and should be clear enough for a technical reviewer.
Explain what the metrics mean, why they matter, likely implications, and practical next steps.
Distinguish observed facts from reasonable interpretation.
Use exactly these section headings:
EXECUTIVE SUMMARY
REPOSITORY PROFILE
DEVELOPMENT ACTIVITY
COLLABORATION AND CONTRIBUTORS
ISSUES AND MAINTENANCE
TECHNOLOGY PROFILE
HEALTH ASSESSMENT
KEY STRENGTHS
KEY RISKS
RECOMMENDATIONS
CONCLUSION
Use concise paragraphs and bullet points. Never fabricate missing data."""

    user = f"""Write the detailed RepoIntel report from this live repository context.

Repository context:
{context}

Important:
- All-time commit_total is the repository's total commit count.
- commit_activity is the recent 12-week activity series and should be discussed separately.
- health contains the calculated overall, activity, maintenance, contributor, and issue scores.
- Explain trends from the supplied 12-week activity values, but do not invent a trend when the values do not support one.
- Mention unavailable information explicitly instead of filling gaps.

Return the complete report only."""
    answer = await groq_chat(system, user)
    return {"ok": True, "report": answer, "model": GROQ_MODEL}

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

    system = """You are RepoIntel AI, a private assistant inside the RepoIntel website.
You answer questions ONLY about the repository context supplied by RepoIntel.
Never claim to browse the web or access information outside that context.
If the context does not contain enough information, say so clearly.
Be concise and useful to a software developer."""

    user = f"""Repository context:
{context}

User question:
{question}

Answer specifically using the repository context above."""

    answer = await groq_chat(system, user)
    return {"ok": True, "answer": answer, "model": GROQ_MODEL}


@app.get("/")
async def root():
    return {"service": "RepoIntel API", "status": "online"}


@app.get("/api/profile/{owner}/repos")
async def profile_repos(owner: str):
    return await github_get(
        f"/users/{owner}/repos",
        {"type": "public", "sort": "updated", "per_page": 100},
    )


@app.get("/api/analyze")
async def analyze(owner: str, repo: str):
    repository = await github_get(f"/repos/{owner}/{repo}")
    contributors = await get_contributors(owner, repo)
    languages = await github_get(f"/repos/{owner}/{repo}/languages")
    activity = await get_commit_activity(owner, repo)
    commit_total = await get_actual_commit_total(owner, repo)
    issues = await get_issue_counts(owner, repo)
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
