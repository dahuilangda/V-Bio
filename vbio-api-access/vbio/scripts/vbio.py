#!/usr/bin/env python3
"""Send Command Builder requests through the V-Bio API Access gateway."""

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlencode, urlsplit
import zipfile

from task_history import TERMINAL_STATES, TaskHistory, now, summarize_status

PREDICTION_FILES = {"yaml_file", "template_files"}
COMMON_FIELDS = {"backend", "priority", "task_name", "task_summary", "use_msa_server", "msa_mode"}
WORKFLOWS = {
    "prediction": ("/predict", PREDICTION_FILES, {"yaml_file"}, {"low_vram", "custom_ccd_molecules"}),
    "virtual_screening": ("/predict", PREDICTION_FILES | {"compounds_file"}, {"yaml_file"}, set()),
    "affinity": (
        "/api/boltz2score",
        {"protein_file", "ligand_file", "pocket_ligand"},
        {"protein_file"},
        {"mode", "compute_ipsae", "seed", "enable_affinity", "target_chain", "ligand_chain",
         "ligand_smiles_map", "ligand_smiles", "ligand_filename", "pocket_residues",
         "center_x", "center_y", "center_z", "size_x", "size_y", "size_z"},
    ),
}
PRIVATE_IPV4 = tuple(ipaddress.ip_network(block) for block in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))
CONNECTION_KEYS = ("VBIO_API_BASE", "VBIO_PROJECT_ID", "VBIO_API_TOKEN")


class GatewayError(ValueError):
    def __init__(self, status, message):
        super().__init__(f"Gateway returned HTTP {status}. {message}".rstrip())
        self.status = status


class NetworkError(ValueError):
    def __init__(self, code):
        super().__init__(f"Network request failed (curl {code}).")
        self.code = code


def load_submission(path, project_id):
    spec = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict) or set(spec) - {"workflow", "fields", "files"}:
        raise ValueError("Expected a manifest object with workflow, fields and files.")
    workflow = spec.get("workflow")
    if workflow == "peptide_design":
        raise ValueError("Peptide Design uses the V-Bio web workflow in this skill. Keep this plan as a draft.")
    if not isinstance(workflow, str) or workflow not in WORKFLOWS:
        raise ValueError("Select prediction, virtual_screening or affinity.")
    endpoint, file_keys, required_files, field_keys = WORKFLOWS[workflow]
    fields, files = spec.get("fields"), spec.get("files")
    if not isinstance(fields, dict) or not isinstance(files, dict):
        raise ValueError("fields and files must be objects.")
    unknown = (fields.keys() - (COMMON_FIELDS | field_keys)) | (files.keys() - file_keys)
    if unknown:
        raise ValueError(f"Unsupported manifest keys for {workflow}: {', '.join(sorted(unknown))}.")
    backend = fields.get("backend")
    if not isinstance(backend, str) or not backend.strip():
        raise ValueError("Set fields.backend to the backend selected in Command Builder.")
    missing = required_files - files.keys()
    if missing:
        raise ValueError("Add the required upload to files: " + ", ".join(sorted(missing)) + ".")
    if workflow == "affinity":
        if fields.get("mode") in ("score", "pose", "refine", "interface"):
            if "ligand_file" not in files:
                raise ValueError("Affinity scoring requires ligand_file.")
        elif fields.get("mode") == "dock":
            axes = {"center_x", "center_y", "center_z"}
            present = axes & fields.keys()
            if present and present != axes:
                raise ValueError("Provide all three pocket center coordinates.")
            methods = sum((bool(present), bool(fields.get("pocket_residues")), "pocket_ligand" in files))
            if not fields.get("ligand_smiles") or "ligand_file" in files or methods != 1:
                raise ValueError("Affinity dock requires ligand_smiles and exactly one pocket method.")
        else:
            raise ValueError("Affinity requires a mode selected in Command Builder.")

    fields = dict(fields)
    if project_id:
        fields["project_id"] = project_id
    if endpoint == "/predict":
        fields["workflow"] = workflow
    form = []
    for name, value in fields.items():
        if type(value) not in (str, int, float, bool):
            raise ValueError(f"{name} must be a string, number or boolean.")
        data = value if isinstance(value, str) else json.dumps(value, allow_nan=False)
        form.extend(["--form-string", f"{name}={data}"])
    uploads = {}
    for name, value in files.items():
        paths = value if name == "template_files" and isinstance(value, list) else [value]
        if not paths or any(not isinstance(item, str) or not item for item in paths):
            raise ValueError(f"{name} requires a file path.")
        uploads[name] = []
        for item in paths:
            source = (path.parent / item).resolve()
            if not source.is_file():
                raise ValueError(f"Upload file does not exist: {source}")
            escaped = str(source).replace("\\", "\\\\").replace('"', '\\"')
            form.extend(["--form", f'{name}=@"{escaped}"'])
            uploads[name].append(str(source))
    return endpoint, form, {
        "method": "POST", "route": endpoint, "fields": {**fields, "project_id": project_id},
        "files": uploads,
    }


