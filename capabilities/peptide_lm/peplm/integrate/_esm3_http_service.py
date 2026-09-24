"""ESM3 HTTP service — runs on the HOST (Boltz2Score venv, CUDA).

Loads the 3B model once, serves propose/perplexity/learn over HTTP.
Workers reach it at http://<host>:9333 regardless of their container.
"""
from __future__ import annotations

import json
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

PEPLM_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PEPLM_ROOT))

import torch

from peplm.models.esm3_policy import ESM3Policy

policy = ESM3Policy(
    checkpoint=PEPLM_ROOT / "models/external/esm3_sm_open_v1.pth",
    device="cuda:0",
    dtype=torch.bfloat16,
)
print("[esm3-http] model loaded", flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length))
        mode = req.get("mode", "")
        try:
            if mode == "propose":
                resp = self._propose(req)
            elif mode == "perplexity":
                resp = self._perplexity(req)
            elif mode == "learn":
                resp = self._learn(req)
            else:
                resp = {"error": f"unknown mode: {mode}"}
            resp["ok"] = True
        except Exception as exc:
            resp = {"ok": False, "error": str(exc)}
        body = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _propose(self, req):
        import math
        from collections import Counter
        receptor = req["receptor_sequence"]
        pep_len = int(req.get("peptide_length", 14))
        n = int(req.get("n_samples", 48))
        keep = int(req.get("n_keep", 16))
        with torch.no_grad():
            trajs = policy.sample_batch(
                receptor=receptor, pep_len=pep_len, n=n, keep=keep,
                temperature=float(req.get("temperature", 0.9)),
                num_steps=8, strategy="entropy")
        seqs = []
        for t in trajs:
            s = policy.decode_tokens(t.peptide_tokens)
            if not s or len(s) != pep_len:
                continue
            freq = Counter(s)
            h = -sum((c / pep_len) * math.log2(c / pep_len) for c in freq.values())
            if h < 1.5:
                continue
            seqs.append({"sequence": s, "logprob": t.mean_logprob})
        seqs.sort(key=lambda x: -x["logprob"])
        return {"sequences": seqs[:keep]}

    def _perplexity(self, req):
        score = policy.score_sequence(
            req.get("receptor_sequence", ""),
            req.get("peptide_sequence", ""))
        return {"perplexity": float(-score) if score is not None else 10.0}

    def _learn(self, req):
        from peplm.generate.masked_grpo import MaskedGRPOUpdater, Rollout
        data = req.get("rollouts", [])
        pep_len = int(req.get("peptide_length", 14))
        groups = {}
        for rd in data:
            groups.setdefault(rd.get("group", "d"), []).append(rd)
        valid = {g: r for g, r in groups.items() if len(r) >= 2}
        if not valid:
            return {"skipped": "no valid groups"}
        rollouts = []
        for g, rows in valid.items():
            rewards = [r["reward"] for r in rows]
            m = sum(rewards) / len(rewards)
            sd = max(max(rewards) - m, m - min(rewards), 1e-6)
            for r in rows:
                tokens = policy.encode_peptide(r["sequence"], pep_len)
                if tokens:
                    rollouts.append(Rollout(
                        peptide_tokens=tokens,
                        advantage=(r["reward"] - m) / sd, group=g))
        if len(rollouts) < 2:
            return {"skipped": "tokenization failed"}
        updater = MaskedGRPOUpdater(policy, lr=1e-5, kl_beta=0.04)
        stats = updater.update(rollouts)
        adapter = req.get("new_adapter_dir")
        if adapter:
            Path(adapter).mkdir(parents=True, exist_ok=True)
            policy.save_adapter(Path(adapter))
        return {"adapter_dir": adapter, "stats": {k: float(v) if isinstance(v, (int, float)) else v for k, v in stats.items()}}

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9333
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"[esm3-http] listening on :{port}", flush=True)
    server.serve_forever()
