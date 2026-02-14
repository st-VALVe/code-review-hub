#!/usr/bin/env python3
"""AI Code Review using Google Gemini API with context caching support."""

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

MAX_FILE_SIZE = 50_000      # 50 KB per file
MAX_TOTAL_SIZE = 900_000    # ~900 KB total (fits well within 1M token window)

# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------

def collect_files(project_dir):
    """Recursively collect source files, respecting size limits."""
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

## \U0001f916 Еженедельный AI Code Review

### Overall Health Score: X/10

### \U0001f4d0 SOLID: X/10
| Принцип | Оценка | Комментарий |
|---------|--------|-------------|
| SRP | \u2026 | \u2026 |
| OCP | \u2026 | \u2026 |
| LSP | \u2026 | \u2026 |
| ISP | \u2026 | \u2026 |
| DIP | \u2026 | \u2026 |

### \U0001f512 Безопасность: N проблем
(список с \U0001f6a8 Critical / \u274c High / \u26a0\ufe0f Medium / \u2139\ufe0f Low)

### \u267b\ufe0f Рефакторинг: N возможностей
(список с приоритетом и рекомендуемым подходом)

### \U0001f9ea Тестирование: N пробелов
(список)

### \U0001f3d7\ufe0f Архитектура
(ключевые наблюдения)

### \U0001f4cb Приоритетный план действий
1. \u2026
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

## \U0001f50d AI Review PR

### Резюме
(1-2 предложения)

### Проблемы: N
(список с \U0001f6a8/\u274c/\u26a0\ufe0f/\u2139\ufe0f)

### Предложения
(конкретные улучшения)

### Вердикт: \u2705 Approve / \u26a0\ufe0f Needs Changes / \U0001f6a8 Critical Issues
"""

# ---------------------------------------------------------------------------
# Gemini API — SDK path (with context caching)
# ---------------------------------------------------------------------------

def review_sdk(code_context, mode, api_key, model):
    """Use the google-genai SDK (supports explicit context caching)."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    system = SYSTEM_FULL if mode == 'full' else SYSTEM_PR
    gen_cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=0.2,
        max_output_tokens=8192,
    )

    # --- Explicit context caching for large full reviews ---
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
            print(f"\u2713 Context cache created: {cache.name}", file=sys.stderr)

            resp = client.models.generate_content(
                model=model,
                contents="Выполни полный еженедельный code review по инструкциям из системного промпта.",
                config=types.GenerateContentConfig(
                    cached_content=cache.name,
                    temperature=0.2,
                    max_output_tokens=8192,
                ),
            )
            # Cleanup
            try:
                client.caches.delete(cache.name)
            except Exception:
                pass
            return resp.text
        except Exception as exc:
            print(f"\u26a0 Cache fallback: {exc}", file=sys.stderr)

    # --- Direct request (PR / small full / cache fallback) ---
    resp = client.models.generate_content(
        model=model,
        contents=[types.Content(
            role="user",
            parts=[types.Part(text=code_context + "\n\n---\nВыполни code review.")],
        )],
        config=gen_cfg,
    )
    return resp.text

# ---------------------------------------------------------------------------
# Gemini API — REST fallback
# ---------------------------------------------------------------------------

def review_rest(code_context, mode, api_key, model):
    """Pure-stdlib REST fallback when SDK is unavailable."""
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
        return f"\u274c AI Review failed: HTTP {exc.code}"
    except Exception as exc:
        print(f"Gemini REST error: {exc}", file=sys.stderr)
        return f"\u274c AI Review failed: {exc}"

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="AI Code Review with Gemini")
    ap.add_argument("--mode", choices=["full", "pr"], default="full")
    ap.add_argument("--project-dir", default=".")
    ap.add_argument("--diff-file", default=None)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    api_key = args.api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("Error: GEMINI_API_KEY not set")

    # Build context
    if args.mode == "pr" and args.diff_file:
        with open(args.diff_file, "r") as f:
            context = f.read()
        if not context.strip():
            print("Empty diff, nothing to review.", file=sys.stderr)
            report = "\u2705 Нет изменений для ревью."
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

    # Call Gemini
    try:
        from google import genai  # noqa: F401
        report = review_sdk(context, args.mode, api_key, args.model)
    except ImportError:
        print("google-genai not installed, using REST API", file=sys.stderr)
        report = review_rest(context, args.mode, api_key, args.model)

    # Output
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    main()
