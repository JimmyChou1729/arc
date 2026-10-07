#!/usr/bin/env python3
"""Verify a ZIP through a fresh Git runtime outside the source checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from plugin_bundle import read_zip, validate_files

SMOKE = r'''
import importlib.metadata as metadata
import json, os, sys
from pathlib import Path
import httpx, socksio
from jsonschema import Draft202012Validator
from ac_llm import InvalidRequestError, HostCoordinator,HostTaskService,JsonOutput,LLMClient,LLMCompleted,LLMExecutionOptions,LLMPaused,LLMRequest,ModelSelection
root=Path(sys.argv[1]); lock=json.loads(Path(sys.argv[2]).read_text())
provenance={}
for source in lock['sources']:
    for name in source['packages']:
        dist=metadata.distribution(name)
        direct=json.loads(dist.read_text('direct_url.json'))
        assert direct['vcs_info']['commit_id']==source['commit'], name
        assert direct['url'].removesuffix('.git')==source['repository'].removesuffix('.git'), name
        provenance[name]={'version':dist.version,'source':direct}
assert metadata.version('socksio')=='1.0.0'
old={key:os.environ.get(key) for key in ('ALL_PROXY','all_proxy')}
try:
    for key in old:os.environ[key]='socks5h://127.0.0.1:1'
    with httpx.Client() as probe:assert probe.trust_env
finally:
    for key,value in old.items():
        if value is None:os.environ.pop(key,None)
        else:os.environ[key]=value
client=LLMClient(); service=HostTaskService()
opts=LLMExecutionOptions(host_coordinator=HostCoordinator('fresh-zip-fixture',native_fallback=False,fresh_context=True),task_binding={'fresh_context_required':True})
req=LLMRequest('fresh-zip-smoke','Return the fixture value 42.',JsonOutput({'type':'object','properties':{'answer':{'const':42}},'required':['answer'],'additionalProperties':False}),ModelSelection(provider='host'))
location={'run_root':root/'runs','run_id':'host-smoke'}
first=client.generate(req,**location,options=opts); assert isinstance(first.outcome,LLMPaused)
task=service.export(**location,task_id=service.pending(**location)[0]['task_id'])
response={'schema_version':'ac.llm.host_response.v1','task_id':task['task_id'],'request_sha256':task['request_sha256'],'actor':{'actor_id':'offline-fixture','kind':'fake','context_id':'offline-fixture-context'},'isolation':'fresh_context','output':{'schema_version':'ac.llm.host_turn.v1','state':'complete','result':{'answer':42},'host_request':None}}
Draft202012Validator(task['response_schema']).validate(response)
bad={**response,'actor':{**response['actor'],'kind':'agent'}}
assert not Draft202012Validator(task['response_schema']).is_valid(bad)
try:
    service.submit(**location,response=bad)
except InvalidRequestError as exc:
    assert 'actor.kind' in str(exc)
else:
    raise AssertionError('fresh task accepted coordinator actor')
service.submit(**location,response=response)
completed=client.resume(**location,options=opts); assert isinstance(completed.outcome,LLMCompleted)
assert completed.outcome.value=={'answer':42}
assert service.submit(**location,response=response)['reused']
assert client.resume(**location,options=opts).outcome.value==completed.outcome.value
assert len(service.pending(**location,include_completed=True))==1
print(json.dumps({'executed':True,'protocol_accepted':True,'scientific_accepted':None,'report_delivered':None,'interpreter':sys.executable,'packages':provenance,'socksio':metadata.version('socksio')}))
'''

NETWORK = r'''
import dataclasses,json,sys
from unittest.mock import patch
from pathlib import Path
from arc_paper import ArcPaperService,DocumentTarget
root=Path(sys.argv[1]); paper=sys.argv[2]
result={'executed':True,'scientific_accepted':None,'report_delivered':None}
try:
    service=ArcPaperService(cache_root=root/'paper-cache')
    metadata=service.get_metadata(paper)
    result['metadata_received']=bool(metadata)
    assert result['metadata_received'], 'empty metadata'
    target=DocumentTarget('reference',reference=paper)
    toc=service.get_table_of_contents(target,source_format='html')
    (root/'toc.json').write_text(json.dumps(dataclasses.asdict(toc),indent=2))
    assert toc.entries, 'empty table of contents'
    exact=DocumentTarget('document',document=toc.source.document)
    section=service.get_section(exact,toc.entries[0].section_id)
    assert section.text.strip(), 'empty section'
    # Exact cached document reads must work with networking explicitly forbidden.
    def forbidden(*args,**kwargs):raise AssertionError('warm document read attempted network')
    with patch('httpx.Client.send', side_effect=forbidden):
        warm=ArcPaperService(cache_root=root/'paper-cache').get_table_of_contents(exact)
    assert warm.source.document.parsed_document_sha256==toc.source.document.parsed_document_sha256
    result.update(document=dataclasses.asdict(toc.source.document),section_characters=len(section.text),warm_cache_same_fingerprint=True)
    result['passed']=True
except Exception as exc:
    result.update(passed=False,error_type=type(exc).__name__,code=getattr(exc,'code',None),message=str(exc))
print(json.dumps(result))
sys.exit(0 if result['passed'] else 1)
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--scientific", action="store_true")
    parser.add_argument("--owned-tex", action="store_true", help="with --report, explicitly initialize pinned user-owned TeX")
    parser.add_argument("--network-paper")
    parser.add_argument("--verify-private-cache", action="store_true", help="require real uv to populate the selected private cache")
    args = parser.parse_args(argv)
    if args.owned_tex and not args.report:
        parser.error("--owned-tex requires --report")
    root = args.output_dir.expanduser().resolve()
    if root.exists():
        parser.error("output-dir must not exist; cold verification never reuses old state")
    if root.is_relative_to(Path(__file__).resolve().parents[1]):
        parser.error("output-dir must be outside the source checkout")
    if args.network_paper and os.environ.get("ARC_RUN_NET_TESTS") != "1":
        parser.error("real network verification requires ARC_RUN_NET_TESTS=1")
    files = read_zip(args.zip)
    validation = validate_files(files, "public-skills")
    if not validation["valid"]:
        print(json.dumps(validation)); return 1
    root.mkdir(parents=True)
    plugin = root / "plugin"
    for name, data in files.items():
        target = plugin / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    scripts = plugin / "skills/arc/scripts"
    launcher = ["bash", str(scripts / "arc-runtime")]
    env = dict(os.environ)
    for name in ("AC_FOUNDATION_REPO_ROOT", "AC_PRODUCT_REPO_ROOT", "ARC_REQUIRE_REPO_ROOT", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "AC_RUNTIME_SOURCES_FILE", "AC_RUNTIME_CONSTRAINTS_FILE", "AC_LLM_PROVIDER_CONFIG", "ARC_REPORT_ENVIRONMENT"):
        env.pop(name, None)
    env.update(AC_INSTALL_SOURCE="git", AC_HOME=str(root / "ac"), AC_RUNTIME_HOME=str(root / "runtimes"),
               AC_DOCUMENT_CACHE=str(root / "document-cache"), ARC_PAPER_CACHE=str(root / "paper-cache"),
               PYTHONDONTWRITEBYTECODE="1", AC_INSTALL_PYTHON_BIN=sys.executable,
               AC_LLM_HOST_COORDINATOR=json.dumps({"coordinator_id": "fresh-zip-fixture", "default_provider": "host", "native_fallback": False}))
    result = {"schema_version": "arc.fresh_plugin_verification.v1", "zip_sha256": hashlib.sha256(args.zip.read_bytes()).hexdigest(),
              "commands": [], "checks": {name: {"executed": False, "status": "not_requested"} for name, enabled in (("scientific", args.scientific), ("report", args.report), ("network", args.network_paper)) if not enabled}}
    def command(label, command):
        completed = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
        (root / (label + ".stdout")).write_text(completed.stdout)
        (root / (label + ".stderr")).write_text(completed.stderr)
        result["commands"].append({"label": label, "executed": True, "exit_code": completed.returncode})
        if completed.returncode:
            raise RuntimeError(f"{label} failed with exit {completed.returncode}; inspect its saved stdout/stderr")
        return completed.stdout
    try:
        command("setup", [*launcher, "setup"])
        runtime = json.loads(command("runtime-doctor", [*launcher, "doctor"]))
        assert runtime["ready"] and runtime["source_mode"] == "git"
        expected_constraints = hashlib.sha256(files["skills/arc/scripts/runtime-constraints.txt"]).hexdigest()
        installed = json.loads((Path(runtime["runtime"]) / "install.ok").read_text())
        assert installed["identity"]["constraints_sha256"] == expected_constraints
        result["checks"]["runtime"] = runtime
        marker = Path(runtime["runtime"]) / "install.ok"
        committed = marker.read_bytes()
        command("setup-repeat", [*launcher, "setup"])
        command("setup-retry-ready", [*launcher, "setup", "--retry"])
        assert marker.read_bytes() == committed
        result["checks"]["setup_idempotent"] = {"executed": True, "passed": True}
        if args.verify_private_cache:
            selected_cache = runtime["install_paths"]["UV_CACHE_DIR"]
            cache = Path(selected_cache["path"])
            assert selected_cache["source"] == "private_default" and selected_cache["active"]
            assert cache.is_relative_to(Path(runtime["runtime"])) and any(cache.iterdir())
            result["checks"]["private_cache"] = {"executed": True, "passed": True, "path": str(cache),
                "default_xdg_cache": env.get("XDG_CACHE_HOME"), "evidence": "real package installation populated private uv cache"}
        smoke = root / "host-smoke.py"
        smoke.write_text(SMOKE)
        result["checks"]["host"] = json.loads(command("host-smoke", [*launcher, "script", str(smoke), str(root), str(scripts / "runtime-sources.json")]))
        result["checks"]["doctor"] = json.loads(command("environment-doctor", [*launcher, "script", str(scripts / "doctor-arc.py"), "--project-dir", str(root)]))
        if args.scientific:
            selected = [*launcher, "--requirements", str(scripts / "scientific-requirements.txt")]
            command("scientific-setup", [*selected, "setup"])
            result["checks"]["scientific"] = json.loads(command("scientific-verify", [*selected, "script", str(scripts / "scientific-python.py"), "--verify"]))
        if args.report:
            tex_args = ["--tex-dir", str(root / "report-tex")] if args.owned_tex else []
            setup_script = [*launcher, "script", str(scripts / "setup-report.py")]
            if args.owned_tex:
                before = json.loads(command("tex-status-before", [*setup_script, "--status", *tex_args]))
                assert before["tex"]["state"] == "not_started"
                assert not Path(before["tex"]["directory"]).exists()
            profile = json.loads(command("report-setup", [*launcher, "script", str(scripts / "setup-report.py"), "--output-dir", str(root / "report-profile"), *tex_args]))
            result["checks"]["report_setup"] = profile
            if args.owned_tex:
                after = json.loads(command("tex-status-after", [*setup_script, "--status", *tex_args, "--output-dir", str(root / "report-profile")]))
                assert after["tex"]["state"] == "succeeded" and after["profile"]["present"]
                attempt = after["tex"]["current"]["attempt_id"]
                repeated = json.loads(command("tex-retry-ready", [*setup_script, "--retry", *tex_args, "--output-dir", str(root / "report-profile")]))
                assert repeated["tex"]["reused"] and repeated["status"] == "ready"
                final = json.loads(command("tex-status-repeat", [*setup_script, "--status", *tex_args]))
                assert final["tex"]["current"]["attempt_id"] == attempt
                result["checks"]["tex_operation"] = {"executed": True, "passed": True, "readonly_before": before, "after": after, "retry_reused": True}
            result["checks"]["report"] = json.loads(command("report-verify", [*launcher, "script", str(scripts / "verify-report.py"), "--project-dir", str(root / "report-proof"), "--environment", profile["environment"]]))
            command("report-pages", ["pdftoppm", "-png", "-scale-to", "1600", str(root / "report-proof/report-verification.pdf"), str(root / "report-proof/page")])
            delivery = json.loads(command("report-cli", [*launcher, "script", str(scripts / "render-report.py"),
                "--project-dir", str(root / "report-proof"), "--input", str(root / "report-proof/.arc/report-verification.md"),
                "--output", str(root / "report-proof/report-cli.pdf"), "--environment", profile["environment"]]))
            assert delivery["delivery_status"] == "published" and delivery["artifacts"]
            result["checks"]["report_cli"] = delivery
        if args.network_paper:
            network = root / "network-probe.py"
            network.write_text(NETWORK)
            result["checks"]["network"] = json.loads(command("network-probe", [*launcher, "script", str(network), str(root), args.network_paper]))
        result["passed"] = True
    except Exception as exc:
        result.update(passed=False, error_type=type(exc).__name__, message=str(exc))
    (root / "verification.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"passed": result["passed"], "evidence": str(root / "verification.json"), "message": result.get("message")}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
