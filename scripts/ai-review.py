#!/usr/bin/env python3
"""AI Code Review — supports Gemini API and Claude via Vertex AI."""

import os
import sys
import json
import argparse
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CODE_EXTENSIONS = {
    '.js', '.ts', '.tsx', '.jsx', '.py', '.json', '.yml', '.yaml',
    '.css', '.scss', '.html', '.sql', '.sh',
}

SKIP_DIRS = {
    'node_modules', '.git', 'dist', 'build', '.next', 'coverage',
    '__pycache__', '.cache', '.github', 'vendor', '.vscode',
}

MAX_FILE_SIZE = 50_000
MAX_TOTAL_SIZE = 900_000

# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------

def collect_files(project_dir):
    files = {}
    total = 0
    for root, dirs, names in os.walk(project_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            if Path(name).suffix.lower() not in CODE_EXTENSIONS:
                continue
            fp = os.path.join(root, name)
            try:
                sz = os.path.getsize(fp)
            except OSError:
                continue
            if sz > MAX_FILE_SIZE or total + sz > MAX_TOTAL_SIZE:
                continue
            try:
                with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                    files[os.path.relpath(fp, project_dir)] = f.read()
                total += sz
            except OSError:
                continue
    return files


def build_code_context(files):
    parts = []
    for path, content in files.items():
        parts.append(f"### File: {path}\n```\n{content}\n```")
    return "\n\n".join(parts)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYSTEM_FULL = """\
You are an expert senior software architect performing a comprehensive weekly code review.
Respond in **Russian**.

Analyse the provided codebase and produce a structured report covering:

1. **SOLID Compliance** (score 1-10 per principle + overall)
2. **Security Issues** — hardcoded secrets, injection risks, XSS, insecure data handling, missing input validation
3. **Refactoring Opportunities** — duplication, god objects, long methods, magic values, dead code, complex conditionals
4. **Test Coverage Gaps** — missing test files, untested critical paths, edge-cases, error-handling
5. **Architecture & Best Practices** — design patterns, error handling, logging, configuration management

Use **exactly** this output template:

## 🤖 Еженедельный AI Code Review

### Overall Health Score: X/10

### 📐 SOLID: X/10
| Принцип | Оценка | Комментарий |
|---------|--------|-------------|
| SRP | … | … |
| OCP | … | … |
| LSP | … | … |
| ISP | … | … |
| DIP | … | … |

### 🔒 Безопасность: N проблем
(список с 🚨 Critical / ❌ High / ⚠️ Medium / ℹ️ Low)

### ♻️ Рефакторинг: N возможностей
(список с приоритетом и рекомендуемым подходом)

### 🧪 Тестирование: N пробелов
(список)

### 🏗️ Архитектура
(ключевые наблюдения)

### 📋 Приоритетный план действий
1. …
"""

SYSTEM_PR = """\
You are an expert code reviewer analysing a pull request diff.
Respond in **Russian**. Be concise and actionable.

Focus on:
1. **Баги / логические ошибки**
2. **Безопасность** изменённого кода
3. **Нарушения SOLID**
4. **Качество кода** — читаемость, именование, сложность
5. **Конкретные предложения** по улучшению

Use **exactly** this output template:

## 🔍 AI Review PR

### Резюме
(1-2 предложения)

### Проблемы: N
(список с 🚨/❌/⚠️/ℹ️)

### Предложения
(конкретные улучшения)

### Вердикт: ✅ Approve / ⚠️ Needs Changes / 🚨 Critical Issues
"""

# ---------------------------------------------------------------------------
# Gemini provider (Google AI Studio — uses GEMINI_API_KEY)
# ---------------------------------------------------------------------------

def review_gemini(code_context, mode, api_key, model):
    """Google Gemini via google-genai SDK."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    system = SYSTEM_FULL if mode == 'full' else SYSTEM_PR
    gen_cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=0.2,
        max_output_tokens=8192,
    )

    # Context caching for large full reviews
    if mode == 'full' and len(code_context) > 32_000:
        try:
            cache = client.caches.create(
                model=model,
                config=types.CreateCachedContentConfig(
                    system_instruction=system,
                    contents=[types.Content(
                        role="user",
                        parts=[types.Part(text=(
                            "Вот полный исходный код проекта для ревью:\n\n"
                            + code_context
                        ))],
                    )],
                    ttl="3600s",
                    display_name="weekly-review-cache",
                ),
            )
            print(f"✓ Gemini context cache created: {cache.name}", file=sys.stderr)
            resp = client.models.generate_content(
                model=model,
                contents="Выполни полный еженедельный code review по инструкциям из системного промпта.",
                config=types.GenerateContentConfig(
                    cached_content=cache.name,
                    temperature=0.2,
                    max_output_tokens=8192,
                ),
            )
            try:
                client.caches.delete(cache.name)
            except Exception:
                pass
            return resp.text
        except Exception as exc:
            print(f"⚠ Gemini cache fallback: {exc}", file=sys.stderr)

    resp = client.models.generate_content(
        model=model,
        contents=[types.Content(
            role="user",
            parts=[types.Part(text=code_context + "\n\n---\nВыполни code review.")],
        )],
        config=gen_cfg,
    )
    return resp.text


def review_gemini_rest(code_context, mode, api_key, model):
    """Gemini REST fallback."""
    import urllib.request, urllib.error

    system = SYSTEM_FULL if mode == 'full' else SYSTEM_PR
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model}:generateContent?key={api_key}"
    )
    payload = json.dumps({
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": code_context + "\n\n---\nВыполни code review."}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192},
    }).encode()

    req = urllib.request.Request(url, payload, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            body = json.loads(r.read())
            return body["candidates"][0]["content"]["parts"][0]["text"]
    except urllib.error.HTTPError as exc:
        err = exc.read().decode()
        print(f"Gemini REST error {exc.code}: {err}", file=sys.stderr)
        return f"❌ Gemini review failed: HTTP {exc.code}"
    except Exception as exc:
        return f"❌ Gemini review failed: {exc}"

# ---------------------------------------------------------------------------
# Claude provider (Vertex AI — uses GCP service account or ADC)
# ---------------------------------------------------------------------------

def review_claude_vertex(code_context, mode, model, project_id, region):
    """Claude via Google Vertex AI using anthropic[vertex] SDK."""
    from anthropic import AnthropicVertex

    client = AnthropicVertex(project_id=project_id, region=region)
    system = SYSTEM_FULL if mode == 'full' else SYSTEM_PR

    if len(code_context) > 600_000:
        code_context = code_context[:600_000] + "\n\n[... truncated ...]"

    print(f"→ Calling Claude {model} via Vertex AI (project={project_id}, region={region})", file=sys.stderr)

    message = client.messages.create(
        model=model,
        max_tokens=8192,
        temperature=0.2,
        system=system,
        messages=[
            {"role": "user", "content": code_context + "\n\n---\nВыполни code review."}
        ],
    )

    return "".join(b.text for b in message.content if hasattr(b, 'text'))


def review_claude_rest(code_context, mode, model, project_id, region):
    """Claude via Vertex AI REST (when SDK unavailable)."""
    import urllib.request, urllib.error, subprocess

    system = SYSTEM_FULL if mode == 'full' else SYSTEM_PR

    if len(code_context) > 600_000:
        code_context = code_context[:600_000] + "\n\n[... truncated ...]"

    # Get access token from gcloud
    try:
        token = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"],
            text=True
        ).strip()
    except Exception as exc:
        return f"❌ Failed to get GCP access token: {exc}"

    url = (
        f"https://{region}-aiplatform.googleapis.com/v1/"
        f"projects/{project_id}/locations/{region}/"
        f"publishers/anthropic/models/{model}:rawPredict"
    )

    payload = json.dumps({
        "anthropic_version": "vertex-2023-10-16",
        "max_tokens": 8192,
        "temperature": 0.2,
        "system": system,
        "messages": [
            {"role": "user", "content": code_context + "\n\n---\nВыполни code review."}
        ],
    }).encode()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    req = urllib.request.Request(url, payload, headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            body = json.loads(r.read())
            return "".join(b["text"] for b in body["content"] if b["type"] == "text")
    except urllib.error.HTTPError as exc:
        err = exc.read().decode()
        print(f"Claude Vertex REST error {exc.code}: {err}", file=sys.stderr)
        return f"❌ Claude review failed: HTTP {exc.code}"
    except Exception as exc:
        return f"❌ Claude review failed: {exc}"

# ---------------------------------------------------------------------------
# Provider dispatcher
# ---------------------------------------------------------------------------

def run_review(code_context, mode, provider, model, **kwargs):
    """Route to the right provider."""

    if provider == "gemini":
        api_key = kwargs.get("api_key") or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            sys.exit("Error: GEMINI_API_KEY not set")
        model = model or "gemini-2.5-flash"
        print(f"→ Provider: Gemini, Model: {model}", file=sys.stderr)
        try:
            from google import genai  # noqa: F401
            return review_gemini(code_context, mode, api_key, model)
        except ImportError:
            print("⚠ google-genai not installed, using REST", file=sys.stderr)
            return review_gemini_rest(code_context, mode, api_key, model)

    elif provider == "claude":
        project_id = kwargs.get("gcp_project") or os.environ.get("GCP_PROJECT_ID")
        region = kwargs.get("gcp_region") or os.environ.get("GCP_REGION", "us-east5")
        model = model or "claude-opus-4-6"
        if not project_id:
            sys.exit("Error: GCP_PROJECT_ID not set (required for Claude via Vertex AI)")
        print(f"→ Provider: Claude (Vertex AI), Model: {model}", file=sys.stderr)
        try:
            from anthropic import AnthropicVertex  # noqa: F401
            return review_claude_vertex(code_context, mode, model, project_id, region)
        except ImportError:
            print("⚠ anthropic[vertex] not installed, using REST", file=sys.stderr)
            return review_claude_rest(code_context, mode, model, project_id, region)

    else:
        sys.exit(f"Unknown provider: {provider}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="AI Code Review — Gemini / Claude")
    ap.add_argument("--mode", choices=["full", "pr"], default="full")
    ap.add_argument("--provider", choices=["gemini", "claude"], default="gemini")
    ap.add_argument("--project-dir", default=".")
    ap.add_argument("--diff-file", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--output", default=None)
    # Gemini-specific
    ap.add_argument("--api-key", default=None)
    # Claude/Vertex-specific
    ap.add_argument("--gcp-project", default=None)
    ap.add_argument("--gcp-region", default=None)
    args = ap.parse_args()

    # Build context
    if args.mode == "pr" and args.diff_file:
        with open(args.diff_file, "r") as f:
            context = f.read()
        if not context.strip():
            report = "✅ Нет изменений для ревью."
            if args.output:
                Path(args.output).write_text(report)
            else:
                print(report)
            return
    else:
        files = collect_files(args.project_dir)
        if not files:
            sys.exit("No source files found.")
        context = build_code_context(files)
        print(f"Collected {len(files)} files for review", file=sys.stderr)

    # Run review
    report = run_review(
        context, args.mode, args.provider, args.model,
        api_key=args.api_key,
        gcp_project=args.gcp_project,
        gcp_region=args.gcp_region,
    )

    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    main()
