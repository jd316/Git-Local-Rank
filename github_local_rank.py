#!/usr/bin/env python3
"""
Git Local Rank Checker
─────────────────────────
Resolves PIN/ZIP codes to actual geographic locations, searches GitHub
for developers in that area, enriches their profiles, computes a
composite ranking score, and shows where you stand among local devs.

Architecture:
  PinResolver     → Maps PIN/ZIP to city, district, state, country
  GitHubClient    → Handles all GitHub API calls (search + profile)
  Ranker          → Computes composite scores and ranks users
  Display         → Rich terminal output formatting
  Orchestrator    → Ties everything together in a clean pipeline

Usage:
  python3 github_local_rank.py -u <username> -p <pincode>
  python3 github_local_rank.py -u <username> -p <pincode> -c us
  GITHUB_TOKEN=ghp_xxx python3 github_local_rank.py -u jd316 -p 743165
"""

import os
import sys
import math
import time
import json
import shutil
import argparse
import subprocess
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

try:
    import requests
except ImportError:
    print("╔════════════════════════════════════════════════════╗")
    print("║  [!] 'requests' library is required.              ║")
    print("║  Run: pip install requests                        ║")
    print("╚════════════════════════════════════════════════════╝")
    sys.exit(1)

# Auto-load .env file (GITHUB_TOKEN, etc.) if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional; user can still export vars manually


# ═══════════════════════════════════════════════════════════════
#  INDIAN CITY NAME ALIASES (colonial ↔ modern, common variants)
# ═══════════════════════════════════════════════════════════════

INDIAN_CITY_ALIASES: Dict[str, List[str]] = {
    "calcutta": ["kolkata"],
    "kolkata": ["calcutta"],
    "bombay": ["mumbai"],
    "mumbai": ["bombay"],
    "madras": ["chennai"],
    "chennai": ["madras"],
    "bangalore": ["bengaluru"],
    "bengaluru": ["bangalore"],
    "poona": ["pune"],
    "pune": ["poona"],
    "trivandrum": ["thiruvananthapuram"],
    "thiruvananthapuram": ["trivandrum"],
    "baroda": ["vadodara"],
    "vadodara": ["baroda"],
    "cochin": ["kochi"],
    "kochi": ["cochin"],
    "benares": ["varanasi"],
    "varanasi": ["benares"],
    "simla": ["shimla"],
    "shimla": ["simla"],
    "pondicherry": ["puducherry"],
    "puducherry": ["pondicherry"],
    "allahabad": ["prayagraj"],
    "prayagraj": ["allahabad"],
    "cawnpore": ["kanpur"],
    "kanpur": ["cawnpore"],
    "mysore": ["mysuru"],
    "mysuru": ["mysore"],
    "mangalore": ["mangaluru"],
    "mangaluru": ["mangalore"],
    "vizag": ["visakhapatnam"],
    "visakhapatnam": ["vizag"],
    "gurgaon": ["gurugram"],
    "gurugram": ["gurgaon"],
    "ooty": ["udhagamandalam"],
    "udhagamandalam": ["ooty"],
    "calicut": ["kozhikode"],
    "kozhikode": ["calicut"],
    "hubli": ["hubballi"],
    "hubballi": ["hubli"],
    "belgaum": ["belagavi"],
    "belagavi": ["belgaum"],
    "shimoga": ["shivamogga"],
    "shivamogga": ["shimoga"],
    "tumkur": ["tumakuru"],
    "tumakuru": ["tumkur"],
}


def _get_aliases(name: str) -> List[str]:
    """Get known aliases for a city/region name."""
    key = name.strip().lower()
    aliases = INDIAN_CITY_ALIASES.get(key, [])
    return [a.title() for a in aliases]


# ═══════════════════════════════════════════════════════════════
#  DATA MODELS
# ═══════════════════════════════════════════════════════════════

