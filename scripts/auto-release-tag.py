#!/usr/bin/env python3
"""Select merged release PRs and publish immutable tags after CI validation."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

REPOSITORY = "tririver/arc"


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def version(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
        raise ValueError(f"Invalid release version: {value!r}")
    return tuple(map(int, value.split(".")))


def checked_head() -> str:
    if (os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
            or os.environ.get("GITHUB_EVENT_NAME") != "push"
            or os.environ.get("GITHUB_REF") != "refs/heads/main"):
        raise ValueError("Automatic tags require an upstream main push")
    head = command("git", "rev-parse", "HEAD")
    if head != os.environ.get("GITHUB_SHA"):
        raise ValueError("Checkout differs from the tested commit")
    command("git", "fetch", "origin", "main")
    command("git", "merge-base", "--is-ancestor", head, "origin/main")
    return head


def candidate() -> str:
    head = checked_head()
    current = command("git", "show", f"{head}:VERSION")
    previous = command("git", "show", f"{head}^1:VERSION")
    if current == previous:
        return ""
    if version(current) <= version(previous):
        raise ValueError("Release version must increase")
    pages = json.loads(command(
        "gh", "api", "--paginate", "--slurp",
        f"repos/{REPOSITORY}/commits/{head}/pulls",
    ))
    if not any(
        pr.get("merged_at") and pr.get("merge_commit_sha") == head
        and pr["base"]["ref"] == "main"
        and pr["base"]["repo"]["full_name"] == REPOSITORY
        for page in pages for pr in page
    ):
        raise ValueError("Version change must come from a merged PR targeting upstream main")
    return f"v{current}"


def remote_target(tag: str) -> str | None:
    ref = f"refs/tags/{tag}"
    rows = command("git", "ls-remote", "origin", ref, ref + "^{}")
    refs = {name: sha for sha, name in (line.split() for line in rows.splitlines())}
    return refs.get(ref + "^{}", refs.get(ref))


def publish(tag: str) -> None:
    head = checked_head()
    current = command("git", "show", f"{head}:VERSION")
    version(current)
    if tag != f"v{current}":
        raise ValueError("Requested tag differs from VERSION")
    target = remote_target(tag)
    if target is not None:
        if target != head:
            raise ValueError(f"Published tag {tag} points to another commit; refusing to move it")
        print(f"{tag} already points to {head}")
        return
    command("git", "-c", "user.name=github-actions[bot]", "-c",
            "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "tag", "-a", tag, head, "-m", f"{REPOSITORY} {tag}")
    try:
        command("git", "push", "origin", f"refs/tags/{tag}")
    except subprocess.CalledProcessError:
        # A concurrent retry may have published the same commit first.
        if remote_target(tag) != head:
            raise
    if remote_target(tag) != head:
        raise ValueError("Remote tag verification failed")
    print(f"Published {tag} at {head}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("candidate", "publish"))
    parser.add_argument("--tag")
    args = parser.parse_args()
    try:
        if args.mode == "candidate":
            print(candidate())
        elif args.tag:
            publish(args.tag)
        else:
            parser.error("publish requires --tag")
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
