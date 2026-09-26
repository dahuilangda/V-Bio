"""ESM3 HTTP service — runs on the HOST (Boltz2Score venv, CUDA).

Loads the esm3_sm_open_v1 checkpoint once, serves
propose/refill/perplexity/learn over HTTP. Workers reach it at
http://<host>:9333 regardless of their container.
"""
from __future__ import annotations

import json
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

PEPLM_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PEPLM_ROOT))

import torch

from peplm.integrate.ss_modes import normalize_mode, resolve_ss_profile
from peplm.models.esm3_policy import ESM3Policy

policy = ESM3Policy(
    checkpoint=PEPLM_ROOT / "models/external/esm3_sm_open_v1.pth",
    device="cuda:0",
    dtype=torch.bfloat16,
)
print("[esm3-http] model loaded", flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 10 * 1024 * 1024)
            req = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError) as exc:
            body = json.dumps({"ok": False, "error": f"bad request: {exc}"}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
            return
        mode = req.get("mode", "")
        try:
            if mode == "propose":
                resp = self._propose(req)
            elif mode == "refill":
                resp = self._refill(req)
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

    def _conditioning(self, req):
        """Sampling-time conditioning, reused by every mode so the
        policy family never shifts between propose and learn.

        structure_mode (auto/helix/hairpin/...) is resolved to an ss8
        template AT EACH CALL'S ACTUAL peptide length — a hairpin turn
        must sit mid-chain, so one static string would misalign when
        lengths vary. A raw ss_profile string still overrides (legacy
        ESM3_SS_PROFILE callers)."""
        mode = req.get("structure_mode")
        cond = {
            "structure_mode": normalize_mode(mode) if mode else None,
            "ss_profile": req.get("ss_profile") or None,
            "peptide_first": bool(req.get("peptide_first", False)),
        }
        return cond

    @staticmethod
    def _ss_profile_for(cond, n: int):
        """Exact-length ss8 template: explicit legacy profile wins, else
        the mode's builder; None leaves the ss8 track off."""
        return resolve_ss_profile(cond.get("structure_mode"),
                                  cond.get("ss_profile"), n)

    def _propose(self, req):
        import math
        from collections import Counter
        receptor = req["receptor_sequence"]
        pep_len = int(req.get("peptide_length", 14))
        n = int(req.get("n_samples", 48))
        keep = int(req.get("n_keep", 16))
        cond = self._conditioning(req)
        # Chunk the sampling: a long receptor context with a full
        # parallel batch exhausts GPU memory. Process in chunks and
        # concatenate trajectories before filtering.
        CHUNK = 8
        all_trajs = []
        with torch.no_grad():
            remaining = n
            while remaining > 0:
                batch = min(CHUNK, remaining)
                trajs = policy.sample_batch(
                    receptor=receptor, pep_len=pep_len, n=batch,
                    keep=batch,
                    temperature=float(req.get("temperature", 0.9)),
                    num_steps=8, strategy="entropy",
                    ss_profile=self._ss_profile_for(cond, pep_len),
                    peptide_first=cond["peptide_first"])
                all_trajs.extend(trajs)
                remaining -= batch
                if remaining > 0:
                    torch.cuda.empty_cache()
        trajs = all_trajs
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

    def _refill(self, req):
        """Resample only the specified positions of a parent sequence.

        Uses ESM3Policy.refill: confident positions stay as context,
        only the remasked positions are sampled. Returns the mutated
        sequence or None if refill produced no change."""
        parent = req.get("parent_sequence", "")
        positions = req.get("remask_positions", [])
        receptor = req.get("receptor_sequence", "")
        temperature = float(req.get("temperature", 0.7))
        cond = self._conditioning(req)
        if not parent or not positions:
            return {"error": "parent_sequence and remask_positions required"}

        parent_tokens = policy.encode_peptide(parent, len(parent))
        if parent_tokens is None:
            return {"error": "failed to tokenize parent"}

        with torch.no_grad():
            traj = policy.refill(
                receptor=receptor,
                parent_tokens=parent_tokens,
                remask_positions=[int(p) for p in positions],
                temperature=temperature,
                ss_profile=self._ss_profile_for(cond, len(parent)),
                peptide_first=cond["peptide_first"],
            )
        if traj is None:
            return {"sequence": None, "reason": "no change"}

        seq = policy.decode_tokens(traj.peptide_tokens)
        return {"sequence": seq if seq else None}

    def _perplexity(self, req):
        seq = req.get("peptide_sequence", "")
        score = policy.score_sequence(
            req.get("receptor_sequence", ""),
            seq,
            ss_profile=self._ss_profile_for(
                self._conditioning(req), len(seq)),
            peptide_first=bool(req.get("peptide_first", False)))
        return {"perplexity": float(-score) if score is not None else 10.0}

    def _learn(self, req):
        """Sequence-level policy gradient: score each peptide's tokens
        under the current policy, apply the group-mean advantage as a
        scalar reward, and take one gradient step. This avoids the full
        MaskGIT trajectory recording that Rollout requires — the HTTP
        boundary carries sequences and rewards, not per-step states."""
        data = req.get("rollouts", [])
        cond = self._conditioning(req)
        groups: dict[str, list] = {}
        for rd in data:
            groups.setdefault(rd.get("group", "default"), []).append(rd)
        valid = {g: r for g, r in groups.items() if len(r) >= 2}
        if not valid:
            return {"skipped": "no group with >= 2 members"}

        import torch.nn.functional as Fn
        updates = []
        for g, rows in valid.items():
            rewards = [r["reward"] for r in rows]
            m = sum(rewards) / len(rewards)
            sd = max(max(rewards) - m, m - min(rewards), 1e-6)
            for r in rows:
                # each rollout trains at ITS OWN length: mutation children
                # keep their parent's length and a padded-to-pep_len row
                # would misalign a length-dependent ss8 template
                n = len(r.get("sequence") or "")
                if n < 4:
                    continue
                tokens = policy.encode_peptide(r["sequence"], n)
                if tokens:
                    adv = (r["reward"] - m) / sd
                    # Per-residue credit: positions with low pLDDT get
                    # higher gradient weight (BindCraft2-style — the model
                    # learns to change what is broken, not what works)
                    plddts = r.get("plddts") or []
                    if plddts and len(plddts) == n:
                        credit = [max(1.0 - float(p), 0.05) for p in plddts]
                        csum = sum(credit) or 1.0
                        credit = [c / csum * n for c in credit]
                    else:
                        credit = [1.0] * n
                    updates.append((tokens, adv, credit))

        if len(updates) < 2:
            return {"skipped": "tokenization failed"}

        # One gradient step over all (tokens, advantage) pairs
        opt = torch.optim.AdamW(
            [p for p in policy.model.parameters() if p.requires_grad],
            lr=1e-5)
        opt.zero_grad()
        total_loss = torch.tensor(0.0, device=policy.device)
        rec_seq = req.get("receptor_sequence", "")
        ctx_cache: dict[int, tuple] = {}
        for tokens, adv, credit in updates:
            n = len(tokens)
            if n not in ctx_cache:
                # context + per-length ss8: hairpin turns sit mid-chain,
                # so each length needs its own template
                ss8_n = self._ss_profile_for(cond, n)
                ctx_cache[n] = (
                    policy.encode_context(rec_seq, n,
                                          peptide_first=cond["peptide_first"]),
                    policy.peptide_start(rec_seq, n,
                                         peptide_first=cond["peptide_first"]),
                    policy.encode_ss8(rec_seq, n, ss8_n,
                                      peptide_first=cond["peptide_first"])
                    if ss8_n else None,
                )
            ctx, pep_start, ss8 = ctx_cache[n]
            toks = ctx.clone()
            for i, tid in enumerate(tokens):
                toks[0, pep_start + i] = tid
            logits = policy._forward_sequence_logits(toks, ss8)
            lp = Fn.log_softmax(
                logits[0, pep_start:pep_start + n, :], dim=-1)
            tok_tensor = torch.tensor(tokens, device=policy.device)
            scores = lp.gather(1, tok_tensor.unsqueeze(1)).squeeze(1)
            # Per-residue weighted REINFORCE: each token's logprob is
            # weighted by its credit (low pLDDT -> high weight)
            credit_t = torch.tensor(credit, device=policy.device,
                                     dtype=scores.dtype)
            weighted = (scores * credit_t).sum() / credit_t.sum()
            total_loss = total_loss - adv * weighted
        total_loss.backward()
        torch.nn.utils.clip_grad_norm_(
            [p for p in policy.model.parameters() if p.requires_grad], 1.0)
        opt.step()

        adapter = req.get("new_adapter_dir")
        if adapter:
            Path(adapter).mkdir(parents=True, exist_ok=True)
            policy.save_adapter(Path(adapter))
        return {
            "adapter_dir": adapter,
            "n_updates": len(updates),
            "loss": float(total_loss.detach()),
        }

    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({"ok": True, "model": "esm3", "device": str(policy.device)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        import sys as _s
        print(f"[esm3-http] {self.address_string()} {fmt % args}", file=_s.stderr, flush=True)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9333
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"[esm3-http] listening on :{port}", flush=True)
    server.serve_forever()
