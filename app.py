#!/usr/bin/env python3
"""
Flask Web Server for Git Local Rank Checker
───────────────────────────────────────────────
Wraps the CLI ranking engine into a JSON API + serves the web frontend.
Supports GitHub OAuth login so users don't need a personal token.
Shareable links stored in Upstash Redis (optional).
"""

import json
import math
import os
import secrets
import time
from datetime import datetime, timezone

import requests as http_requests
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from github_local_rank import GitHubClient, GitHubUser, PinResolver, Ranker

# Optional: Upstash Redis for shareable links (lazy import)
_redis = None


def _get_redis():
    global _redis
    if _redis is not None:
        return _redis
    url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")
    if url and token:
        try:
            from upstash_redis import Redis
            _redis = Redis(url=url, token=token)
            return _redis
        except Exception:
            pass
    return None

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

# GitHub OAuth config
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
GITHUB_OAUTH_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_OAUTH_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_API_USER = "https://api.github.com/user"


# ─────────────────────────────────────────────────────
#  PAGES
# ─────────────────────────────────────────────────────

@app.route("/ping")
def ping():
    """Lightweight health/keep-alive endpoint. Use with cron-job.org or UptimeRobot to prevent cold starts."""
    return "pong", 200, {"Content-Type": "text/plain"}


@app.route("/")
def index():
    """Serve the main frontend page."""
    return render_template("index.html")


# ─────────────────────────────────────────────────────
#  GITHUB OAUTH FLOW
# ─────────────────────────────────────────────────────

@app.route("/login")
def login():
    """Redirect user to GitHub OAuth authorization page."""
    if not GITHUB_CLIENT_ID:
        return jsonify({"error": "GitHub OAuth is not configured. Set GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET in .env"}), 500

    # Generate a random state for CSRF protection
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state

    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": url_for("callback", _external=True),
        "scope": "read:user",
        "state": state,
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return redirect(f"{GITHUB_OAUTH_AUTHORIZE}?{query}")


@app.route("/callback")
def callback():
    """Handle the OAuth callback from GitHub."""
    code = request.args.get("code")
    state = request.args.get("state")

    # Verify CSRF state
    if not code or state != session.pop("oauth_state", None):
        return redirect("/?error=auth_failed")

    # Exchange code for access token
    resp = http_requests.post(
        GITHUB_OAUTH_TOKEN,
        headers={"Accept": "application/json"},
        data={
            "client_id": GITHUB_CLIENT_ID,
            "client_secret": GITHUB_CLIENT_SECRET,
            "code": code,
            "redirect_uri": url_for("callback", _external=True),
        },
        timeout=15,
    )

    if resp.status_code != 200:
        return redirect("/?error=token_exchange_failed")

    token_data = resp.json()
    access_token = token_data.get("access_token")

    if not access_token:
        return redirect("/?error=no_token")

    # Fetch the user's GitHub profile
    user_resp = http_requests.get(
        GITHUB_API_USER,
        headers={
            "Authorization": f"token {access_token}",
            "Accept": "application/vnd.github.v3+json",
        },
        timeout=10,
    )

    if user_resp.status_code != 200:
        return redirect("/?error=profile_fetch_failed")

    user_data = user_resp.json()

    # Store in session
    session["github_token"] = access_token
    session["github_user"] = {
        "login": user_data.get("login", ""),
        "name": user_data.get("name", "") or "",
        "avatar_url": user_data.get("avatar_url", ""),
    }

    return redirect("/")


@app.route("/logout")
def logout():
    """Clear the session and log out."""
    session.clear()
    return redirect("/")


def _is_local_request():
    """True when request is to localhost (token-only dev mode is safe)."""
    host = (request.host or "").split(":")[0].lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host in ("localhost", "127.0.0.1", "::1")


@app.route("/api/me")
def api_me():
    """Return the currently logged-in user's info, or token-only mode for local dev."""
    user = session.get("github_user")
    if user:
        return jsonify({
            "logged_in": True,
            "login": user["login"],
            "name": user["name"],
            "avatar_url": user["avatar_url"],
        })
    # Token-only mode: GITHUB_TOKEN in .env + localhost → no login required
    payload = {"logged_in": False}
    if os.environ.get("GITHUB_TOKEN") and _is_local_request():
        payload["token_available"] = True
    return jsonify(payload)


