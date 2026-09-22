"""Shared vocabulary and metric-resolution utilities."""
from __future__ import annotations

AA20 = frozenset("ACDEFGHIKLMNPQRSTVWY")


def placement_allows(rule: str, index: int, length: int) -> bool:
    """Whether an NCAA with the given placement rule may occupy `index`
    (0-based) in a peptide of `length` residues."""
    rule = rule.lower()
    if rule == "any":
        return True
    if rule == "n_term":
        return index == 0
    if rule == "c_term":
        return index == length - 1
    if rule == "terminal":
        return index == 0 or index == length - 1
    return True


def resolve_interface_metric(metrics: dict) -> float:
    """Primary interface score from a metrics dict.

    Priority: ipsae_dom > ligand_ipsae_max > pair_iptm > iptm.
    Returns 0.0 when no interface metric is present."""
    for key in ("ipsae_dom", "ligand_ipsae_max", "pair_iptm", "iptm"):
        v = metrics.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def banned_decode_tokens(vocab) -> set[int]:
    """Token ids that must never appear in a decoded peptide: special
    markers, SS tokens, length buckets — everything that is not a
    residue or a user-registered NCAA."""
    structural_prefixes = (
        "<dev_", "<mask>", "<pad>", "<bos>", "<eos>", "<unk>",
        "<cls>", "<s>", "</s>", "<h>", "<e>", "<l>", "<s_free>",
        "<cont>", "<pre>", "<suf>", "<mid>",
    )
    banned = set()
    for token, idx in vocab.stoi.items():
        if token.startswith(structural_prefixes):
            banned.add(idx)
        elif token.startswith("<L"):  # length buckets <L5>..<L120>
            banned.add(idx)
        elif token.startswith("<lin") or token.startswith("<cyc") or token.startswith("<bicy"):
            banned.add(idx)  # modality tokens are prompt-only
    return banned
