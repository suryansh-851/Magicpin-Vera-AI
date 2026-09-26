"""Build a clean submission ZIP from an explicit allow-list — never a blind copy of the working directory.

Excludes: .env, *.llm_cache.json, *.judge_cache.json, __pycache__/, *.pyc, dataset/expanded/ (regenerable),
OS/editor junk. Runs a secret scan (LLM_API_KEY-shaped tokens, common key prefixes) before writing anything
and refuses to package if it finds one outside .env.example / this script's own patterns.

Usage: python scripts/package_submission.py [out.zip]
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# explicit allow-list: files and (non-recursive-by-default) directories to include
ALLOW_FILES = [
    "bot.py", "conversation_handlers.py", "judge_simulator.py", "requirements.txt",
    "Procfile", "render.yaml", "README.md", "submission.jsonl", ".env.example",
    "challenge-brief.md", "challenge-testing-brief.md",
]
# dataset/ base seed files (not the generated dataset/expanded/, which is excluded as regenerable)
ALLOW_DATASET_FILES = ["dataset/customers_seed.json", "dataset/merchants_seed.json",
                       "dataset/triggers_seed.json", "dataset/generate_dataset.py"]

EXCLUDE_PATTERNS = (r"__pycache__", r"\.pyc$", r"\.pyo$", r"^\.env$", r"llm_cache\.json$",
                    r"judge_cache\.json$", r"^\.DS_Store$", r"Thumbs\.db$", r"dataset/expanded/")

# secret-shaped tokens: provider API key prefixes, or a KEY=<40+ char token> assignment outside .env.example
SECRET_PATTERNS = [
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),          # Google/Gemini
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),              # OpenAI/DeepSeek-style
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}"),             # Groq
    re.compile(r"\bsk-or-[A-Za-z0-9-]{10,}"),          # OpenRouter
    re.compile(r"(?im)^\s*LLM_API_KEY\s*=\s*\S{8,}"),  # a filled-in key in a tracked file
]


def _excluded(rel: str) -> bool:
    return any(re.search(p, rel) for p in EXCLUDE_PATTERNS)


def collect_files() -> list[Path]:
    out: list[Path] = []
    for name in ALLOW_FILES:
        p = ROOT / name
        if p.exists():
            out.append(p)
    for name in ALLOW_DATASET_FILES:
        p = ROOT / name
        if p.exists():
            out.append(p)
    for d in ("vera", "scripts", "dataset/categories"):
        base = ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file() and not _excluded(str(p.relative_to(ROOT))):
                out.append(p)
    return sorted(set(out))


def secret_scan(files: list[Path]) -> list[str]:
    hits = []
    for f in files:
        if f.name == ".env.example":
            continue  # names only, no real values — allowed
        if f.suffix not in (".py", ".md", ".json", ".yaml", ".yml", ".txt", ""):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in SECRET_PATTERNS:
            if pat.search(text):
                hits.append(f"{f.relative_to(ROOT)}: matches {pat.pattern[:40]}...")
    return hits


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "submission_package.zip"
    files = collect_files()
    if not files:
        sys.exit("No files matched the allow-list — check ALLOW_FILES/ALLOW_DIRS.")

    hits = secret_scan(files)
    if hits:
        print("SECRET SCAN FAILED — refusing to package. Found possible secrets in:", file=sys.stderr)
        for h in hits:
            print(f"  {h}", file=sys.stderr)
        sys.exit(1)
    print(f"Secret scan clean ({len(files)} files checked).")

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.relative_to(ROOT))

    print(f"Wrote {out} ({len(files)} files, {out.stat().st_size / 1024:.0f} KB)")
    print("Contents:")
    for f in files:
        print(f"  {f.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
