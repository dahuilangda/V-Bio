#!/usr/bin/env python
"""Evaluate a ModernPrior checkpoint standalone (run as soon as prior.pt
appears): sampling termination, tag separation, FIM accuracy."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch

from peplm.models.train import load_prior
from peplm.props.descriptors import compute_props
from peplm.vocab import parse_tokens


def main(ckpt: str, device: str = "cuda:0"):
    model, vocab = load_prior(ckpt, device=device)
    ban = ["<sol_h>", "<sol_m>", "<sol_l>", "<syn_h>", "<syn_m>", "<syn_l>",
           "<liab_h>", "<liab_m>", "<liab_l>", "<dev_hi>", "<dev_md>",
           "<dev_lo>", "<cont>", "<mask>", "<pre>", "<suf>", "<mid>",
           "<lin>", "<cyc>", "<bicy>"] + [f"<L{5*k}>" for k in range(1, 10)]
    hi_prompt = ["<sol_h>", "<syn_h>", "<liab_h>", "<L15>", "<lin>"]
    lo_prompt = ["<sol_l>", "<syn_l>", "<liab_l>", "<L15>", "<lin>"]
    stats = {}
    for name, prompt in (("hi", hi_prompt), ("lo", lo_prompt)):
        outs = model.sample_with_prompt(prompt, 300, device, temperature=1.0,
                                        top_p=0.95, return_tokens=True,
                                        ban_tokens=ban)
        seqs = [[x for x in t if not x.startswith("<")] for t in outs if t]
        devs = [compute_props(s)["developability"] for s in seqs if s]
        stats[f"mean_dev_{name}"] = sum(devs) / max(len(devs), 1)
        stats[f"n_{name}"] = len(seqs)
        lens = [len(s) for s in seqs]
        stats[f"median_len_{name}"] = sorted(lens)[len(lens) // 2] if lens else 0
        stats[f"max_len_{name}"] = max(lens) if lens else 0
        stats[f"unique_{name}"] = len({"".join(s) for s in seqs})
    stats["tag_separation"] = stats["mean_dev_hi"] - stats["mean_dev_lo"]
    # FIM teacher-forced accuracy
    seqs = [l.split()[-1] for l in
            open("/data/V-Bio/capabilities/peptide_lm/runs/data/uniref_val.txt")
            .read().splitlines()[:400]]
    seqs = [s for s in seqs if "<pre>" not in s and len(parse_tokens(s)) >= 12][:100]
    tot = hit = 0
    for s in seqs:
        toks = parse_tokens(s)
        w = 6
        a = (len(toks) - w) // 2
        P, M, S = toks[:a], toks[a:a + w], toks[a + w:]
        prompt = (["<sol_h>", "<syn_h>", "<liab_h>",
                   f"<L{min(max((len(toks)//5)*5,5),45)}>", "<lin>",
                   "<pre>"] + P + ["<suf>"] + S + ["<mid>"])
        ids = torch.tensor([[vocab.bos] + vocab.encode_tokens(prompt + M)],
                           device=device)
        with torch.no_grad():
            lp = torch.log_softmax(
                model.gpt(ids[:, :-1]).logits.float(), -1)[0]
        for j in range(len(prompt), len(prompt) + len(M)):
            tot += 1
            if int(lp[j - 1].argmax()) == int(ids[0, j]):
                hit += 1
    stats["fim_token_acc"] = hit / max(tot, 1)
    print(json.dumps(stats, indent=1))
    Path(str(ckpt) + ".eval.json").write_text(json.dumps(stats, indent=1))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1
         else "/data/V-Bio/capabilities/peptide_lm/models/prior.pt")