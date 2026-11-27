import io
import json
import os
import runpy
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

CLIENT = Path(__file__).resolve().parents[1] / "vbio" / "scripts" / "vbio.py"
TOKEN = "test-project-token"


class Gateway(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        content_type = self.headers["Content-Type"]
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\n\r\n".encode() + body
        )
        parts = [
            (part.get_param("name", header="content-disposition"), part.get_payload(decode=True))
            for part in message.iter_parts()
        ]
        self.server.requests.append((self.path, self.headers["X-API-Token"], parts))
        self.respond(*self.server.submit_response)

    def do_GET(self):
        self.server.requests.append((self.path, self.headers["X-API-Token"]))
        if "/project?" in self.path:
            self.respond(200, b'{"project_id":"project+A","workflow":"prediction","can_submit":true}')
        elif "/view?" in self.path:
            self.respond(200, self.server.archive)
        elif "/status/" in self.path:
            task_id = self.path.split("/status/")[1].split("?")[0]
            queue = getattr(self.server, "status_queue", [])
            state, info = queue.pop(0) if queue else ("SUCCESS", {})
            self.respond(200, json.dumps({"task_id": task_id, "state": state, "info": info}).encode())
        elif "/screening?" in self.path:
            self.respond(200, b'{"rows":[]}')
        elif "/results/job-1?" in self.path or "/results/job-2?" in self.path:
            if getattr(self.server, "archive_delay", 0):
                self.server.archive_delay -= 1
                self.respond(404, b'{"error":"Result not found"}')
            else:
                self.respond(200, self.server.archive)
        else:
            self.respond(404, b'{"error":"Result not found"}')

    def respond(self, status, body):
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class ClientTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        gateway = HTTPServer(("127.0.0.1", 0), Gateway)
        self.addCleanup(gateway.server_close)
        self.gateway = gateway
        gateway.requests = []
        gateway.submit_response = (202, b'{"task_id":"job-1"}')
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as result:
            result.writestr("sample.txt", "mock result")
        gateway.archive = archive.getvalue()
        thread = threading.Thread(target=gateway.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(gateway.shutdown)
        self.env = {
            **os.environ,
            "VBIO_API_BASE": f"http://127.0.0.1:{gateway.server_port}/vbio-api",
            "VBIO_PROJECT_ID": "project+A",
            "VBIO_API_TOKEN": TOKEN,
        }

    def cli(self, *args, code=0, stdin=None):
        result = subprocess.run(
            [sys.executable, str(CLIENT), *args], cwd=self.root, env=self.env,
            capture_output=True, text=True, timeout=10, input=stdin,
        )
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        return result

    def manifest(self, workflow="prediction", fields=None, files=None):
        if files is None:
            files = {"yaml_file": 'input ,; ".yaml'}
        for names in files.values():
            for name in names if isinstance(names, list) else [names]:
                (self.root / name).write_text(name, encoding="utf-8")
        (self.root / "job.json").write_text(json.dumps({
            "workflow": workflow, "fields": {"backend": "boltz", **(fields or {})}, "files": files,
        }), encoding="utf-8")

    def test_offline_draft_needs_no_credentials(self):
        self.manifest()
        for key in ("VBIO_API_BASE", "VBIO_PROJECT_ID", "VBIO_API_TOKEN"):
            self.env.pop(key)
        preview = json.loads(self.cli("submit", "job.json", "--dry-run").stdout)
        self.assertIsNone(preview["fields"]["project_id"])
        self.assertEqual(set(preview["missing_environment"]), {
            "VBIO_API_BASE", "VBIO_PROJECT_ID", "VBIO_API_TOKEN",
        })
        self.assertFalse(self.gateway.requests)
        error = self.cli("submit", "job.json", code=1).stderr
        self.assertIn("VBIO_API_TOKEN", error)
        self.assertNotIn("duplicate submission", error)

    def test_command_builder_multipart_routes(self):
        cases = [
            ("prediction", "/predict", {"task_name": "@literal"},
             {"yaml_file": 'input ,; ".yaml', "template_files": ["one.cif", "two.cif"]}),
            ("virtual_screening", "/predict", {},
             {"yaml_file": "screening.yaml", "compounds_file": "library.csv"}),
            ("affinity", "/api/boltz2score", {"mode": "score"},
             {"protein_file": "protein.pdb", "ligand_file": "ligand.sdf"}),
        ]
        for workflow, route, fields, files in cases:
            with self.subTest(workflow=workflow):
                self.manifest(workflow, fields, files)
                before = len(self.gateway.requests)
                preview = json.loads(self.cli("submit", "job.json", "--dry-run").stdout)
                self.assertEqual(preview["route"], route)
                self.assertEqual(len(self.gateway.requests), before)
                self.assertEqual(json.loads(self.cli("submit", "job.json").stdout)["task_id"], "job-1")
                path, token, parts = self.gateway.requests[-1]
                self.assertEqual((path, token), ("/vbio-api" + route, TOKEN))
                self.assertIn(("project_id", b"project+A"), parts)
                self.assertEqual(("workflow", workflow.encode()) in parts, workflow != "affinity")
                for key, values in files.items():
                    for value in values if isinstance(values, list) else [values]:
                        self.assertIn((key, value.encode()), parts)
                if workflow == "prediction":
                    self.assertIn(("task_name", b"@literal"), parts)

    def test_peptide_design_does_not_fall_back_to_prediction(self):
        self.manifest("peptide_design")
        error = self.cli("submit", "job.json", "--dry-run", code=1).stderr
        self.assertIn("V-Bio web workflow", error)
        self.assertFalse(self.gateway.requests)

    def test_rejection_and_uncertain_submission(self):
        self.manifest()
        self.gateway.submit_response = (403, json.dumps({"error": f"Invalid token {TOKEN}"}).encode())
        error = self.cli("submit", "job.json", code=1).stderr
        self.assertIn("HTTP 403. Invalid token [redacted]", error)
        self.assertNotIn("duplicate submission", error)
        self.gateway.submit_response = (504, b'{"error":"Upstream runtime request timed out"}')
        self.assertIn("duplicate submission", self.cli("submit", "job.json", code=1).stderr)
        self.gateway.submit_response = (202, b'{"accepted":true}')
        self.assertIn("duplicate submission", self.cli("submit", "job.json", code=1).stderr)
        self.assertEqual(len(self.gateway.requests), 3)

    def test_switching_drafts_requires_a_current_approval(self):
        self.manifest(fields={"task_name": "A"}, files={"yaml_file": "input.yaml"})
        (self.root / "job.json").rename(self.root / "a.json")
        upload = self.root / "input.yaml"
        original = b"# opaque Builder export\r\n"
        upload.write_bytes(original)
        approved = json.loads(self.cli("submit", "a.json", "--dry-run").stdout)["preview_id"]
        draft = json.loads((self.root / "a.json").read_text())
        draft["fields"]["task_name"] = "B"
        (self.root / "b.json").write_text(json.dumps(draft))
        self.assertIn("stale", self.cli("submit", "b.json", "--approved-preview", approved, code=1).stderr)
        upload.write_bytes(b"# edited input\n")
        self.assertIn("stale", self.cli("submit", "a.json", "--approved-preview", approved, code=1).stderr)
        upload.write_bytes(original)
        self.env["VBIO_PROJECT_ID"] = "another-project"
        self.assertIn("stale", self.cli("submit", "a.json", "--approved-preview", approved, code=1).stderr)
        self.assertFalse(self.gateway.requests)
        self.env["VBIO_PROJECT_ID"] = "project+A"
        self.cli("submit", "a.json", "--approved-preview", approved)
        self.assertIn(("yaml_file", original), self.gateway.requests[0][2])

    def test_project_check_is_read_only(self):
        project = json.loads(self.cli("project").stdout)
        self.assertEqual(project["workflow"], "prediction")
        self.assertTrue(project["can_submit"])
        self.assertEqual(self.gateway.requests, [
            ("/vbio-api/project?project_id=project%2BA", TOKEN)
        ])

    def test_metrics_reads_compact_view_without_saving_an_archive(self):
        self.assertEqual(json.loads(self.cli("metrics", "job-1").stdout), {"metrics": None})
        archive = io.BytesIO()
        metrics = {"interface_sc": 0.6, "pair_ipsae": [{"a_to_b": 0.7, "b_to_a": 0.5}]}
        with zipfile.ZipFile(archive, "w") as result:
            result.writestr("compact_metrics.json", json.dumps(metrics))
        self.gateway.archive = archive.getvalue()
        self.assertEqual(json.loads(self.cli("metrics", "job-1").stdout), {"metrics": metrics})
        self.assertEqual(self.gateway.requests[-1], (
            "/vbio-api/results/job-1/view?project_id=project%2BA", TOKEN
        ))
        self.assertFalse((self.root / "vbio-results").exists())

    def test_task_reads_and_zip(self):
        self.assertEqual(json.loads(self.cli("status", "job-1").stdout)["state"], "SUCCESS")
        self.assertEqual(self.gateway.requests[-1], ("/vbio-api/status/job-1?project_id=project%2BA", TOKEN))
        self.assertEqual(json.loads(self.cli("screening", "job-1").stdout), {"rows": []})
        self.cli("results", "job-1", "out.zip")
        self.assertEqual((self.root / "out.zip").read_bytes(), self.gateway.archive)
        self.cli("results", "job-1", "out.zip", code=1)
        self.assertIn("Result not found", self.cli("results", "unknown", "missing.zip", code=1).stderr)
        self.assertFalse((self.root / "missing.zip").exists())

    def test_connection_from_conversation_overrides_environment_as_a_unit(self):
        self.manifest()
        connection = {key: self.env.pop(key) for key in (
            "VBIO_API_BASE", "VBIO_PROJECT_ID", "VBIO_API_TOKEN",
        )}
        self.env["VBIO_PROJECT_ID"] = "other-project"
        placeholder = {**connection, "VBIO_API_TOKEN": "<YOUR_API_TOKEN>"}
        preview = self.cli("--connection-stdin", "submit", "job.json", "--dry-run",
                           stdin=json.dumps(placeholder))
        self.assertEqual(json.loads(preview.stdout)["missing_environment"], ["VBIO_API_TOKEN"])
        self.assertFalse(self.gateway.requests)
        self.cli("--connection-stdin", "submit", "job.json", stdin=json.dumps(connection))
        self.assertIn(("project_id", b"project+A"), self.gateway.requests[-1][2])
        self.cli("--connection-stdin", "results", "job-1", stdin=json.dumps(connection))
        self.assertTrue((self.root / "vbio-results" / "vbio-job-1.zip").is_file())

    def test_internal_gateway_http(self):
        with patch.object(sys, "path", [str(CLIENT.parent), *sys.path]):
            validate = runpy.run_path(str(CLIENT))["validate_api_base"]
        validate("http://172.17.3.200:5173/vbio-api")
        with self.assertRaisesRegex(ValueError, "Use HTTPS"):
            validate("http://example.org/vbio-api")

    def test_default_download_stays_in_client_workspace(self):
        destination = self.root / "vbio-results" / "vbio-job-1.zip"
        result = self.cli("results", "job-1")
        self.assertEqual(result.stdout.strip(), str(destination))
        self.assertEqual(destination.read_bytes(), self.gateway.archive)
        count = len(self.gateway.requests)
        self.cli("results", "job-1", code=1)
        self.assertEqual(len(self.gateway.requests), count)


    def test_two_tasks_keep_separate_status_and_download_paths(self):
        self.manifest(fields={"task_name": "Ubiquitin A"})
        self.cli("submit", "job.json")
        self.gateway.submit_response = (202, b'{"task_id":"job-2"}')
        self.manifest(fields={"task_name": "Ubiquitin B"})
        self.cli("submit", "job.json")
        self.gateway.status_queue = [
            ("PENDING", {"message": "Waiting in queue"}),
            ("PROGRESS", {"progress_percent": 30, "estimated_remaining_seconds": 12}),
            ("SUCCESS", {"completed_at": "2026-11-27T00:05:00Z"}),
        ]
        self.gateway.archive_delay = 1
        output = self.cli("wait", "job-1", "--interval", "1", "--timeout", "8").stdout
        events = [json.loads(line) for line in output.splitlines()]
        self.assertEqual([event["state"] for event in events], ["PENDING", "PROGRESS", "SUCCESS", "SUCCESS"])
        self.assertIsNone(events[0]["estimated_remaining_seconds"])
        self.assertEqual(events[1]["estimated_remaining_seconds"], 12)
        self.assertEqual(events[1]["progress_percent"], 30)
        self.assertNotIn("result_path", events[2])
        self.assertTrue(Path(events[3]["result_path"]).is_file())
        self.cli("wait", "job-2", "--interval", "1", "--timeout", "5")
        self.env.pop("VBIO_API_TOKEN")
        tasks = {row["task_id"]: row for row in json.loads(self.cli("tasks").stdout)}
        for i, name in ((1, "Ubiquitin A"), (2, "Ubiquitin B")):
            row = tasks[f"job-{i}"]
            self.assertEqual(row["task_name"], name)
            self.assertEqual(row["state"], "SUCCESS")
            self.assertEqual(Path(row["result_path"]).name, f"vbio-job-{i}.zip")
            self.assertTrue(row["result_file_exists"])
        self.assertNotIn("estimated_remaining_seconds", tasks["job-1"])
        self.assertEqual(sum(path.startswith("/vbio-api/results/job-1?") for path, *_ in self.gateway.requests), 2)
        self.env["VBIO_API_TOKEN"] = TOKEN
        self.cli("wait", "job-1", "--timeout", "5")
        self.assertEqual(sum(path.startswith("/vbio-api/results/job-1?") for path, *_ in self.gateway.requests), 2)

    def test_wait_timeout_and_terminal_failure_never_download_or_resubmit(self):
        self.gateway.status_queue = [
            ("PENDING", {}), ("FAILURE", {"exc_message": ["Invalid input", TOKEN]}),
        ]
        pending = [json.loads(line) for line in self.cli(
            "wait", "job-1", "--interval", "3", "--timeout", "1", code=2
        ).stdout.splitlines()]
        self.assertEqual(pending[-1], {"task_id": "job-1", "state": "PENDING", "wait": "ended"})
        failed = json.loads(self.cli("wait", "job-1", "--timeout", "5", code=1).stdout)
        self.assertEqual(failed["state"], "FAILURE")
        self.assertEqual(failed["status_message"], "Invalid input [redacted]")
        self.assertTrue(all(path.startswith("/vbio-api/status/") for path, *_ in self.gateway.requests))
        self.assertFalse((self.root / "vbio-results").exists())


if __name__ == "__main__":
    unittest.main()
