"""Check local Markdown links, images and heading anchors without network access."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit


def prose(text: str) -> str:
    """Ignore fenced code, preserving indented list links and continuations."""
    result = []
    fence = None
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if match:
            marker = match[1]
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            result.append("")
        elif fence is None:
            result.append(line)
        else:
            result.append("")
    return "\n".join(result)


def anchors(text: str) -> set[str]:
    text = prose(text)
    found = set(re.findall(r'<(?:a|h[1-6])\s+[^>]*(?:id|name)=["\']([^"\']+)', text))
    counts: dict[str, int] = {}
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        title = match[1] if match else None
        if (
            title is None
            and index + 1 < len(lines)
            and re.fullmatch(r"\s*(?:=+|-+)\s*", lines[index + 1])
        ):
            title = line.strip() or None
        if title is None:
            continue
        title = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", title)
        title = re.sub(r"<[^>]+>", "", title).lower()
        slug = re.sub(r"[^\w\- ]", "", title).replace(" ", "-")
        duplicate = counts.get(slug, 0)
        counts[slug] = duplicate + 1
        found.add(f"{slug}-{duplicate}" if duplicate else slug)
    return found


def links(text: str) -> list[str]:
    text = prose(text)
    text = re.sub(r"(`+).*?\1", "", text)
    references = {
        label.strip().lower(): target
        for label, target in re.findall(r"^\s{0,3}\[([^\]]+)\]:\s*<?([^\s>]+)>?", text, re.M)
    }
    result = re.findall(r"!?\[[^\]\n]*\]\(\s*<?([^\s>]+?)>?(?:\s+[\"'][^\n]*?[\"'])?\s*\)", text)
    for label, reference in re.findall(r"!?\[([^\]\n]+)\]\[([^\]\n]*)\]", text):
        key = (reference or label).strip().lower()
        if key in references:
            result.append(references[key])
        else:
            result.append(f"missing-reference:{key}")
    for label in re.findall(r"(?<!!)\[([^\]\n]+)\](?![\[( :])", text):
        if label.strip().lower() in references:
            result.append(references[label.strip().lower()])
    return result


def check(root: Path, paths: list[Path]) -> list[str]:
    errors = []
    cache: dict[Path, set[str]] = {}
    for source in paths:
        for target in links(source.read_text()):
            url = urlsplit(target)
            if url.scheme == "missing-reference":
                errors.append(f"{source.relative_to(root)}: undefined reference {url.path}")
                continue
            if url.scheme or url.netloc:
                continue
            destination = (
                (source.parent / unquote(url.path)).resolve() if url.path else source.resolve()
            )
            if not destination.is_relative_to(root.resolve()):
                errors.append(f"{source.relative_to(root)}: link escapes repository: {target}")
            elif not destination.exists():
                errors.append(f"{source.relative_to(root)}: missing target: {target}")
            elif url.fragment and destination.suffix.lower() == ".md":
                if destination not in cache:
                    cache[destination] = anchors(destination.read_text())
                if unquote(url.fragment) not in cache[destination]:
                    errors.append(f"{source.relative_to(root)}: missing anchor: {target}")
    return errors


def inventory(root: Path) -> list[Path]:
    if not (root / ".git").exists():
        # Reviewed source archives have no Git metadata. Never scan a whole
        # environment/runtime directory to compensate for that missing metadata.
        roots = (
            "docs",
            "desktop",
            "deploy",
            "examples",
            "skills",
            "plugins",
            "release",
            ".agents",
            ".github",
        )
        excluded = {
            "node_modules",
            ".venv",
            ".git",
            ".tessera",
            "build",
            "dist",
            "receipts",
            "runtime",
        }
        paths = list(root.glob("*.md"))
        for name in roots:
            paths.extend(
                path
                for path in (root / name).rglob("*.md")
                if not excluded.intersection(path.relative_to(root).parts)
            )
        return sorted(set(paths))
    names = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root
        )
        .decode()
        .split("\0")
    )
    return sorted(
        {root / name for name in names if name.endswith(".md") and (root / name).is_file()}
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    paths = inventory(args.root)
    errors = check(args.root, paths)
    for error in errors:
        print(error)
    print(f"Checked {len(paths)} Markdown files; {len(errors)} local link/anchor errors.")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