@dataclass
class Location:
    """
    Resolved geographic location from a PIN/ZIP code.

    Geographic hierarchy (most specific → broadest):
      post_office_name  →  town  →  district  →  region  →  state  →  country

    The 'region' field represents the nearest major city
    (e.g., "Calcutta" for PIN 743165 near Kolkata).
    """
    pin_code: str
    town: str = ""             # Block / town name (e.g., Naihati)
    district: str = ""         # District (e.g., North 24 Parganas)
    region: str = ""           # Nearest major city (e.g., Calcutta)
    state: str = ""            # State (e.g., West Bengal)
    country: str = ""          # Country (e.g., India)
    post_office_name: str = ""

    def search_terms(self) -> List[str]:
        """
        Returns a comprehensive, de-duplicated list of search terms
        ordered from most specific to broadest:

          1. Town/block name          (Naihati)
          2. District                 (North 24 Parganas)
          3. Region / nearest city    (Calcutta)
          4. City name aliases         (Kolkata ← alias of Calcutta)
          5. PIN code                 (743165)

        This ensures maximum coverage since GitHub users might set
        their location to any of these variations.
        """
        terms: List[str] = []
        seen: set = set()

        # Build candidate list: town, district, region
        candidates = [self.town, self.district, self.region]

        # Expand each candidate with known aliases
        expanded: List[str] = []
        for c in candidates:
            if c and c.strip():
                expanded.append(c.strip())
                # Add all known aliases for this name
                for alias in _get_aliases(c):
                    expanded.append(alias)

        # Always search by exact PIN code too
        expanded.append(self.pin_code)

        # De-duplicate while preserving order
        for term in expanded:
            normalized = term.strip().lower()
            if normalized and normalized not in seen:
                terms.append(term.strip())
                seen.add(normalized)

        return terms

    def display_name(self) -> str:
        """Human-readable area name for display."""
        # Prefer region (major city) if available, otherwise town
        primary = self.region or self.town
        
        parts = []
        for p in [primary, self.district, self.state]:
            # Deduplicate items (e.g., avoid "Chicago, Chicago, Illinois")
            if p and p not in parts:
                parts.append(p)
                
        return ", ".join(parts) if parts else self.pin_code

    def nearest_city(self) -> str:
        """Returns the nearest major city name (modern spelling)."""
        if self.region:
            aliases = _get_aliases(self.region)
            # Return modern name if alias exists, else the region itself
            return aliases[0] if aliases else self.region
        return self.town or self.district


@dataclass
class GitHubUser:
    """Enriched GitHub user profile with computed ranking score."""
    username: str
    profile_url: str = ""
    avatar_url: str = ""
    name: str = ""
    location: str = ""
    bio: str = ""
    followers: int = 0
    following: int = 0
    public_repos: int = 0
    public_gists: int = 0
    created_at: str = ""
    score: float = 0.0


# ═══════════════════════════════════════════════════════════════
#  PIN CODE RESOLVER
# ═══════════════════════════════════════════════════════════════

