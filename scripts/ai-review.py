#!/usr/bin/env python3
"""AI Code Review — supports Gemini and Claude APIs with switchable provider."""

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
# Gemini API
# ---------------------------------------------------------------------------

def review_gemini_sdk(code_context, mode, api_key, model):
    """Google Gemini via google-genai SDK (with context caching)."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    system = SYSTEM_FULL if mode == 'full' else SYSTEM_PR
    gen_cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=0.2,
        max_output_tokens=8192,
    )

    # Explicit context caching for large full reviews
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
    """Gemini REST fallback when SDK is unavailable."""
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
        with urllib.request.urlopen(req, timeout=180) as r:
            body = json.loads(r.read())
            return body["candidates"][0]["content"]["parts"][0]["text"]
    except urllib.error.HTTPError as exc:
        err = exc.read().decode()
        print(f"Gemini REST error {exc.code}: {err}", file=sys.stderr)
        return f"❌ Gemini review failed: HTTP {exc.code}"
    except Exception as exc:
        print(f"Gemini REST error: {exc}", file=sys.stderr)
        return f"❌ Gemini review failed: {exc}"

# ---------------------------------------------------------------------------
# Claude API
# ---------------------------------------------------------------------------

def review_claude_sdk(code_context, mode, api_key, model):
    """Anthropic Claude via anthropic SDK."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    system = SYSTEM_FULL if mode == 'full' else SYSTEM_PR

    # Truncate if too large for Claude (200K context window)
    if len(code_context) > 600_000:
        code_context = code_context[:600_000] + "\n\n[... truncated due to size ...]"

    print(f"→ Calling Claude {model} ({len(code_context)} chars)", file=sys.stderr)

    message = client.messages.create(
        model=model,
        max_tokens=8192,
        temperature=0.2,
        system=system,
        messages=[
            {"role": "user", "content": code_context + "\n\n---\nВыполни code review."}
        ],
    )

    # Extract text from response
    result = ""
    for block in message.content:
        if hasattr(block, 'text'):
            result += block.text
    return result


def review_claude_rest(code_context, mode, api_key, model):
    """Claude REST fallback when SDK is unavailable."""
    import urllib.request, urllib.error

    system = SYSTEM_FULL if mode == 'full' else SYSTEM_PR

    if len(code_context) > 600_000:
        code_context = code_context[:600_000] + "\n\n[... truncated due to size ...]"

    url = "https://api.anthropic.com/v1/messages"
    payload = json.dumps({
        "model": model,
        "max_tokens": 8192,
        "temperature": 0.2,
        "system": system,
        "messages": [
            {"role": "user", "content": code_context + "\n\n---\nВыполни code review."}
        ],
    }).encode()

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    req = urllib.request.Request(url, payload, headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            body = json.loads(r.read())
            return "".join(b["text"] for b in body["content"] if b["type"] == "text")
    except urllib.error.HTTPError as exc:
        err = exc.read().decode()
        print(f"Claude REST error {exc.code}: {err}", file=sys.stderr)
        return f"❌ Claude review failed: HTTP {exc.code}"
    except Exception as exc:
        print(f"Claude REST error: {exc}", file=sys.stderr)
        return f"❌ Claude review failed: {exc}"

# ---------------------------------------------------------------------------
# Provider dispatcher
# ---------------------------------------------------------------------------

PROVIDERS = {
    "gemini": {
        "sdk_fn": review_gemini_sdk,
        "rest_fn": review_gemini_rest,
        "sdk_import": "google.genai",
        "sdk_package": "google-genai",
        "env_key": "GEMINI_API_KEY",
        "default_model": "gemini-2.5-flash",
    },
    "claude": {
        "sdk_fn": review_claude_sdk,
        "rest_fn": review_claude_rest,
        "sdk_import": "anthropic",
        "sdk_package": "anthropic",
        "env_key": "ANTHROPIC_API_KEY",
        "default_model": "claude-opus-4-20250514",
    },
}


def run_review(code_context, mode, provider, api_key, model):
    """Run review using the specified provider."""
    p = PROVIDERS[provider]
    if not model:
        model = p["default_model"]

    # Try SDK first, fall back to REST
    try:
        __import__(p["sdk_import"])
        print(f"→ Using {provider} SDK ({model})", file=sys.stderr)
        return p["sdk_fn"](code_context, mode, api_key, model)
    except ImportError:
        print(f"⚠ {p['sdk_package']} not installed, using REST API", file=sys.stderr)
        return p["rest_fn"](code_context, mode, api_key, model)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="AI Code Review — Gemini / Claude")
    ap.add_argument("--mode", choices=["full", "pr"], default="full")
    ap.add_argument("--provider", choices=["gemini", "claude"], default="gemini")
    ap.add_argument("--project-dir", default=".")
    ap.add_argument("--diff-file", default=None)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    provider = args.provider
    p = PROVIDERS[provider]

    api_key = args.api_key or os.environ.get(p["env_key"])
    if not api_key:
        sys.exit(f"Error: {p['env_key']} not set")

    # Build context
    if args.mode == "pr" and args.diff_file:
        with open(args.diff_file, "r") as f:
            context = f.read()
        if not context.strip():
            print("Empty diff, nothing to review.", file=sys.stderr)
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
    report = run_review(context, args.mode, provider, api_key, args.model)

    # Output
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    main()
