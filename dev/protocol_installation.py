"""Fail-closed local wheel/import/entrypoint binding for the opt-in protocol gate."""

import base64
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit


def check_paths(python, bin_dir, wheel, expected_sha):
    python, bin_dir, wheel = (Path(p).absolute() for p in (python, bin_dir, wheel))
    if not isinstance(expected_sha, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_sha):
        raise ValueError("An independently supplied wheel SHA256 is required")
    if not wheel.is_file() or hashlib.sha256(wheel.read_bytes()).hexdigest() != expected_sha:
        raise ValueError("Wheel SHA256 mismatch or wheel missing")
    if not python.is_file() or not os.access(python, os.X_OK) or python.parent != bin_dir:
        raise ValueError("Candidate Python and bin directory must identify the same environment")
    prefix = bin_dir.parent
    if not (prefix / "pyvenv.cfg").is_file():
        raise ValueError("An explicit candidate virtual environment is required")
    for name in ("tessera-mcp", "tessera-a2a-peer"):
        entry = bin_dir / name
        if (
            not entry.is_file()
            or not os.access(entry, os.X_OK)
            or entry.resolve().parent != bin_dir.resolve()
        ):
            raise ValueError("Installed entrypoint missing or outside candidate bin directory")
        with entry.open() as stream:
            if stream.readline().strip() != "#!" + str(python):
                raise ValueError(
                    "Installed entrypoint shebang must use the explicit candidate Python"
                )
    for pth in prefix.glob("lib/python*/site-packages/*.pth"):
        body = pth.read_text().lower()
        if "tessera" in pth.name.lower() or "tessera" in body or "__editable__" in body:
            raise ValueError("Editable/Tessera .pth is not allowed in candidate environment")
    return {
        "python": str(python),
        "bin_dir": str(bin_dir),
        "prefix": str(prefix),
        "wheel": str(wheel),
        "wheel_sha256": expected_sha,
    }


def check_origins(origins, sites):
    sites = [Path(p).resolve() for p in sites]
    if not origins:
        raise ValueError("No Tessera import origins observed")
    for origin in origins.values():
        if not isinstance(origin, str) or not any(
            Path(origin).resolve().is_relative_to(p) for p in sites
        ):
            raise ValueError("Tessera import escaped candidate site-packages")


def check_wheel_metadata(direct, installer, wheel, expected_sha):
    """UV may omit PEP610 hashes; never pretend that omission is an installer hash."""
    wheel = Path(wheel)
    if (
        not isinstance(expected_sha, str)
        or not re.fullmatch(r"[a-f0-9]{64}", expected_sha)
        or not wheel.is_file()
        or hashlib.sha256(wheel.read_bytes()).hexdigest() != expected_sha
    ):
        raise ValueError("Independent wheel SHA256 mismatch or missing")
    if (
        not isinstance(direct, dict)
        or set(direct) != {"url", "archive_info"}
        or not isinstance(direct.get("url"), str)
    ):
        raise ValueError("Only exact noneditable local archive installation metadata is allowed")
    url = urlsplit(direct["url"])
    path = Path(unquote(url.path))
    if (
        url.scheme != "file"
        or url.netloc
        or url.query
        or url.fragment
        or ".." in path.parts
        or not path.is_absolute()
        or str(path) != str(wheel.resolve())
    ):
        raise ValueError("Installation URL does not identify the canonical supplied local wheel")
    archive = direct["archive_info"]
    if not isinstance(archive, dict) or set(archive) - {"hash", "hashes"}:
        raise ValueError("Unknown archive installation metadata")
    hashes = []
    if "hash" in archive:
        hashes.append(archive["hash"])
    if "hashes" in archive:
        if (
            not isinstance(archive["hashes"], dict)
            or set(archive["hashes"]) != {"sha256"}
            or not isinstance(archive["hashes"]["sha256"], str)
        ):
            raise ValueError("Only explicit SHA256 installation hashes are supported")
        hashes.append("sha256=" + archive["hashes"]["sha256"])
    if hashes:
        if any(value != "sha256=" + expected_sha for value in hashes):
            raise ValueError("Conflicting installer wheel SHA256")
        return {
            "installer_archive_sha256": expected_sha,
            "binding_basis": "explicit_installer_hash_plus_verified_wheel",
        }
    if installer.strip() != "uv" or archive != {}:
        raise ValueError(
            "Hashless installation metadata is supported only for standard UV archives"
        )
    return {
        "installer_archive_sha256": None,
        "binding_basis": "uv_exact_local_url_plus_independent_wheel_and_installed_bytes",
    }


