#!/usr/bin/env python3
"""Build and validate deterministic ARC plugin ZIPs without installing them."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import re
import stat
import sys
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "scripts/schemas/agent-plugin-1.0.0.json"
PROFILES = ("private-local", "public-skills")
MAX_ARCHIVE = 100_000_000
MAX_MEMBER = 100 * 1024**2
MAX_TOTAL = 512 * 1024**2
MAX_ENTRIES = 5000
MANIFESTS = ("plugin.json", ".codex-plugin/plugin.json", ".claude-plugin/plugin.json", ".agent-plugin/plugin.json")
CATEGORIES = {"Productivity", "Creativity", "Developer Tools", "Business & Operations", "Data & Analytics", "Communication", "Education & Research", "Security", "Finance", "Healthcare", "Travel", "Entertainment", "Other"}
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")


class BundleError(ValueError):
    pass


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise BundleError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    def constant(value):
        raise BundleError(f"non-finite JSON value: {value}")
    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


def safe_path(name):
    if not isinstance(name, str) or not name or name != name.strip() or "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise BundleError("entry must be a non-empty relative POSIX path without outer whitespace")
    parts = (name[:-1] if name.endswith("/") else name).split("/")
    if len(parts) > 20 or any(part in {"", ".", ".."} for part in parts) or any(unicodedata.category(char) in {"Cc", "Cf"} for char in name):
        raise BundleError("entry has an empty/parent/control segment or exceeds 20 segments")
    if len(name.encode("utf-8")) > 1024:
        raise BundleError("ARC path limit is 1024 UTF-8 bytes")
    return "/".join(parts)


def read_zip(path):
    if path.stat().st_size > MAX_ARCHIVE:
        raise BundleError("compressed archive exceeds 100 MB")
    files = {}
    normalized = set()
    directories = set()
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if not entries or len(entries) > MAX_ENTRIES or sum(item.file_size for item in entries) > MAX_TOTAL:
            raise BundleError("archive is empty or exceeds entry/expanded-size limits")
        for item in entries:
            if item.orig_filename != item.filename:
                raise BundleError("archive filename contains a truncated control character")
            name = safe_path(item.filename)
            key = unicodedata.normalize("NFC", name).casefold()
            if key in normalized:
                raise BundleError(f"duplicate or normalized path collision: {name}")
            normalized.add(key)
            mode = stat.S_IFMT(item.external_attr >> 16)
            expected = stat.S_IFDIR if item.is_dir() else stat.S_IFREG
            if mode not in {0, expected} or item.flag_bits & 1 or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise BundleError(f"unsupported/encrypted entry: {name}")
            if item.file_size > MAX_MEMBER:
                raise BundleError(f"entry exceeds 100 MiB: {name}")
            if item.is_dir():
                directories.add(name)
            else:
                files[name] = archive.read(item)
        parents = {str(Path(name).parent).replace("\\", "/") for name in files}
        for name in list(files) + list(directories):
            pieces = name.split("/")
            parents.update("/".join(pieces[:index]) for index in range(1, len(pieces)))
        normalize = lambda name: unicodedata.normalize("NFC", name).casefold()
        if {normalize(name) for name in files} & {normalize(name) for name in directories | parents}:
            raise BundleError("an archive path is both a file and a directory")
    if "plugin.json" not in files:
        top = {name.split("/")[0] for name in files}
        if len(top) != 1:
            raise BundleError("plugin root is missing or has siblings")
        prefix = next(iter(top)) + "/"
        files = {name.removeprefix(prefix): value for name, value in files.items()}
    if "plugin.json" not in files:
        raise BundleError("ARC bundles require root plugin.json")
    if any(name.endswith("/plugin.json") and name not in MANIFESTS for name in files):
        raise BundleError("bundle has an ambiguous nested plugin manifest")
    return files


def read_directory(root, *, for_build=False):
    files = {}
    for path in sorted(root.rglob("*")):
        if for_build and set(path.relative_to(root).parts) & {".git", "__pycache__", ".DS_Store", "node_modules", ".venv", "venv", "local"}:
            continue
        if path.is_symlink():
            raise BundleError(f"symlink is not a package entry: {path.relative_to(root)}")
        if path.is_file():
            if path.stat().st_size > MAX_MEMBER:
                raise BundleError("source entry exceeds 100 MiB")
            files[safe_path(path.relative_to(root).as_posix())] = path.read_bytes()
    return files


def text(value, limit, *, multiline=False):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise BundleError(f"expected nonblank text of at most {limit} characters")
    for char in value:
        if (unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"}) and not (multiline and char in "\n\r\t"):
            raise BundleError("text contains unsupported control or formatting characters")
    return value


def https_url(value, limit):
    value = text(value, limit)
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or any(char.isspace() for char in value):
        raise BundleError("URL must use HTTPS without credentials")


def reference(value, files, *, prefix=""):
    if not isinstance(value, str) or not value.startswith("./"):
        raise BundleError("manifest references must start with ./")
    name = safe_path(prefix + value[2:])
    if name not in files and not any(item.startswith(name + "/") for item in files):
        raise BundleError(f"referenced path is absent: {name}")
    return name


def icon(name, data):
    if len(data) > 5 * 1024**2:
        raise BundleError("icon exceeds 5 MiB")
    suffix = Path(name).suffix.lower()
    if suffix == ".svg":
        element = ET.fromstring(data)
        if element.tag != "{http://www.w3.org/2000/svg}svg":
            raise BundleError("invalid SVG root")
        box = [float(item) for item in re.split(r"[ ,]+", element.attrib.get("viewBox", "").strip())]
        if len(box) != 4 or not all(math.isfinite(value) for value in box) or box[2] != box[3] or not box[2] >= 48:
            raise BundleError("SVG needs a square numeric viewBox of at least 48 units")
        for dimension in ("width", "height"):
            if dimension in element.attrib and float(element.attrib[dimension]) != box[2]:
                raise BundleError("SVG width/height must agree with its square viewBox")
    elif suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as bitmap:
            width, height = bitmap.size
            if bitmap.format not in {"PNG", "JPEG", "WEBP"} or not 48 <= width == height <= 4096:
                raise BundleError("bitmap icon must be square, 48–4096 pixels, PNG/JPEG/WebP")
            bitmap.verify()
    else:
        raise BundleError("unsupported icon format")


def interface(settings, files, public):
    if not isinstance(settings, dict):
        raise BundleError("interface must be an object")
    for name, limit in {"displayName": 30 if public else 80, "shortDescription": 30 if public else 240,
                        "longDescription": 4000, "developerName": 80 if public else 120}.items():
        text(settings.get(name), limit, multiline=name == "longDescription")
    if settings.get("category", "Other") not in CATEGORIES:
        raise BundleError("unsupported category")
    capabilities = settings.get("capabilities", [])
    if not isinstance(capabilities, list) or len(capabilities) > 20:
        raise BundleError("capabilities must be an array of at most 20 entries")
    for capability in capabilities:
        text(capability, 120)
    prompts = settings.get("defaultPrompt", [])
    prompts = [prompts] if isinstance(prompts, str) else prompts
    if not isinstance(prompts, list) or len(prompts) > 3:
        raise BundleError("defaultPrompt must be a string or at most three prompts")
    normalized = []
    for prompt in prompts:
        text(prompt, 128 if public else 512)
        if "@" in prompt:
            raise BundleError("starter prompts must not depend on @mentions")
        normalized.append(" ".join(unicodedata.normalize("NFKC", prompt).split()))
    if len(set(normalized)) != len(normalized):
        raise BundleError("starter prompts must be unique")
    for name in ("websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL"):
        if name in settings:
            https_url(settings[name], 1024 if public else 2048)
    if public and settings.get("screenshots") is not None:
        raise BundleError("public-skills excludes screenshots")
    for name in ("logo", "composerIcon", "logoDark", "composerIconDark"):
        if name in settings or name in {"logo", "composerIcon"}:
            asset = reference(settings.get(name), files)
            if asset not in files:
                raise BundleError("icon reference must be a regular file")
            icon(asset, files[asset])
    for name, background in (("brandColor", 1.0), ("brandColorDark", 0.0152085)):
        if name in settings:
            color = settings[name]
            if not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
                raise BundleError("brand color must be six-digit hex")
            rgb = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [item / 12.92 if item <= 0.04045 else ((item + 0.055) / 1.055)**2.4 for item in rgb]
            luminance = sum(a * b for a, b in zip(linear, (0.2126, 0.7152, 0.0722)))
            if (max(luminance, background) + 0.05) / (min(luminance, background) + 0.05) < 2:
                raise BundleError("brand color contrast is below 2:1")


def validate_files(files, profile):
    public = profile == "public-skills"
    issues = []
    def check(layer, path, operation):
        try:
            operation()
        except (ValueError, TypeError, KeyError, UnicodeError, ET.ParseError, yaml.YAMLError, OSError) as exc:
            issues.append({"layer": layer, "path": path, "message": str(exc)})
    manifests = {}
    for name in MANIFESTS:
        if name in files:
            check("schema", name, lambda name=name: manifests.update({name: strict_json(files[name].decode("utf-8"))}))
    if "plugin.json" not in manifests:
        issues.append({"layer": "schema", "path": "plugin.json", "message": "portable root manifest is required"})
    validator = Draft202012Validator(strict_json(SCHEMA.read_bytes()))
    for name, manifest in manifests.items():
        if name in {"plugin.json", ".agent-plugin/plugin.json"} or isinstance(manifest, dict) and "$schema" in manifest:
            for error in validator.iter_errors(manifest):
                issues.append({"layer": "schema", "path": name + ":" + ".".join(map(str, error.path)), "message": error.message})
    canonical = manifests.get("plugin.json", {})
    canonical = canonical if isinstance(canonical, dict) else {}
    def openai(manifest):
        extensions = manifest.get("extensions", {})
        if not isinstance(extensions, dict) or not isinstance(extensions.get("com.openai", {}), dict):
            raise BundleError("extensions and com.openai must be objects")
        return extensions.get("com.openai", {})
    canonical_interface = None
    def root_settings():
        nonlocal canonical_interface
        canonical_interface = openai(canonical).get("interface")
        interface(canonical_interface, files, public)
    check("submission" if public else "package", "plugin.json", root_settings)
    for name, manifest in manifests.items():
        def validate_manifest():
            if not isinstance(manifest, dict):
                raise BundleError("manifest must be an object")
            if public:
                def disabled(value):
                    if isinstance(value, dict):
                        for key, child in value.items():
                            if key in {"apps", "hooks", "mcpServers", "screenshots"} and child is not None:
                                raise BundleError(f"public-skills excludes {key}, including shadowed overlays")
                            disabled(child)
                    elif isinstance(value, list):
                        for child in value:
                            disabled(child)
                disabled(manifest)
            text(manifest.get("name"), 64)
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", manifest["name"]):
                raise BundleError("submission name has unsupported characters")
            version = text(manifest.get("version"), 64)
            if not SEMVER.fullmatch(version):
                raise BundleError("version must use semantic versioning")
            text(manifest.get("description"), 1024, multiline=True)
            author = manifest.get("author")
            if not isinstance(author, dict):
                raise BundleError("author must be an object")
            text(author.get("name"), 120)
            if "email" in author:
                text(author["email"], 320)
            if "url" in author:
                https_url(author["url"], 2048)
            for field in ("homepage", "repository"):
                if field in manifest:
                    https_url(manifest[field], 2048)
            if "keywords" in manifest and (not isinstance(manifest["keywords"], list) or any(not isinstance(value, str) for value in manifest["keywords"])):
                raise BundleError("keywords must be an array of strings")
            if "id" in manifest:
                text(manifest["id"], 120)
            for key in ("name", "version", "description", "author", "license"):
                if manifest.get(key) != canonical.get(key):
                    raise BundleError(f"compatibility {key} differs from the portable identity")
            extension = openai(manifest)
            for settings in (manifest, extension):
                for key in ("apps", "hooks", "mcpServers"):
                    if settings.get(key) is not None:
                        if public:
                            raise BundleError(f"public-skills excludes {key}, including shadowed overlays")
                        value = settings[key]
                        if isinstance(value, str):
                            component = reference(value, files)
                            if component in files and component.endswith(".json"):
                                strict_json(files[component].decode("utf-8"))
                        else:
                            raise BundleError("ARC private component declarations require a contained JSON path")
                if "interface" in settings:
                    interface(settings["interface"], files, public)
            if "skills" in manifest and manifest["skills"] != "./skills/":
                raise BundleError("legacy skills must refer to ./skills/")
            if name == ".codex-plugin/plugin.json" and canonical_interface != manifest.get("interface"):
                raise BundleError("OpenAI overlay presentation differs from the portable extension")
        check("submission" if public else "package", name, validate_manifest)
    skill_names = set()
    skills = [name for name in files if name.startswith("skills/") and name.endswith("/SKILL.md")]
    if not skills:
        issues.append({"layer": "package", "path": "skills/", "message": "at least one valid skill is required"})
    for name in skills:
        def validate_skill():
            parts = name.split("/")
            if len(parts) != 3 or parts[1].startswith("."):
                raise BundleError("skills must be immediate, visible children of skills/")
            source = files[name].decode("utf-8")
            lines = source.splitlines()
            if not lines or lines[0] != "---" or "---" not in lines[1:]:
                raise BundleError("skill needs closed YAML frontmatter")
            end = lines[1:].index("---") + 1
            header = yaml.safe_load("\n".join(lines[1:end]))
            if not isinstance(header, dict):
                raise BundleError("skill frontmatter must be a mapping")
            identity = text(header.get("name"), 64)
            text(header.get("description"), 1024, multiline=True)
            if identity != parts[1] or len(str(canonical.get("name", "")) + ":" + identity) > 64 or identity in skill_names:
                raise BundleError("skill name must match its folder and be unique within the 64-character combined identity")
            skill_names.add(identity)
            if not "\n".join(lines[end + 1:]).strip():
                raise BundleError("skill body must be non-empty")
        check("package", name, validate_skill)
        metadata_path = str(Path(name).parent / "agents/openai.yaml")
        if metadata_path in files:
            def validate_agent():
                document = yaml.safe_load(files[metadata_path].decode("utf-8"))
                if not isinstance(document, dict) or not isinstance(document.get("interface"), dict):
                    raise BundleError("skill agent metadata needs an interface mapping")
                settings = document["interface"]
                text(settings.get("display_name"), 30)
                text(settings.get("short_description"), 120)
                if "default_prompt" in settings:
                    text(settings["default_prompt"], 128)
                for field in ("icon_small", "icon_large"):
                    if field in settings:
                        value = settings[field]
                        value = value if isinstance(value, str) and value.startswith("./") else "./" + value if isinstance(value, str) else value
                        asset = reference(value, files, prefix=str(Path(name).parent) + "/")
                        icon(asset, files[asset])
                if "policy" in document and not isinstance(document["policy"], dict):
                    raise BundleError("skill policy must be a mapping")
                if "brand_color" in settings and not re.fullmatch(r"#[0-9a-fA-F]{6}", str(settings["brand_color"])):
                    raise BundleError("skill brand_color must be six-digit hex")
            check("package", metadata_path, validate_agent)
    def clean():
        if any(name.endswith("/plugin.json") and name not in MANIFESTS for name in files):
            raise BundleError("bundle has an ambiguous nested plugin manifest")
        if "LICENSE" not in files or b"MIT" not in files["LICENSE"]:
            raise BundleError("ARC bundle must preserve the MIT license")
        normalized = set()
        parents = {"/".join(name.split("/")[:index]) for name in files for index in range(1, len(name.split("/")))}
        if {unicodedata.normalize("NFC", name).casefold() for name in files} & {unicodedata.normalize("NFC", name).casefold() for name in parents}:
            raise BundleError("normalized file/directory type conflict")
        for name, data in files.items():
            safe_path(name)
            key = unicodedata.normalize("NFC", name).casefold()
            if key in normalized:
                raise BundleError(f"normalized collision: {name}")
            normalized.add(key)
            parts = name.split("/")
            if set(parts) & {".git", ".venv", "venv", "__pycache__", "node_modules", "local", ".DS_Store"} or Path(name).suffix.lower() in {".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".pdf", ".zip", ".pem", ".key"} or any(part == ".env" or part.startswith(".env.") for part in parts):
                raise BundleError(f"unrelated/cache/binary/credential entry: {name}")
            if public and (parts[0] in {"hooks", "apps"} or Path(name).name in {"mcp.json", ".mcp.json", ".app.json"}):
                raise BundleError(f"public-skills excludes component file: {name}")
            if Path(name).suffix.lower() in {".py", ".sh", ".md", ".json", ".yml", ".yaml", ".js"}:
                content = data.decode("utf-8")
                if re.search(r"(?:/Users/[^/\s]+/|/home/[^/\s]+/|[A-Za-z]:\\Users\\)", content) or "BEGIN PRIVATE KEY" in content or "sitecustomize" in name:
                    raise BundleError(f"machine path/credential/experiment residue: {name}")
        if len(files) > MAX_ENTRIES or sum(map(len, files.values())) > MAX_TOTAL or any(len(data) > MAX_MEMBER for data in files.values()):
            raise BundleError("bundle exceeds expanded size/entry limits")
    check("project", ".", clean)
    return {"schema_version": "arc.plugin_validation.v1", "profile": profile, "valid": not issues,
            "checks": {layer: "failed" if any(item["layer"] == layer for item in issues) else "passed" for layer in ("schema", "package", "project", "submission") if layer != "submission" or public},
            "issues": issues, "entry_count": len(files), "expanded_bytes": sum(map(len, files.values())),
            "public_submission_ready": False, "runtime_content_verified": False,
            "limitations": ["Offline format checks do not establish publisher verification, policy attestations, skill scans, or platform installation.",
                            "A release needs approved versions and reachable, tested runtime pins; packaging does not update them."]}


def build(source, output, profile):
    files = read_directory(source, for_build=True)
    if profile == "public-skills":
        files = {name: data for name, data in files.items() if name.split("/")[0] not in {"bin", "dsh"}}
    files["LICENSE"] = (ROOT / "LICENSE").read_bytes()
    report = validate_files(files, profile)
    if not report["valid"]:
        return report
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = output.with_suffix(output.suffix + ".tmp")
    with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            item = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            item.create_system = 3
            executable = name.startswith("bin/") or name.endswith("/arc-runtime") or name.endswith(".sh")
            item.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
            item.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(item, data, compresslevel=9)
    verified = validate_files(read_zip(staged), profile)
    if not verified["valid"]:
        staged.unlink()
        return verified
    os.replace(staged, output)
    report.update(verified)
    report["zip"] = str(output)
    report["sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    report["files"] = [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in sorted(files.items())]
    output.with_suffix(output.suffix + ".sha256").write_text(report["sha256"] + "  " + output.name + "\n")
    output.with_suffix(output.suffix + ".manifest.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("build")
    make.add_argument("--source", type=Path, default=ROOT / "plugins/arc")
    make.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("path", type=Path)
    for command in (make, validate):
        command.add_argument("--profile", choices=PROFILES, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            if args.output.resolve().is_relative_to(args.source.resolve()):
                raise BundleError("write archives outside the source plugin directory")
            report = build(args.source, args.output, args.profile)
        else:
            report = validate_files(read_directory(args.path) if args.path.is_dir() else read_zip(args.path), args.profile)
    except (BundleError, OSError, zipfile.BadZipFile, RuntimeError) as exc:
        report = {"valid": False, "profile": args.profile, "issues": [{"layer": "archive", "message": str(exc)}]}
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
