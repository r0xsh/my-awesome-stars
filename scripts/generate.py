#!/usr/bin/env python3
"""Static file generator for my-awesome-stars.

Parses README.md (by language) + topics.md (by topic/tag), optionally enriches
via the GitHub API (star count, starred_at date, canonical description/topics),
caches API results in data/stars-cache.json, and emits dist/index.html +
dist/stars.json — a static card gallery with filters.

Usage:
    python3 scripts/generate.py [--username r0xsh] [--no-fetch] [--rebuild-cache]
                                [--readme README.md] [--topics topics.md]
                                [--cache data/stars-cache.json] [--out dist]

Env:
    GITHUB_TOKEN / GH_TOKEN  (optional but recommended: higher rate limits +
                             access to starred_at dates for your own stars)
    GITHUB_USERNAME          (fallback for --username)
"""
import argparse
import datetime
import html
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

BULLET_RE = re.compile(
    r"^\s*-\s*\[(?P<full>[^/\]]+/[^\]]+)\]\((?P<url>https?://[^)]+)\)\s*(?:-\s*(?P<desc>.*))?$"
)
SECTION_RE = re.compile(r"^##\s+(?P<section>.+?)\s*$")

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html")


def parse_md(path, kind):
    """Return dict full_name_lower -> {'full':, 'url':, 'desc_md':, kind_key}."""
    repos = {}
    section = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = SECTION_RE.match(line.rstrip("\n"))
            if m:
                name = m.group("section").strip()
                if name.lower() == "contents":
                    section = None
                else:
                    section = name
                continue
            b = BULLET_RE.match(line.rstrip("\n"))
            if b and section:
                full = b.group("full").strip()
                key = full.lower()
                entry = repos.setdefault(key, {"full": full, "url": b.group("url").strip(),
                                               "desc_md": (b.group("desc") or "").strip()})
                if kind == "language":
                    entry["language"] = section
                else:
                    entry.setdefault("topics", [])
                    if section not in entry["topics"]:
                        entry["topics"].append(section)
    # ensure topics key exists
    for v in repos.values():
        v.setdefault("topics", [])
    return repos


def merge(readme_repos, topics_repos):
    merged = {}
    for key, r in readme_repos.items():
        merged[key] = {"full": r["full"], "url": r["url"],
                       "desc_md": r.get("desc_md", ""),
                       "language": r.get("language", "Others"),
                       "topics": []}
    for key, t in topics_repos.items():
        e = merged.setdefault(key, {"full": t["full"], "url": t["url"],
                                    "desc_md": t.get("desc_md", ""),
                                    "language": "Others", "topics": []})
        if t.get("desc_md") and not e.get("desc_md"):
            e["desc_md"] = t["desc_md"]
        for tag in t.get("topics", []):
            if tag not in e["topics"]:
                e["topics"].append(tag)
    return merged