class PinResolver:
    """
    Resolves PIN/ZIP codes to geographic locations.
    
    Supported backends:
      • OpenStreetMap (Nominatim — free, global) — Primary Method
      • India Post API  (Indian PIN codes, 6 digits) — via curl subprocess
      • Zippopotam.us   (US ZIP, UK postcodes, DE PLZ, etc.)
    """

    NOMINATIM_API = "https://nominatim.openstreetmap.org/search?postalcode={pin}&countrycodes={country}&format=json&addressdetails=1"
    INDIA_POST_API = "https://api.postalpincode.in/pincode/{pin}"
    ZIPPOPOTAM_API = "https://api.zippopotam.us/{country}/{pin}"

    @staticmethod
    def resolve(pin_code: str, country_code: str = "in") -> Optional[Location]:
        """Resolve a PIN/ZIP code to a Location object."""
        country_code = country_code.strip().lower()
        if country_code == "uk":
            country_code = "gb"  # OSM & Zippopotam use 'gb' for the United Kingdom
            
        # 1. Primary: OpenStreetMap Nominatim (Free, Global, and Very Reliable)
        location = PinResolver._resolve_osm(pin_code, country_code)
        if location:
            return location

        # 2. Fallback for India: India Post API
        if country_code == "in":
            location = PinResolver._resolve_india(pin_code)
            if location:
                return location

        # 3. Fallback for others: Zippopotam
        return PinResolver._resolve_zippopotam(pin_code, country_code)

    @staticmethod
    def _resolve_osm(pin_code: str, country_code: str) -> Optional[Location]:
        """Resolve using OpenStreetMap's Nominatim API (Free, no key required)."""
        try:
            url = PinResolver.NOMINATIM_API.format(pin=pin_code, country=country_code)
            headers = {"User-Agent": "GitHubLocalRank/1.0 (https://github.com/jd316/git-local-rank)"}
            
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            if data and isinstance(data, list) and len(data) > 0:
                place = data[0]
                addr = place.get("address", {})
                
                # Fetch best regional grouping: city / town / county
                town = addr.get("city", addr.get("town", addr.get("village", addr.get("county", ""))))
                district = addr.get("state_district", addr.get("county", ""))
                state = addr.get("state", "")
                country = addr.get("country", "")
                
                # In OpenStreetMap, the display_name usually works as a region identifier
                return Location(
                    pin_code=pin_code,
                    town=town,
                    district=district,
                    region=town,
                    state=state,
                    country=country
                )
        except Exception as e:
            print(f"  [!] OpenStreetMap API error: {e}")
            
        return None

    @staticmethod
    def _parse_india_post_json(data: Any, pin_code: str) -> Optional[Location]:
        """
        Parse India Post API response JSON into a Location object.

        India Post API fields used:
          Name     → Post office name
          Block    → Town/block (often "NA" for urban areas)
          District → District name
          Region   → Nearest major city (key for mapping!)
          State    → State name
          Country  → Always "India"
        """
        if data and data[0].get("Status") == "Success":
            post_offices = data[0].get("PostOffice", [])
            if post_offices:
                po = post_offices[0]

                # Extract town: prefer Block, fall back to Name
                town = po.get("Block", "") or ""
                if not town or town.upper() == "NA":
                    town = po.get("Name", "")
                if town.upper() == "NA":
                    town = ""

                # Extract region (nearest major city) — this is the key field!
                region = po.get("Region", "") or ""
                if region.upper() == "NA":
                    region = ""

                return Location(
                    pin_code=pin_code,
                    town=town,
                    district=po.get("District", ""),
                    region=region,
                    state=po.get("State", ""),
                    country="India",
                    post_office_name=po.get("Name", ""),
                )
        return None

    @staticmethod
    def _resolve_india(pin_code: str) -> Optional[Location]:
        """
        Resolve using the India Post API.
        
        Uses curl subprocess as primary method (the API often blocks
        Python's requests library due to User-Agent filtering), with
        requests as fallback. Retries up to 3 times on timeout.
        """
        url = PinResolver.INDIA_POST_API.format(pin=pin_code)

        for attempt in range(3):
            timeout_secs = 10 + (attempt * 5)  # 10s, 15s, 20s

            # ── Primary: curl subprocess (more reliable) ──
            if shutil.which("curl"):
                try:
                    result = subprocess.run(
                        ["curl", "-s", "--max-time", str(timeout_secs), url],
                        capture_output=True, text=True, timeout=timeout_secs + 5,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        data = json.loads(result.stdout)
                        location = PinResolver._parse_india_post_json(data, pin_code)
                        if location:
                            return location
                except subprocess.TimeoutExpired:
                    print(f"  [!] India Post API (curl) timed out (attempt {attempt + 1}/3).")
                except json.JSONDecodeError:
                    print("  [!] Invalid response from India Post API.")
                    break  # Don't retry bad JSON
                except Exception as e:
                    print(f"  [!] curl error: {e}")

            # ── Fallback: requests library ──
            try:
                resp = requests.get(url, timeout=timeout_secs, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                data = resp.json()
                location = PinResolver._parse_india_post_json(data, pin_code)
                if location:
                    return location
            except requests.exceptions.Timeout:
                print(f"  [!] India Post API timed out (attempt {attempt + 1}/3).")
            except requests.exceptions.ConnectionError:
                print("  [!] Cannot reach India Post API.")
            except Exception as e:
                print(f"  [!] India Post API error: {e}")
                break  # Don't retry non-timeout errors

            if attempt < 2:
                time.sleep(1)  # Brief pause before retry

        return None

    @staticmethod
    def _resolve_zippopotam(pin_code: str, country_code: str) -> Optional[Location]:
        """Resolve using Zippopotam.us (supports US, UK, DE, FR, etc.)."""
        try:
            url = PinResolver.ZIPPOPOTAM_API.format(country=country_code, pin=pin_code)
            resp = requests.get(url, timeout=10)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()

            places = data.get("places", [])
            if places:
                place = places[0]
                return Location(
                    pin_code=pin_code,
                    town=place.get("place name", ""),
                    district=place.get("place name", ""),
                    region="",
                    state=place.get("state", ""),
                    country=data.get("country", ""),
                )
        except Exception as e:
            print(f"  [!] Zippopotam API error: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
#  GITHUB API CLIENT
# ═══════════════════════════════════════════════════════════════

class GitHubClient:
    """
    Handles all GitHub API interactions.
    
    Features:
      • Automatic auth via GITHUB_TOKEN env var
      • Rate limit detection and auto-wait (up to 60s)
      • Request timeouts
      • Polite request spacing to avoid abuse limits
    """

    BASE_URL = "https://api.github.com"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/vnd.github.v3+json"})

        token = os.environ.get("GITHUB_TOKEN")
        if token:
            self.session.headers.update({"Authorization": f"token {token}"})
            self.authenticated = True
        else:
            self.authenticated = False

    def _check_rate_limit(self, response: requests.Response) -> bool:
        """
        Inspect rate-limit headers. If exhausted and reset is ≤60s away,
        sleep and signal the caller to retry. Returns True if caller should retry.
        """
        remaining = int(response.headers.get("X-RateLimit-Remaining", -1))
        reset_ts = int(response.headers.get("X-RateLimit-Reset", 0))

        if response.status_code == 403 and remaining == 0 and reset_ts > 0:
            wait = max(0, reset_ts - int(time.time())) + 1
            if wait <= 60:
                print(f"  ⏳ Rate limit hit. Auto-waiting {wait}s...")
                time.sleep(wait)
                return True
            else:
                print(f"  [!] Rate limit exceeded. Resets in {wait}s.")
                if not self.authenticated:
                    print("  💡 Tip: export GITHUB_TOKEN=ghp_... for 5000 req/hr instead of 10 req/min.")
                return False

        if response.status_code == 403:
            print("  [!] 403 Forbidden — possibly secondary rate limit. Pausing 30s...")
            time.sleep(30)
            return True

        return False

    def search_users_by_location(self, location_term: str, max_pages: int = 5) -> List[Dict[str, Any]]:
        """Search GitHub users by a location string. Returns raw user dicts."""
        users: List[Dict[str, Any]] = []
        page = 1
        safe_term = location_term.replace('"', '').replace('\\', '').strip()

        while page <= max_pages:
            url = (
                f"{self.BASE_URL}/search/users"
                f"?q=location:\"{safe_term}\""
                f"&sort=followers&order=desc&per_page=100&page={page}"
            )
            try:
                resp = self.session.get(url, timeout=15)

                if resp.status_code == 403:
                    if self._check_rate_limit(resp):
                        continue  # retry same page
                    break

                resp.raise_for_status()
                data = resp.json()

                items = data.get("items", [])
                total_count = data.get("total_count", 0)

                if not items:
                    break

                users.extend(items)

                if len(users) >= total_count or "next" not in resp.links:
                    break

                page += 1
                time.sleep(2 if not self.authenticated else 0.5)

            except requests.exceptions.Timeout:
                print(f"  [!] Timeout searching '{location_term}'. Moving on.")
                break
            except requests.exceptions.ConnectionError:
                print("  [!] Connection error. Check your internet.")
                break
            except Exception as e:
                print(f"  [!] Search error: {e}")
                break

        return users

    def get_user_profile(self, username: str) -> Optional[Dict[str, Any]]:
        """Fetch full profile for a single user. Returns None on failure."""
        max_retries = 2
        for attempt in range(max_retries):
            try:
                url = f"{self.BASE_URL}/users/{username}"
                resp = self.session.get(url, timeout=10)

                if resp.status_code == 403:
                    if self._check_rate_limit(resp):
                        continue
                    return None

                if resp.status_code == 404:
                    return None

                resp.raise_for_status()
                return resp.json()

            except requests.exceptions.Timeout:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return None
            except Exception:
                return None

        return None

    def check_user_exists(self, username: str) -> bool:
        """Quick check if a GitHub username exists."""
        try:
            resp = self.session.get(f"{self.BASE_URL}/users/{username}", timeout=10)
            return resp.status_code == 200
        except Exception:
            return False


# ═══════════════════════════════════════════════════════════════
#  RANKER — Composite Scoring Engine
# ═══════════════════════════════════════════════════════════════

class Ranker:
    """
    Computes composite ranking scores for GitHub users.

    Score formula (log-scaled to dampen outliers):
      score = 0.40 × log(1 + followers)
            + 0.30 × log(1 + public_repos)
            + 0.05 × log(1 + public_gists)
            + 0.25 × log(1 + account_age_years)
    """

    WEIGHTS = {
        "followers": 0.40,
        "public_repos": 0.30,
        "public_gists": 0.05,
        "account_age_years": 0.25,
    }

    @staticmethod
    def compute_score(user: GitHubUser) -> float:
        """Compute a composite ranking score for a single user."""
        score = 0.0
        score += math.log1p(user.followers) * Ranker.WEIGHTS["followers"]
        score += math.log1p(user.public_repos) * Ranker.WEIGHTS["public_repos"]
        score += math.log1p(user.public_gists) * Ranker.WEIGHTS["public_gists"]

        if user.created_at:
            try:
                created = datetime.strptime(user.created_at, "%Y-%m-%dT%H:%M:%SZ")
                age_years = (datetime.now(timezone.utc).replace(tzinfo=None) - created).days / 365.25
                score += math.log1p(max(0, age_years)) * Ranker.WEIGHTS["account_age_years"]
            except ValueError:
                pass

        return round(score, 4)

    @staticmethod
    def rank_users(users: List[GitHubUser]) -> List[GitHubUser]:
        """Rank users by composite score (descending)."""
        for user in users:
            user.score = Ranker.compute_score(user)
        return sorted(users, key=lambda u: u.score, reverse=True)


# ═══════════════════════════════════════════════════════════════
#  DISPLAY — Rich Terminal Output
# ═══════════════════════════════════════════════════════════════

class Display:
    """Handles all terminal output formatting."""

    BANNER = r"""
╔══════════════════════════════════════════════════════════╗
║                                                          ║
║   🌍  Git Local Rank Checker                             ║
║   ──  Find your rank among developers in your area       ║
║                                                          ║
║   Powered by: India Post API + GitHub Search API         ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝"""

    @staticmethod
    def banner():
        print(Display.BANNER)

    @staticmethod
    def step(number: int, text: str):
        print(f"\n{'━' * 55}")
        print(f"  Step {number}: {text}")
        print(f"{'━' * 55}")

    @staticmethod
    def location_card(location: Location):
        nearest = location.nearest_city()
        search_terms = location.search_terms()

        print("\n  ┌───────────────────────────────────────────────┐")
        print("  │  📍 Resolved Location                          │")
        print("  ├───────────────────────────────────────────────┤")
        print(f"  │  PIN Code      : {location.pin_code or 'N/A':<28}│")
        print(f"  │  Post Office   : {location.post_office_name or 'N/A':<28}│")
        print(f"  │  Town/Block    : {location.town or 'N/A':<28}│")
        print(f"  │  District      : {location.district or 'N/A':<28}│")
        print(f"  │  Nearest City  : {nearest or 'N/A':<28}│")
        print(f"  │  Region (API)  : {location.region or 'N/A':<28}│")
        print(f"  │  State         : {location.state or 'N/A':<28}│")
        print(f"  │  Country       : {location.country or 'N/A':<28}│")
        print("  ├───────────────────────────────────────────────┤")
        print("  │  🔎 Will search GitHub for:                    │")
        for t in search_terms:
            print(f"  │     • {t:<40}│")
        print("  └───────────────────────────────────────────────┘")

    @staticmethod
    def search_progress(term: str, found: int):
        print(f"    🔎 \"{term}\" → {found} users found")

    @staticmethod
    def enrichment_progress(current: int, total: int):
        bar_len = 30
        filled = int(bar_len * current / total) if total > 0 else 0
        bar = "█" * filled + "░" * (bar_len - filled)
        pct = int(100 * current / total) if total > 0 else 0
        print(f"\r    📊 Enriching profiles: [{bar}] {pct}% ({current}/{total})", end="", flush=True)

    @staticmethod
    def results(target_username: str, location: Location, ranked_users: List[GitHubUser]):
        total = len(ranked_users)
        area = location.display_name()

        if total == 0:
            print("\n  No developers found in this area on GitHub.")
            return

        # Find target user
        target_rank = None
        target_user = None
        for i, user in enumerate(ranked_users):
            if user.username.lower() == target_username.lower():
                target_rank = i + 1
                target_user = user
                break

        # Header
        print(f"\n╔{'═' * 55}╗")
        print(f"║  📊 RANKING RESULTS{' ' * 36}║")
        print(f"╠{'═' * 55}╣")
        print(f"║  Area       : {area[:40]:<41}║")
        print(f"║  Developers : {total:<41}║")

        if target_rank and target_user:
            percentile = round(((total - target_rank) / total) * 100, 1) if total > 1 else 100.0

            # Medal
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            medal = medals.get(target_rank, "⭐" if target_rank <= 10 else "📌")

            print(f"╠{'═' * 55}╣")
            print(f"║  {medal} YOUR RANK: #{target_rank} / {total}{' ' * max(0, 38 - len(str(target_rank)) - len(str(total)))}║")
            print(f"║  📈 You are in the top {100 - percentile:.1f}%{' ' * max(0, 32 - len(f'{100-percentile:.1f}'))}║")
            print(f"╠{'═' * 55}╣")
            print(f"║  👤 Username      : {target_user.username:<35}║")
            print(f"║  📛 Name          : {(target_user.name or 'N/A'):<35}║")
            print(f"║  📍 Location      : {(target_user.location or 'N/A')[:35]:<35}║")
            print(f"║  👥 Followers     : {target_user.followers:<35}║")
            print(f"║  📦 Public Repos  : {target_user.public_repos:<35}║")
            print(f"║  🏆 Composite Score: {target_user.score:<34}║")
        else:
            nearest = location.nearest_city()
            search_terms = location.search_terms()
            print(f"╠{'═' * 55}╣")
            print(f"║  ❌ '{target_username}' not found among local developers.    ║")
            print("║                                                       ║")
            print("║  💡 Your GitHub Location field should include one of:  ║")
            for st in search_terms[:4]:
                print(f"║     • {st:<49}║")
            print("║                                                       ║")
            print("║  🔧 Recommended: Set your GitHub Location to          ║")
            print(f"║     \"{nearest}\" or \"{location.district}\"{' ' * max(0, 30 - len(nearest) - len(location.district))}║")
            print("║                                                       ║")
            print("║  Go to: github.com/settings/profile → Location        ║")

        print(f"╚{'═' * 55}╝")

        # Leaderboard
        show_count = min(50, total)
        print(f"\n  🏆 LEADERBOARD — Top {show_count} in {area}")
        print(f"  {'─' * 92}")
        print(f"  {'#':<4} {'Username':<22} {'Followers':>10} {'Repos':>7} {'Score':>8}   {'Profile Link'}")
        print(f"  {'─' * 92}")

        for i, user in enumerate(ranked_users[:show_count]):
            is_target = user.username.lower() == target_username.lower()
            marker = " ◄── YOU" if is_target else ""
            highlight_start = "» " if is_target else "  "
            print(
                f"{highlight_start}{i + 1:<4}"
                f"{user.username[:20]:<22}"
                f"{user.followers:>10}"
                f"{user.public_repos:>7}"
                f"{user.score:>8.2f}   "
                f"{user.profile_url}"
                f"{marker}"
            )

        print(f"  {'─' * 92}")

        # Scoring breakdown
        print("\n  ℹ️  Scoring formula:")
        print("      40% log(followers) + 30% log(repos) + 5% log(gists) + 25% log(account age)")
        print()


# ═══════════════════════════════════════════════════════════════
#  ORCHESTRATOR — Main Pipeline
# ═══════════════════════════════════════════════════════════════

def run(username: str, pin_code: str, country: str = "in", max_enrich: int = 100):
    """
    Main pipeline:
      1. Resolve PIN → Location
      2. Search GitHub by city / district / PIN
      3. Enrich top-N user profiles
      4. Compute composite rankings
      5. Display results
    """

    Display.banner()

    # ── Step 1: Resolve PIN ────────────────────────────────────
    Display.step(1, "Resolving PIN code to geographic location")

    location = PinResolver.resolve(pin_code, country)

    if not location:
        print(f"\n  [!] Could not resolve PIN code '{pin_code}'.")
        print("  • Double-check the PIN code is valid.")
        print("  • If using a non-Indian code, pass --country <code> (e.g., us, gb, de).")
        return

    Display.location_card(location)

    # ── Step 2: Search GitHub ──────────────────────────────────
    Display.step(2, "Searching GitHub for developers in your area")

    client = GitHubClient()

    if client.authenticated:
        print("  ✅ Authenticated via GITHUB_TOKEN (higher rate limits)\n")
    else:
        print("  ⚠️  Unauthenticated mode — limited to ~10 searches/min")
        print("  💡 For better results: export GITHUB_TOKEN=ghp_yourtoken\n")

    search_terms = location.search_terms()
    all_users: Dict[str, Dict[str, Any]] = {}

    for term in search_terms:
        results = client.search_users_by_location(term)
        new_count = 0
        for user in results:
            login = user["login"].lower()
            if login not in all_users:
                all_users[login] = user
                new_count += 1
        Display.search_progress(term, len(results))
        if len(results) > 0:
            print(f"      ({new_count} new unique users added)")

    # Ensure the target user is always included
    if username.lower() not in all_users:
        print(f"\n  🔍 Target user '{username}' not in search results — fetching directly...")
        if client.check_user_exists(username):
            all_users[username.lower()] = {
                "login": username,
                "html_url": f"https://github.com/{username}",
                "avatar_url": "",
            }
            print(f"  ✅ '{username}' exists on GitHub — will include in ranking.")
        else:
            print(f"  ❌ GitHub user '{username}' does not exist. Please check the username.")
            return

    unique_list = list(all_users.values())
    print(f"\n  🎯 Total unique developers found: {len(unique_list)}")

    if not unique_list:
        print("  No developers found. Try a different location or broader search.")
        return

    # ── Step 3: Enrich Profiles ────────────────────────────────
    Display.step(3, "Fetching detailed profiles for each developer")

    # IMPORTANT: Ensure the target user is at the FRONT of the
    # enrichment queue so they never get cut off by the cap.
    target_key = username.lower()
    to_enrich = unique_list.copy()

    # Move target user to the front
    target_idx = None
    for i, u in enumerate(to_enrich):
        if u["login"].lower() == target_key:
            target_idx = i
            break
    if target_idx is not None and target_idx > 0:
        target_entry = to_enrich.pop(target_idx)
        to_enrich.insert(0, target_entry)
        print(f"  ✅ Target user '{username}' prioritized for enrichment.")

    if len(to_enrich) > max_enrich:
        print(f"  ℹ️  Capping enrichment at top {max_enrich} users to conserve API calls.")
        print(f"      (Target user '{username}' is guaranteed to be included.)")
        to_enrich = to_enrich[:max_enrich]

    total = len(to_enrich)
    enriched: List[GitHubUser] = []
    failed = 0

    for idx, user_stub in enumerate(to_enrich):
        Display.enrichment_progress(idx + 1, total)

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
        else:
            failed += 1

        # Polite spacing
        time.sleep(0.3 if client.authenticated else 1.2)

    print()  # newline after progress bar
    print(f"\n  ✅ Successfully enriched {len(enriched)} profiles" +
          (f" ({failed} failed)" if failed else ""))

    if not enriched:
        print("  [!] Could not fetch any profiles. Likely rate-limited.")
        print("  💡 Try again with: export GITHUB_TOKEN=ghp_yourtoken")
        return

    # ── Step 4: Rank ───────────────────────────────────────────
    Display.step(4, "Computing composite rankings")
    ranked = Ranker.rank_users(enriched)
    print(f"  ✅ Ranked {len(ranked)} developers by composite score.")

    # ── Step 5: Display Results ────────────────────────────────
    Display.step(5, "Results")
    Display.results(username, location, ranked)


# ═══════════════════════════════════════════════════════════════
#  CLI ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="🌍 Git Local Rank Checker — Find your rank among local developers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 github_local_rank.py -u jd316 -p 743165
  python3 github_local_rank.py -u octocat -p 94107 -c us
  GITHUB_TOKEN=ghp_xxx python3 github_local_rank.py -u jd316 -p 743165

Scoring:
  Composite score = 40%% log(followers) + 30%% log(repos)
                  + 5%% log(gists) + 25%% log(account_age)

Environment:
  GITHUB_TOKEN    GitHub PAT for higher API rate limits (recommended)
        """,
    )
    parser.add_argument("-u", "--username", type=str, help="Your GitHub username")
    parser.add_argument("-p", "--pincode", type=str, help="PIN / ZIP code")
    parser.add_argument(
        "-c", "--country", type=str, default="in",
        help="2-letter country code (default: 'in'). Supports: in, us, gb, de, fr, etc.",
    )
    parser.add_argument(
        "-n", "--max-enrich", type=int, default=100,
        help="Max users to fetch full profiles for (default: 100)",
    )
    args = parser.parse_args()

    try:
        username = args.username or input("Enter your GitHub username: ").strip()
        pincode = args.pincode or input("Enter your PIN code / ZIP code: ").strip()

        if not username or not pincode:
            print("\n  [!] Both username and PIN code are required.")
            sys.exit(1)

        run(username, pincode, args.country, args.max_enrich)

    except KeyboardInterrupt:
        print("\n\n  Operation cancelled by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
