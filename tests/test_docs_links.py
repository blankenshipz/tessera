import importlib.util
import shutil
import tomllib
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "check_docs", Path(__file__).parents[1] / "dev/check_docs.py"
)
docs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(docs)


def test_fences_images_references_and_anchors(tmp_path):
    (tmp_path / "image.png").write_bytes(b"fixture")
    (tmp_path / "other file.md").write_text("# Hello `World`!\n# Hello World\n")
    source = tmp_path / "README.md"
    source.write_text(
        "# Overview\n[local](other%20file.md#hello-world-1)\n![image](image.png)\n"
        '[ref][guide]\n[guide]: <other%20file.md#hello-world> "Guide"\n'
        "```md\n[not a link](missing.md)\n# Not a heading\n```\n"
        "~~~\n[also ignored](missing.md)\n~~~\n"
        "[SDK](https://example.org/sdk#external)\n"
        "`[inline example](missing.md)`\n    [list continuation](image.png)\n"
    )
    assert docs.check(tmp_path, [source]) == []
    assert "not-a-heading" not in docs.anchors(source.read_text())


def test_missing_file_anchor_image_and_reference(tmp_path):
    (tmp_path / "other.md").write_text("# Present\n")
    source = tmp_path / "README.md"
    source.write_text("[x](missing.md)\n[x](other.md#absent)\n![x](missing.png)\n[x][absent]\n")
    errors = docs.check(tmp_path, [source])
    assert len(errors) == 4
    assert any("missing anchor" in error for error in errors)
    assert any("undefined reference" in error for error in errors)


def test_explicit_setext_and_same_file_anchors(tmp_path):
    source = tmp_path / "README.md"
    source.write_text('<a id="custom"></a>\nTitle\n=====\n[x](#title) [y](#custom)\n')
    assert docs.check(tmp_path, [source]) == []


def test_repository_documentation_links():
    root = Path(__file__).parents[1]
    assert docs.check(root, docs.inventory(root)) == []


def test_source_archive_inventory_without_git(tmp_path):
    (tmp_path / "README.md").write_text("# Source archive\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/guide.md").write_text("# Guide\n")
    (tmp_path / "docs/receipts").mkdir()
    (tmp_path / "docs/receipts/private.md").write_text("private fixture\n")
    assert docs.inventory(tmp_path) == [tmp_path / "README.md", tmp_path / "docs/guide.md"]


def test_installed_documentation_layout_and_links(tmp_path):
    root = Path(__file__).parents[1]
    rows = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["setuptools"]["data-files"]
    targets = set()
    for destination, patterns in rows.items():
        for pattern in patterns:
            sources = list(root.glob(pattern))
            assert sources, f"Missing package input: {pattern}"
            for source in sources:
                target = tmp_path / destination / source.name
                assert target not in targets, f"Package basename collision: {target}"
                targets.add(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
    share = tmp_path / "share/tessera"
    assert (share / "README.md").read_bytes() == (root / "README.md").read_bytes()
    assert (share / "docs/README.md").read_bytes() == (root / "docs/README.md").read_bytes()
    assert docs.check(share, sorted(share.rglob("*.md"))) == []
