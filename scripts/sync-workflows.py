#!/usr/bin/env python3
"""
Sync quality & review workflows to all repos managed by code-review-hub.

Auto-discovers repos, detects project type, provisions appropriate workflows.
Only updates workflows tagged with the managed-by marker (safe for custom ones).
Handles repo renames/deletions gracefully — just works off live API data.
"""

import os
import sys
import json
import base64
import hashlib
import subprocess
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

MANAGED_MARKER = "# managed-by: code-review-hub"
MANAGED_VERSION = "v2"

# ── GitHub API via gh CLI ──────────────────────────────────


def gh_api(endpoint, method="GET", fields=None, silent=False):
    """Call GitHub API via gh CLI. Returns parsed JSON or None."""
    cmd = ["gh", "api", endpoint]
    if method != "GET":
        cmd.extend(["-X", method])
    if fields:
        for key, value in fields.items():
            cmd.extend(["-f", f"{key}={value}"])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            if not silent and "404" not in r.stderr:
                print(f"  ⚠ API error: {r.stderr.strip()[:200]}", file=sys.stderr)
            return None
        return json.loads(r.stdout) if r.stdout.strip() else {}
    except Exception as exc:
        if not silent:
            print(f"  ⚠ API call failed: {exc}", file=sys.stderr)
        return None


def gh_api_pages(endpoint):
    """Paginated GET — collects all pages."""
    items = []
    page = 1
    while True:
        sep = "&" if "?" in endpoint else "?"
        data = gh_api(f"{endpoint}{sep}per_page=100&page={page}")
        if not data:
            break
        items.extend(data)
        if len(data) < 100:
            break
        page += 1
    return items


# ── Repo discovery ─────────────────────────────────────────


def discover_repos(owner, config):
    """List repos applying config filters. Uses /user/repos for private access."""
    # /user/repos (authenticated) returns private repos too
    all_repos = gh_api_pages(f"/user/repos?affiliation=owner")
    if not all_repos:
        # Fallback to public API
        print("  ⚠ /user/repos returned nothing, falling back to /users/{owner}/repos", file=sys.stderr)
        all_repos = gh_api_pages(f"/users/{owner}/repos?type=owner")
    names = []
    for r in all_repos:
        # Filter by owner (affiliation=owner may include org repos)
        if r.get("owner", {}).get("login", "").lower() != owner.lower():
            continue
        if r.get("fork") and config.get("skip_forks", True):
            continue
        if r.get("archived") and config.get("skip_archived", True):
            continue
        names.append(r["name"])

    exclude = set(config.get("exclude_repos", []))
    include_only = config.get("include_only", [])
    if include_only:
        return [n for n in names if n in include_only]
    return [n for n in names if n not in exclude]


def detect_project_type(owner, repo):
    """Detect project type by checking for marker files."""
    types = set()

    # Node.js
    r = gh_api(f"/repos/{owner}/{repo}/contents/package.json", silent=True)
    if r and isinstance(r, dict) and r.get("sha"):
        types.add("node")

    # Python
    for marker in ("requirements.txt", "pyproject.toml", "setup.py", "Pipfile"):
        r = gh_api(f"/repos/{owner}/{repo}/contents/{marker}", silent=True)
        if r and isinstance(r, dict) and r.get("sha"):
            types.add("python")
            break

    return types


# ── File operations ────────────────────────────────────────


def get_remote_file(owner, repo, path):
    """Returns (sha, content, is_managed) or (None, None, False)."""
    r = gh_api(f"/repos/{owner}/{repo}/contents/{path}", silent=True)
    if r and isinstance(r, dict) and r.get("sha"):
        raw = base64.b64decode(r.get("content", "")).decode(
            "utf-8", errors="ignore"
        )
        return r["sha"], raw, MANAGED_MARKER in raw
    return None, None, False


def put_remote_file(owner, repo, path, content, message, sha=None):
    """Create or update a file on GitHub."""
    encoded = base64.b64encode(content.encode()).decode()
    fields = {"message": message, "content": encoded}
    if sha:
        fields["sha"] = sha
    return (
        gh_api(
            f"/repos/{owner}/{repo}/contents/{path}",
            method="PUT",
            fields=fields,
        )
        is not None
    )


def content_fingerprint(text):
    """Hash content excluding managed-by lines (for change detection)."""
    lines = [l for l in text.splitlines() if not l.startswith(MANAGED_MARKER)]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


# ── Template loading ───────────────────────────────────────


def load_template(templates_dir, name, replacements=None):
    """Load template and apply placeholder replacements."""
    path = Path(templates_dir) / name
    if not path.exists():
        print(f"  ⚠ Template not found: {path}", file=sys.stderr)
        return None
    content = path.read_text()
    for key, value in (replacements or {}).items():
        content = content.replace("{{" + key + "}}", value)
    return content


# ── Sync logic ─────────────────────────────────────────────

EMOJI = {
    "created": "🆕",
    "updated": "🔄",
    "up-to-date": "✅",
    "skipped-custom": "⏭️",
    "skipped": "⏭️",
    "failed": "❌",
}