# ─────────────────────────────────────────────────────
#  RANKING API
# ─────────────────────────────────────────────────────

def _get_github_client():
    """
    Create a GitHubClient using the best available token:
      1. OAuth session token (user logged in)
      2. GITHUB_TOKEN from .env (fallback)
    """
    # If user is logged in via OAuth, temporarily set the env var
    oauth_token = session.get("github_token")
    if oauth_token:
        old_token = os.environ.get("GITHUB_TOKEN")
        os.environ["GITHUB_TOKEN"] = oauth_token
        client = GitHubClient()
        # Restore old token
        if old_token:
            os.environ["GITHUB_TOKEN"] = old_token
        else:
            os.environ.pop("GITHUB_TOKEN", None)
        return client

    # Fallback to .env token
    return GitHubClient()


@app.route("/api/rank", methods=["POST"])
def api_rank():
    """
    API endpoint: Resolve location, search GitHub, enrich profiles, rank.

    Expects JSON body:
      { "username": "jd316", "pincode": "743165", "country": "in" }

    Returns JSON with location, user stats, ranked leaderboard, chart data.
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON body."}), 400

    username = data.get("username", "").strip()
    pincode = data.get("pincode", "").strip()
    country = data.get("country", "in").strip().lower()
    max_enrich = int(data.get("max_enrich", 100))

    if not username or not pincode:
        return jsonify({"error": "Both 'username' and 'pincode' are required."}), 400

    # ── Step 1: Resolve PIN code ──────────────────────────────
    location = PinResolver.resolve(pincode, country)
    if not location:
        return jsonify({"error": f"Could not resolve PIN code '{pincode}'. The postal API may be slow — please try again."}), 502

    location_data = {
        "pin_code": location.pin_code,
        "town": location.town or "",
        "district": location.district or "",
        "region": location.region or "",
        "nearest_city": location.nearest_city() or "",
        "state": location.state or "",
        "country": location.country or "",
        "post_office_name": location.post_office_name or "",
        "display_name": location.display_name(),
        "search_terms": location.search_terms(),
    }

    # ── Step 2: Search GitHub ─────────────────────────────────
    client = _get_github_client()
    search_terms = location.search_terms()
    all_users = {}
    search_stats = []

    for term in search_terms:
        results = client.search_users_by_location(term)
        new_count = 0
        for user in results:
            login = user["login"].lower()
            if login not in all_users:
                all_users[login] = user
                new_count += 1
        search_stats.append({
            "term": term,
            "found": len(results),
            "new": new_count,
        })

    # Ensure the target user is included
    target_in_search = username.lower() in all_users
    if not target_in_search:
        if client.check_user_exists(username):
            all_users[username.lower()] = {
                "login": username,
                "html_url": f"https://github.com/{username}",
                "avatar_url": "",
            }
        else:
            return jsonify({"error": f"GitHub user '{username}' does not exist."}), 404

    unique_list = list(all_users.values())

    # ── Step 3: Enrich profiles ───────────────────────────────
    target_key = username.lower()
    to_enrich = unique_list.copy()
    for i, u in enumerate(to_enrich):
        if u["login"].lower() == target_key:
            to_enrich.insert(0, to_enrich.pop(i))
            break

    if len(to_enrich) > max_enrich:
        to_enrich = to_enrich[:max_enrich]

    enriched = []
    for user_stub in to_enrich:
        profile = client.get_user_profile(user_stub["login"])
        if profile:
            gu = GitHubUser(
                username=profile["login"],
                profile_url=profile.get("html_url", ""),
                avatar_url=profile.get("avatar_url", ""),
                name=profile.get("name", "") or "",
                location=profile.get("location", "") or "",
                bio=profile.get("bio", "") or "",
                followers=profile.get("followers", 0),
                following=profile.get("following", 0),
                public_repos=profile.get("public_repos", 0),
                public_gists=profile.get("public_gists", 0),
                created_at=profile.get("created_at", ""),
            )
            enriched.append(gu)
        time.sleep(0.2 if client.authenticated else 1.0)

    if not enriched:
        return jsonify({"error": "Could not fetch any profiles. Likely rate-limited."}), 429

    # ── Step 4: Rank ──────────────────────────────────────────
    ranked = Ranker.rank_users(enriched)

    # ── Step 5: Build response ────────────────────────────────
    target_rank = None
    target_user_data = None
    leaderboard = []

    for i, user in enumerate(ranked):
        entry = {
            "rank": i + 1,
            "username": user.username,
            "name": user.name or "",
            "location": user.location or "",
            "bio": user.bio or "",
            "avatar_url": user.avatar_url,
            "profile_url": user.profile_url,
            "followers": user.followers,
            "following": user.following,
            "public_repos": user.public_repos,
            "public_gists": user.public_gists,
            "created_at": user.created_at,
            "score": user.score,
        }

        # Compute score breakdown for charts
        followers_score = math.log1p(user.followers) * 0.40
        repos_score = math.log1p(user.public_repos) * 0.30
        gists_score = math.log1p(user.public_gists) * 0.05
        age_score = 0.0
        if user.created_at:
            try:
                created = datetime.strptime(user.created_at, "%Y-%m-%dT%H:%M:%SZ")
                age_years = (datetime.now(timezone.utc).replace(tzinfo=None) - created).days / 365.25
                age_score = math.log1p(max(0, age_years)) * 0.25
            except ValueError:
                pass

        entry["score_breakdown"] = {
            "followers": round(followers_score, 4),
            "repos": round(repos_score, 4),
            "gists": round(gists_score, 4),
            "account_age": round(age_score, 4),
        }

        leaderboard.append(entry)

        if user.username.lower() == target_key:
            target_rank = i + 1
            target_user_data = entry

    total = len(ranked)
    percentile = round(((total - target_rank) / total) * 100, 1) if target_rank and total > 1 else 0.0

    return jsonify({
        "location": location_data,
        "search_stats": search_stats,
        "total_found": len(unique_list),
        "total_enriched": total,
        "authenticated": client.authenticated,
        "target": {
            "username": username,
            "rank": target_rank,
            "percentile": percentile,
            "top_percent": round(100 - percentile, 1) if target_rank else None,
            "found_in_search": target_in_search,
            "data": target_user_data,
        },
        "leaderboard": leaderboard,
    })


@app.route("/api/oauth-status")
def api_oauth_status():
    """Check if GitHub OAuth is configured."""
    return jsonify({"configured": bool(GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET)})


# ─────────────────────────────────────────────────────
#  SHARE (Upstash Redis)
# ─────────────────────────────────────────────────────

SHARE_TTL = 7 * 24 * 3600  # 7 days

@app.route("/api/share", methods=["POST"])
def api_share():
    """Store result in Redis, return share URL. Requires KV/Upstash env vars."""
    redis = _get_redis()
    if not redis:
        return jsonify({"error": "Share not configured. Add Upstash Redis via Vercel Storage."}), 503

    data = request.get_json()
    if not data or "location" not in data or "target" not in data:
        return jsonify({"error": "Invalid payload."}), 400

    share_id = secrets.token_urlsafe(8)[:10]
    key = f"share:{share_id}"
    created_at = datetime.now(timezone.utc).isoformat()
    payload = {"data": data, "created_at": created_at}
    try:
        redis.set(key, json.dumps(payload), ex=SHARE_TTL)
    except Exception:
        return jsonify({"error": "Failed to save."}), 500

    base = request.url_root.rstrip("/")
    url = f"{base}/share/{share_id}"
    return jsonify({"id": share_id, "url": url, "created_at": created_at, "expires_in_days": 7})


@app.route("/api/share/<share_id>")
def api_share_get(share_id):
    """Fetch shared result from Redis."""
    redis = _get_redis()
    if not redis:
        return jsonify({"error": "Share not configured."}), 503

    key = f"share:{share_id}"
    try:
        raw = redis.get(key)
    except Exception:
        return jsonify({"error": "Storage error."}), 500
    if not raw:
        return jsonify({"error": "Link expired or not found."}), 404
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and "data" in parsed:
            result = {**parsed["data"], "created_at": parsed.get("created_at")}
        else:
            result = parsed
        return jsonify(result)
    except (json.JSONDecodeError, TypeError):
        return jsonify({"error": "Invalid data."}), 500


@app.route("/share/<share_id>")
def share_redirect(share_id):
    """Redirect to main app with share param."""
    return redirect(url_for("index", s=share_id))


if __name__ == "__main__":
    app.run(host="localhost", debug=True, port=5000)
