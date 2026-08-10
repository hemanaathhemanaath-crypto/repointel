# RepoIntel Backend

This backend keeps the GitHub token off the browser and makes authenticated GitHub API requests.

## 1. Create a GitHub token

Create a GitHub personal access token with the minimum permissions needed for the public repositories you want to analyze. For public-data analysis, keep permissions minimal.

GitHub documents token authentication here:
https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api

## 2. Configure

Copy `.env.example` to `.env` and put your token in:

GITHUB_TOKEN=YOUR_TOKEN

**Never commit `.env` or your token to GitHub.**

## 3. Install

Python 3.10+ recommended.

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

## 4. Run

```bash
uvicorn backend.main:app --reload --port 8000
```

Then open `repointel_live_kpis.html` in your browser.

The frontend now calls:

- `http://localhost:8000/api/profile/{owner}/repos`
- `http://localhost:8000/api/analyze?owner=...&repo=...`

## Why this fixes the rate-limit problem

The browser no longer makes unauthenticated requests directly to `api.github.com`.
The backend sends authenticated requests using the token.

GitHub documents a 60 requests/hour primary limit for unauthenticated REST requests and generally 5,000 requests/hour for authenticated users. Search endpoints have separate limits, so this backend deliberately avoids GitHub Search for the main analysis flow.

For a production deployment, use a GitHub App/OAuth flow rather than sharing one personal token across users.