def sync_one(owner, repo, filename, content, force=False, full_path=None):
    """Sync a single file. Returns status string."""
    path = full_path or f".github/workflows/{filename}"
    sha, existing, is_managed = get_remote_file(owner, repo, path)

    if sha and not is_managed and not force:
        return "skipped-custom"

    if sha and (is_managed or force):
        if content_fingerprint(existing) == content_fingerprint(content):
            return "up-to-date"
        ok = put_remote_file(
            owner, repo, path, content,
            f"🤖 Update {filename} (code-review-hub)", sha,
        )
        return "updated" if ok else "failed"

    # New file
    ok = put_remote_file(
        owner, repo, path, content,
        f"🤖 Add {filename} (code-review-hub)",
    )
    return "created" if ok else "failed"


# ── Main ───────────────────────────────────────────────────


def main():
    hub_dir = os.environ.get("HUB_DIR", ".")
    config_path = os.path.join(hub_dir, "config.yml")
    templates_dir = os.path.join(hub_dir, "templates")
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"

    with open(config_path) as f:
        config = yaml.safe_load(f)

    owner = config["github_owner"]
    qc = config.get("quality_workflows", {})
    force = (
        qc.get("force_manage", False)
        or os.environ.get("FORCE_MANAGE", "false").lower() == "true"
    )

    if not qc.get("enabled", True):
        print("ℹ️  Quality workflow sync disabled in config.")
        return

    # Discover
    repos = discover_repos(owner, config)
    print(f"📋 Found {len(repos)} repos: {', '.join(repos)}\n")

    results = {}

    for repo in repos:
        print(f"── {repo} ──")
        repo_results = {}
        types = detect_project_type(owner, repo)
        type_str = ", ".join(types) if types else "generic"
        print(f"   Types: {type_str}")

        # 1) CodeQL
        if qc.get("codeql", True):
            langs = []
            if "node" in types:
                langs.append("'javascript-typescript'")
            if "python" in types:
                langs.append("'python'")
            if not langs:
                # Default to JS for unknown projects
                langs.append("'javascript-typescript'")

            tpl = load_template(
                templates_dir,
                "codeql-analysis.yml",
                {"CODEQL_LANGUAGES": ", ".join(langs)},
            )
            if tpl:
                if dry_run:
                    s = "dry-run"
                else:
                    s = sync_one(owner, repo, "codeql-analysis.yml", tpl, force)
                repo_results["codeql"] = s
                print(f"   {EMOJI.get(s, '?')} codeql: {s}")

        # 2) Code Quality
        if qc.get("code_quality", True):
            if "node" in types:
                tpl = load_template(templates_dir, "code-quality-node.yml")
                label = "Node"
            elif "python" in types:
                tpl = load_template(templates_dir, "code-quality-python.yml")
                label = "Python"
            else:
                tpl = None
                label = None
                repo_results["code-quality"] = "skipped"
                print("   ⏭️ code-quality: skipped (unknown type)")

            if tpl:
                if dry_run:
                    s = "dry-run"
                else:
                    s = sync_one(owner, repo, "code-quality.yml", tpl, force)
                repo_results["code-quality"] = s
                print(f"   {EMOJI.get(s, '?')} code-quality ({label}): {s}")

        # 4) Webhook manifest (.github/code-review-hub.yml)
        tpl = load_template(
            templates_dir, "code-review-hub-config.yml", {"OWNER": owner}
        )
        if tpl:
            if dry_run:
                s = "dry-run"
            else:
                s = sync_one(owner, repo, "code-review-hub.yml", tpl, force,
                             full_path=".github/code-review-hub.yml")
            repo_results["manifest"] = s
            print(f"   {EMOJI.get(s, '?')} manifest: {s}")

        results[repo] = repo_results
        print()

    # ── Summary ──
    counts = {
        "created": 0, "updated": 0, "up-to-date": 0,
        "skipped": 0, "failed": 0,
    }
    for actions in results.values():
        for s in actions.values():
            if s.startswith("skip"):
                counts["skipped"] += 1
            elif s in counts:
                counts[s] += 1

    print("=" * 60)
    print("📊 SYNC SUMMARY")
    print("=" * 60)
    for label, n in counts.items():
        print(f"  {EMOJI.get(label, '?')} {label}: {n}")

    # GitHub Actions step summary
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write("## 🔄 Quality Workflows Sync\n\n")
            f.write("| Repo | PR Review | CodeQL | Quality |\n")
            f.write("|------|-----------|--------|---------|\n")
            for repo, actions in results.items():
                cq = EMOJI.get(actions.get("codeql", "—"), "—")
                qu = EMOJI.get(actions.get("code-quality", "—"), "—")
                f.write(f"| {repo} | {pr} | {cq} | {qu} |\n")
            f.write(f"\n**Totals:** {counts}\n")

    # Webhook
    webhook_url = os.environ.get("WEBHOOK_URL")
    if webhook_url and (counts["created"] > 0 or counts["updated"] > 0):
        import urllib.request

        payload = json.dumps({
            "event": "workflows_synced",
            "owner": owner,
            "created": counts["created"],
            "updated": counts["updated"],
            "repos": list(results.keys()),
        }).encode()
        req = urllib.request.Request(
            webhook_url, payload, {"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req, timeout=30)
        except Exception:
            pass


if __name__ == "__main__":
    main()
