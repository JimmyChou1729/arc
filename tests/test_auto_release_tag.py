"""Exercise automatic release decisions against disposable Git remotes."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("auto_release_tag", ROOT / "scripts/auto-release-tag.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True, stderr=subprocess.PIPE).strip()


@pytest.fixture
def repository(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
    git(work, "config", "user.name", "Test")
    git(work, "config", "user.email", "test@example.invalid")
    git(work, "remote", "add", "origin", str(remote))

    def commit(value):
        (work / "VERSION").write_text(value + "\n")
        git(work, "add", "VERSION")
        git(work, "commit", "--allow-empty", "-m", "Update")
        git(work, "push", "origin", "HEAD:main")
        head = git(work, "rev-parse", "HEAD")
        monkeypatch.setenv("GITHUB_SHA", head)
        return head

    commit("2.2.0")
    head = commit("2.2.1")
    monkeypatch.chdir(work)
    monkeypatch.setenv("GITHUB_REPOSITORY", release.REPOSITORY)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    pages = [[{"merged_at": "2026-01-01", "merge_commit_sha": head,
               "base": {"ref": "main", "repo": {"full_name": release.REPOSITORY}}}]]
    original = release.command

    def command(*args):
        if args[0] == "gh":
            return json.dumps(pages)
        return original(*args)

    monkeypatch.setattr(release, "command", command)
    return work, remote, commit, pages


def test_release_creates_annotated_tag_at_tested_commit_and_replays(repository):
    work, remote, commit, _ = repository
    tag = release.candidate()
    assert tag == "v2.2.1"
    tested = git(work, "rev-parse", "HEAD")
    release.publish(tag)
    obj = git(remote, "rev-parse", tag)
    assert git(remote, "cat-file", "-t", obj) == "tag"
    assert git(remote, "rev-parse", tag + "^{}") == tested
    release.publish(tag)
    assert git(remote, "rev-parse", tag) == obj
    # A rerun must not tag a newer main commit that arrived during CI.
    commit("2.2.2")
    git(work, "checkout", "--detach", tested)
    import os
    os.environ["GITHUB_SHA"] = tested
    release.publish(tag)
    assert git(remote, "rev-parse", tag) == obj


def test_no_version_change_does_not_backfill_missing_tag(repository):
    _, remote, commit, _ = repository
    commit("2.2.1")
    assert release.candidate() == ""
    assert git(remote, "tag", "--list") == ""


@pytest.mark.parametrize("value", ["2.1.9", "02.3.0", "2.3", "2.3.0-rc1"])
def test_invalid_or_decreasing_version_is_rejected(repository, value):
    work, remote, commit, pages = repository
    pages[0][0]["merge_commit_sha"] = commit(value)
    with pytest.raises(ValueError):
        release.candidate()
    assert git(remote, "tag", "--list") == ""


@pytest.mark.parametrize("field,value", [
    ("GITHUB_REPOSITORY", "someone/fork"), ("GITHUB_EVENT_NAME", "pull_request"),
    ("GITHUB_EVENT_NAME", "workflow_dispatch"), ("GITHUB_REF", "refs/heads/dev"),
    ("GITHUB_REF", "refs/tags/v2.2.1"), ("GITHUB_SHA", "0" * 40),
])
def test_nonrelease_context_cannot_select_or_publish(repository, monkeypatch, field, value):
    monkeypatch.setenv(field, value)
    with pytest.raises(ValueError):
        release.candidate()
    with pytest.raises(ValueError):
        release.publish("v2.2.1")


@pytest.mark.parametrize("change", ["no-pr", "unmerged", "other-sha", "other-base", "other-repo"])
def test_version_change_requires_matching_merged_pr(repository, change):
    _, _, _, pages = repository
    pr = pages[0][0]
    if change == "no-pr": pages.clear()
    elif change == "unmerged": pr["merged_at"] = None
    elif change == "other-sha": pr["merge_commit_sha"] = "0" * 40
    elif change == "other-base": pr["base"]["ref"] = "stable"
    else: pr["base"]["repo"]["full_name"] = "someone/fork"
    with pytest.raises(ValueError, match="merged PR"):
        release.candidate()


def test_conflicting_remote_tag_is_never_moved(repository):
    work, remote, _, _ = repository
    git(work, "tag", "v2.2.1", "HEAD^")
    git(work, "push", "origin", "refs/tags/v2.2.1")
    previous = git(remote, "rev-parse", "v2.2.1")
    with pytest.raises(ValueError, match="refusing to move"):
        release.publish("v2.2.1")
    assert git(remote, "rev-parse", "v2.2.1") == previous


def test_concurrent_same_commit_publication_is_idempotent(repository, monkeypatch):
    _, remote, _, _ = repository
    original = release.command

    def command(*args):
        result = original(*args)
        if args[:2] == ("git", "push"):
            raise subprocess.CalledProcessError(1, args)
        return result

    monkeypatch.setattr(release, "command", command)
    release.publish("v2.2.1")
    assert git(remote, "rev-parse", "v2.2.1^{}") == git(remote, "rev-parse", "main")


def test_failed_push_without_remote_tag_fails(repository, monkeypatch):
    _, remote, _, _ = repository
    original = release.command

    def command(*args):
        if args[:2] == ("git", "push"):
            raise subprocess.CalledProcessError(1, args)
        return original(*args)

    monkeypatch.setattr(release, "command", command)
    with pytest.raises(subprocess.CalledProcessError):
        release.publish("v2.2.1")
    assert git(remote, "tag", "--list") == ""


def test_wrong_tag_is_rejected(repository):
    with pytest.raises(ValueError, match="differs from VERSION"):
        release.publish("v9.9.9")


def test_workflow_requires_full_ci_and_release_validation_before_write():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["auto-tag"]
    assert job["needs"] == "test"
    for guard in ("!cancelled()", "needs.test.result == 'success'", "github.event_name == 'push'",
                  "github.ref == 'refs/heads/main'", f"github.repository == '{release.REPOSITORY}'"):
        assert guard in job["if"]
    assert job["permissions"] == {"contents": "write", "pull-requests": "read"}
    steps = job["steps"]
    assert steps[0]["with"] == {"ref": "${{ github.sha }}", "fetch-depth": 0}
    names = [step.get("name") for step in steps]
    assert names.index("Select merged release PR") < names.index("Validate release before tagging") < names.index("Publish immutable version tag")
    check = steps[names.index("Validate release before tagging")]
    assert 'check-release-tag.py --tag "$RELEASE_TAG"' in check["run"]
    if release.REPOSITORY != "tririver/ac-foundation":
        assert "--check-pins" in check["run"]
        assert "check-generated-foundation.py" in check["run"]
    assert all(step.get("if") == "steps.release.outputs.tag != ''"
               for step in steps[names.index("Select merged release PR") + 1:])