def check_wheel_files(archive, dist, sites):
    """Compare product bytes, both RECORD hashes/sizes and immutable wheel metadata."""
    from email.parser import Parser

    prefixes = ("tessera_control/", "tessera_mcp/", "tessera_agent/", "tessera_worker/")
    metadata_names = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
    if len(metadata_names) != 1:
        raise ValueError("Exact wheel distribution metadata required")
    root = metadata_names[0].removesuffix("METADATA")
    metadata = Parser().parsestr(archive.read(root + "METADATA").decode())
    if (
        metadata["Name"].lower().replace("_", "-") != "tessera-control"
        or metadata["Version"] != dist.version
    ):
        raise ValueError("Installed distribution version/name differs from wheel")
    for name in ("METADATA", "WHEEL", "entry_points.txt"):
        if dist.read_text(name) != archive.read(root + name).decode():
            raise ValueError("Installed distribution metadata differs from wheel")
    wheel_record = {
        row[0]: row[1:] for row in csv.reader(io.StringIO(archive.read(root + "RECORD").decode()))
    }
    installed_record = {
        row[0]: row[1:] for row in csv.reader(io.StringIO(dist.read_text("RECORD") or ""))
    }
    names = {
        name for name in archive.namelist() if name.startswith(prefixes) and not name.endswith("/")
    }
    if not names:
        raise ValueError("Bound wheel contains no Tessera implementation")
    for name in names:
        body = archive.read(name)
        expected = [
            "sha256="
            + base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode().rstrip("="),
            str(len(body)),
        ]
        if (
            ".." in Path(name).parts
            or wheel_record.get(name) != expected
            or installed_record.get(name) != expected
            or not any(
                (Path(p) / name).is_file()
                and (Path(p) / name).resolve().is_relative_to(Path(p).resolve())
                and (Path(p) / name).read_bytes() == body
                for p in sites
            )
        ):
            raise ValueError("Installed Tessera bytes or RECORD differ from bound wheel")
    for site in sites:
        for package in prefixes:
            directory = Path(site) / package
            if not directory.exists():
                continue
            for item in directory.rglob("*"):
                if (
                    item.is_file()
                    and "__pycache__" not in item.parts
                    and str(item.relative_to(site)) not in names
                ):
                    raise ValueError("Extra installed Tessera product file outside wheel")
    return len(names)


def current_installation(info):
    """Called inside candidate Python, before fixture/control setup and at receipt."""
    import importlib
    import site
    import sys
    from importlib.metadata import distribution

    checked = check_paths(info["python"], info["bin_dir"], info["wheel"], info["wheel_sha256"])
    if Path(sys.prefix).resolve() != Path(checked["prefix"]).resolve():
        raise ValueError("Gate is not running under candidate Python")
    sites = [
        p
        for p in site.getsitepackages()
        if Path(p).resolve().is_relative_to(Path(sys.prefix).resolve())
    ]
    dist = distribution("tessera-control")
    if not any(
        Path(dist.locate_file("")).resolve().is_relative_to(Path(p).resolve()) for p in sites
    ):
        raise ValueError("Tessera distribution metadata escaped candidate environment")
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    binding = check_wheel_metadata(
        direct, dist.read_text("INSTALLER") or "", info["wheel"], checked["wheel_sha256"]
    )
    with zipfile.ZipFile(info["wheel"]) as archive:
        checked_files = check_wheel_files(archive, dist, sites)
    for name in (
        "tessera_control.api",
        "tessera_control.a2a_peer",
        "tessera_mcp.server",
        "tessera_agent.research_consumer",
        "tessera_worker",
    ):
        importlib.import_module(name)
    origins = {
        name: module.__file__
        for name, module in sys.modules.items()
        if name.startswith(("tessera_control", "tessera_mcp", "tessera_agent", "tessera_worker"))
        and getattr(module, "__file__", None)
    }
    check_origins(origins, sites)
    return {
        **checked,
        **binding,
        "distribution_version": dist.version,
        "import_origins": origins,
        "wheel_matched_product_files": checked_files,
        "site_packages": sites,
        "entrypoint_sha256": {
            name: hashlib.sha256((Path(info["bin_dir"]) / name).read_bytes()).hexdigest()
            for name in ("tessera-mcp", "tessera-a2a-peer")
        },
    }


def inspect_installation(info, env):
    # -I removes caller PYTHONPATH/user-site/cwd import leakage. Only this pure
    # harness helper is added explicitly; product imports must remain installed.
    helper = str(Path(__file__).resolve().parent)
    code = (
        "import sys,json;sys.path.insert(0,sys.argv[1]);"
        "from protocol_installation import current_installation;"
        "print(json.dumps(current_installation(json.loads(sys.argv[2]))))"
    )
    result = subprocess.run(
        [info["python"], "-I", "-c", code, helper, json.dumps(info)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise ValueError("Candidate wheel/import/entrypoint preflight failed")
    return json.loads(result.stdout)