def request(url, token, output, form, timeout=300):
    curl = shutil.which("curl")
    if not curl:
        raise ValueError("curl must be available on PATH.")
    # Put the token on stdin rather than in the command line or a file.
    config = "header = " + json.dumps(f"X-API-Token: {token}")
    result = subprocess.run(
        [curl, "-q", "--config", "-", "--silent", "--show-error", "--globoff",
         "--connect-timeout", "15", "--max-time", f"{max(0.001, timeout):.3f}", "--output", str(output),
         "--write-out", "%{http_code}", *form, "--url", url],
        input=config, text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise NetworkError(result.returncode)
    if not result.stdout.startswith("2"):
        message = ""
        try:
            with output.open(encoding="utf-8") as stream:
                payload = json.loads(stream.read(8192))
            if isinstance(payload, dict):
                message = payload.get("error") or payload.get("message") or ""
            if isinstance(message, str):
                message = " ".join(message.replace(token, "[redacted]").split())[:300]
            else:
                message = ""
        except (OSError, ValueError):
            pass
        raise GatewayError(result.stdout, message)


def preview_id(preview, base):
    uploads = {}
    for field, paths in preview["files"].items():
        uploads[field] = []
        for path in paths:
            checksum = hashlib.sha256()
            with Path(path).open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    checksum.update(chunk)
            uploads[field].append((path, checksum.hexdigest()))
    reviewed = {**preview, "files": uploads, "base": base}
    canonical = json.dumps(reviewed, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def validate_api_base(base):
    parts = urlsplit(base)
    if (parts.scheme not in {"https", "http"} or not parts.hostname or parts.username is not None
            or parts.query or parts.fragment or any(char.isspace() for char in base)):
        raise ValueError("Set VBIO_API_BASE to the full gateway base from Command Builder.")
    if parts.scheme == "http" and parts.hostname != "localhost":
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError:
            address = None
        if address is None or not (address.is_loopback or any(address in block for block in PRIVATE_IPV4)):
            raise ValueError("Use HTTPS for API Access gateways outside localhost or a private IPv4 network.")


def read_connection(from_stdin):
    if not from_stdin:
        return {key: os.environ.get(key, "").strip() for key in CONNECTION_KEYS}
    try:
        settings = json.loads(sys.stdin.read())
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("Expected a connection JSON object on stdin.") from None
    if not isinstance(settings, dict) or settings.keys() - set(CONNECTION_KEYS):
        raise ValueError("Connection JSON accepts only VBIO_API_BASE, VBIO_PROJECT_ID and VBIO_API_TOKEN.")
    if any(not isinstance(value, str) for value in settings.values()):
        raise ValueError("Connection values must be strings.")
    return {key: settings.get(key, "").strip() for key in CONNECTION_KEYS}


def read_json(url, token, form=(), timeout=300):
    with tempfile.TemporaryDirectory() as directory:
        body = Path(directory) / "response"
        request(url, token, body, form, timeout)
        try:
            return json.loads(body.read_text(encoding="utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("Gateway response is not JSON. Check the Command Builder API base.") from None


def task_url(base, project, task_id, kind):
    segment = quote(task_id, safe="")
    prefix = "status" if kind == "status" else "results"
    suffix = f"/{kind}" if kind in {"screening", "view"} else ""
    return f"{base}/{prefix}/{segment}{suffix}?{urlencode({'project_id': project})}"


def save_result(url, token, task_id, output, timeout=300):
    if output is not None:
        destination = output.expanduser().absolute()
    else:
        directory = Path.cwd() / "vbio-results"
        directory.mkdir(exist_ok=True)
        safe_id = quote(task_id, safe="-_").replace("%", "_")
        destination = directory / f"vbio-{safe_id}.zip"
    if destination.exists():
        raise ValueError("Output already exists. Choose another path.")
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        body = Path(temporary) / "response"
        request(url, token, body, (), timeout)
        if not zipfile.is_zipfile(body):
            raise ValueError("Gateway did not return a ZIP archive.")
        os.link(body, destination)
    return str(destination)


def read_compact_metrics(url, token):
    with tempfile.TemporaryDirectory() as temporary:
        body = Path(temporary) / "response"
        request(url, token, body, ())
        try:
            with zipfile.ZipFile(body) as archive:
                try:
                    entry = archive.getinfo("compact_metrics.json")
                except KeyError:
                    return None
                if entry.file_size > 16 * 1024 * 1024:
                    raise ValueError("Compact result metrics exceed the supported size.")
                data = json.loads(archive.read(entry))
        except (zipfile.BadZipFile, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Gateway returned an invalid result view.") from exc
        if not isinstance(data, dict):
            raise ValueError("Expected an object in compact result metrics.")
        return data


def remember(history, key, **fields):
    try:
        return history.save(*key, **fields)
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"V-Bio: Could not save the local task record: {error}", file=sys.stderr)
        base, project, task_id = key
        return {"api_base": base, "project_id": project, "task_id": task_id, **fields}


def status_for(history, key, token, timeout=300):
    response = read_json(task_url(*key, "status"), token, timeout=timeout)
    summary = summarize_status(response, key[2], token)
    receipt = remember(history, key, status_checked_at=now(), **summary)
    return response, receipt


def wait_for_result(history, key, token, output, interval, wait_seconds):
    deadline = time.monotonic() + wait_seconds
    last_state = None
    last_update = None
    last_error = None
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            if last_state != "SUCCESS":
                _, receipt = status_for(history, key, token, timeout=min(300, remaining))
                last_state = receipt["state"]
                update = tuple(receipt.get(field) for field in (
                    "state", "status_message", "progress_percent", "estimated_remaining_seconds",
                ))
                if update != last_update:
                    print(json.dumps(receipt, ensure_ascii=False), flush=True)
                    last_update = update
                if last_state in TERMINAL_STATES and last_state != "SUCCESS":
                    return 1
            if last_state == "SUCCESS" and (remaining := deadline - time.monotonic()) > 0:
                saved = receipt.get("result_path")
                if output is None and saved and zipfile.is_zipfile(saved):
                    return 0
                path = save_result(task_url(*key, "results"), token, key[2], output, min(300, remaining))
                result = remember(history, key, result_path=path, downloaded_at=now())
                print(json.dumps(result, ensure_ascii=False), flush=True)
                return 0
            last_error = None
        except GatewayError as error:
            if error.status not in {"408", "429", "502", "503", "504"} and (
                last_state != "SUCCESS" or error.status != "404"
            ):
                raise
            last_error = str(error)
        except NetworkError as error:
            if error.code not in {5, 6, 7, 18, 28, 52, 55, 56}:
                raise
            last_error = str(error)
        time.sleep(max(0, min(interval, deadline - time.monotonic())))
    outcome = {"task_id": key[2], "state": last_state, "wait": "ended"}
    if last_state == "SUCCESS":
        outcome["download"] = "archive_unavailable"
    if last_error:
        outcome["last_read_error"] = last_error
    print(json.dumps(outcome, ensure_ascii=False), flush=True)
    return 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-stdin", action="store_true",
                        help="Read connection settings as JSON from stdin instead of the environment.")
    commands = parser.add_subparsers(dest="operation", required=True)
    submit = commands.add_parser("submit", help="Preview or submit a Command Builder manifest.")
    submit.add_argument("manifest", type=Path)
    review = submit.add_mutually_exclusive_group()
    review.add_argument("--dry-run", action="store_true", help="Validate locally without a network request.")
    review.add_argument("--approved-preview", metavar="ID", help="Require an exact match to the reviewed preview.")
    commands.add_parser("project", help="Check the configured project and submission permission.")
    commands.add_parser("tasks", help="List task records saved in this workspace.")
    for name, help_text in (
        ("status", "Read the current task state."),
        ("results", "Download a completed result ZIP to a new file."),
        ("metrics", "Read compact metrics from the task result view."),
        ("screening", "Read Virtual Screening results as JSON."),
        ("wait", "Follow one task and download its ZIP when ready."),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("task_id")
        if name in ("results", "wait"):
            command.add_argument("output", type=Path, nargs="?",
                                 help="Defaults to vbio-results/vbio-<task_id>.zip in this workspace.")
        if name == "wait":
            command.add_argument("--interval", type=int, default=15, help="Seconds between checks.")
            command.add_argument("--timeout", type=int, default=900, help="Maximum seconds to wait.")
    args = parser.parse_args()
    may_have_submitted = False
    try:
        history = TaskHistory(Path.cwd())
        if args.operation == "tasks":
            print(json.dumps(history.list(), ensure_ascii=False))
            return 0
        settings = read_connection(args.connection_stdin)
        token = settings["VBIO_API_TOKEN"]
        if token.startswith("<") and token.endswith(">"):
            settings["VBIO_API_TOKEN"] = ""
        missing_settings = [key for key, value in settings.items() if not value]
        project = settings["VBIO_PROJECT_ID"]
        base = settings["VBIO_API_BASE"].rstrip("/")
        form = []
        if args.operation == "submit":
            route, form, preview = load_submission(args.manifest, project or None)
            if args.dry_run:
                preview["preview_id"] = preview_id(preview, base)
                preview["missing_environment"] = missing_settings
                print(json.dumps(preview, ensure_ascii=False, indent=2))
                return 0
        if missing_settings:
            raise ValueError("Set these Command Builder environment variables: " + ", ".join(missing_settings) + ".")
        token = settings["VBIO_API_TOKEN"]
        validate_api_base(base)
        if not token or not token.isascii() or any(not 33 <= ord(char) <= 126 for char in token):
            raise ValueError("Set a valid VBIO_API_TOKEN in the harness environment.")
        if args.operation == "project":
            query = urlencode({"project_id": project})
            print(json.dumps(read_json(f"{base}/project?{query}", token), ensure_ascii=False))
            return 0
        if args.operation == "submit" and args.approved_preview:
            if args.approved_preview != preview_id(preview, base):
                raise ValueError("The approved preview is stale. Preview the current plan and confirm it again.")
        if args.operation == "submit":
            may_have_submitted = True
            try:
                response = read_json(base + route, token, form)
            except GatewayError as error:
                if error.status.startswith(("3", "4")) and error.status != "408":
                    may_have_submitted = False
                raise
            task_id = response.get("task_id") if isinstance(response, dict) else None
            if not isinstance(task_id, str) or not task_id.strip():
                raise ValueError("Submission response lacks task_id.")
            may_have_submitted = False
            fields = preview["fields"]
            remember(history, (base, project, task_id), state="ACCEPTED", submitted_at=now(),
                     workflow=fields.get("workflow", "affinity"), backend=fields["backend"],
                     task_name=str(fields.get("task_name", "")).replace(token, "[redacted]"))
            print(json.dumps(response, ensure_ascii=False))
        else:
            key = (base, project, args.task_id)
            if args.operation == "wait":
                if args.interval < 1 or args.timeout < 1:
                    raise ValueError("Wait interval and timeout must be positive seconds.")
                return wait_for_result(history, key, token, args.output, args.interval, args.timeout)
            if args.operation == "metrics":
                metrics = read_compact_metrics(task_url(*key, "view"), token)
                print(json.dumps({"metrics": metrics}, ensure_ascii=False))
            elif args.operation == "results":
                path = save_result(task_url(*key, "results"), token, args.task_id, args.output)
                remember(history, key, result_path=path, downloaded_at=now())
                print(path)
            else:
                if args.operation == "status":
                    response, _ = status_for(history, key, token)
                else:
                    response = read_json(task_url(*key, "screening"), token)
                print(json.dumps(response, ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        print("V-Bio: Stopped waiting locally. Check the task again using the same ID.", file=sys.stderr)
        return 130
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"V-Bio: {error}", file=sys.stderr)
        if may_have_submitted:
            print("Check the project task list before retrying to avoid a duplicate submission.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