def gh_request(url, token, accept=None):
    headers = {"User-Agent": "my-awesome-stars-generator",
               "Accept": accept or "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data, dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        print(f"WARN: HTTP {e.code} for {url}: {body}", file=sys.stderr)
        if e.code in (403, 429):
            reset = e.headers.get("x-ratelimit-reset")
            if reset:
                wait = max(0, int(reset) - int(time.time()) + 2)
                print(f"Rate limited, sleeping {wait}s...", file=sys.stderr)
                time.sleep(min(wait, 300))
                return gh_request(url, token, accept)
        return None, {}
    except Exception as e:
        print(f"WARN: request failed for {url}: {e}", file=sys.stderr)
        return None, {}


def fetch_starred_map(username, token):
    """Map full_name.lower() -> starred_at (ISO str). Needs token for full history."""
    starred = {}
    page = 1
    accept = "application/vnd.github.star+json"
    while True:
        url = (f"https://api.github.com/users/{username}/starred"
               f"?per_page=100&page={page}")
        data, _ = gh_request(url, token, accept)
        if not data:
            break
        if not isinstance(data, list) or not data:
            break
        for item in data:
            repo = item.get("repo", {})
            full = (repo.get("full_name") or "").lower()
            if full:
                starred[full] = item.get("starred_at")
        if len(data) < 100:
            break
        page += 1
        if page > 50:  # safety: 5000 stars max
            break
        time.sleep(0.2)
    return starred


def fetch_repo(full, token):
    data, _ = gh_request(f"https://api.github.com/repos/{full}", token)
    return data


def build_records(merged, cache, starred_map, token, no_fetch):
    records = []
    for key in sorted(merged):
        m = merged[key]
        full = m["full"]
        cached = cache.get(key, {})
        api = None
        if not no_fetch and (key not in cache or not cached.get("fetched_at")):
            api = fetch_repo(full, token)
            time.sleep(0.15)  # be nice to rate limits
            if api and "message" not in api or (api and "full_name" in api):
                cached = {"repo": api, "fetched_at": datetime.datetime.now(
                    datetime.timezone.utc).isoformat()}
                cache[key] = cached
            else:
                api = cached.get("repo")
        else:
            api = cached.get("repo")
        if isinstance(api, dict) and api.get("full_name"):
            description = api.get("description") or m["desc_md"]
            language = api.get("language") or m.get("language") or "Others"
            topics = api.get("topics") or m.get("topics", [])
            stars = api.get("stargazers_count", 0)
            updated = api.get("pushed_at") or api.get("updated_at")
            homepage = api.get("homepage") or ""
            avatar = ((api.get("owner") or {}).get("avatar_url") or "")
        else:
            description = m["desc_md"]
            language = m.get("language") or "Others"
            topics = m.get("topics", [])
            stars = 0
            updated = None
            homepage = ""
            avatar = ""
        records.append({
            "full_name": full,
            "name": full.split("/", 1)[1] if "/" in full else full,
            "owner": full.split("/", 1)[0] if "/" in full else "",
            "url": m["url"] or f"https://github.com/{full}",
            "description": description or "",
            "language": language or "Others",
            "topics": sorted(topics),
            "stargazers": stars,
            "starred_at": starred_map.get(key) or cached.get("starred_at"),
            "repo_updated_at": updated,
            "homepage": homepage,
            "avatar": avatar,
            # Mimics GitHub's generated social preview ("the image og github generate")
            "image": f"https://opengraph.githubassets.com/1/{full}",
        })
    # persist starred_at into cache for offline runs
    for r in records:
        if r["starred_at"]:
            cache.setdefault(r["full_name"].lower(), {}).update(
                {"starred_at": r["starred_at"]})
    return records


def render_html(records):
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        tpl = f.read()
    payload = json.dumps(records, ensure_ascii=False)
    # escape closing script tag inside JSON strings
    payload = payload.replace("</", "<\\/")
    return tpl.replace("__STARS_JSON__", payload)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--username", default=os.environ.get("GITHUB_USERNAME", "r0xsh"))
    ap.add_argument("--readme", default="README.md")
    ap.add_argument("--topics", default="topics.md")
    ap.add_argument("--cache", default="data/stars-cache.json")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--no-fetch", action="store_true",
                    help="offline mode: use markdown + cache only, no API calls")
    ap.add_argument("--rebuild-cache", action="store_true",
                    help="ignore existing cache and refetch everything")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    readme_repos = parse_md(args.readme, "language")
    topics_repos = parse_md(args.topics, "topic")
    merged = merge(readme_repos, topics_repos)
    print(f"Parsed {len(merged)} unique repos "
          f"({len(readme_repos)} in README, {len(topics_repos)} in topics)")

    cache = {}
    if not args.rebuild_cache and os.path.exists(args.cache):
        with open(args.cache, encoding="utf-8") as f:
            cache = json.load(f)

    starred_map = {}
    if not args.no_fetch:
        if token:
            print(f"Fetching starred dates for user '{args.username}'...")
            starred_map = fetch_starred_map(args.username, token)
            print(f"Got starred_at for {len(starred_map)} repos")
        else:
            print("No GITHUB_TOKEN: skipping starred_at fetch "
                  "(sort by recently-starred will fall back to repo update date).",
                  file=sys.stderr)
    else:
        for k, v in cache.items():
            if isinstance(v, dict) and v.get("starred_at"):
                starred_map[k] = v["starred_at"]

    records = build_records(merged, cache, starred_map, token, args.no_fetch)

    os.makedirs(os.path.dirname(args.cache) or ".", exist_ok=True)
    with open(args.cache, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "stars.json"), "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=1)
    with open(os.path.join(args.out, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_html(records))
    print(f"Wrote {len(records)} records -> {args.out}/index.html + {args.out}/stars.json")


if __name__ == "__main__":
    main()
