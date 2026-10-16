# Native affinity head for Protenix ("protenix2dock affinity").
#
# Design lineage (取其精华):
#   boltz2 AffinityModule — consumes the SAME trunk representations the
#     confidence head uses (s_inputs / z_trunk / predicted coordinates), so
#     affinity is read out of the model's own structural understanding; a
#     distance-distogram embedding injects explicit 3D evidence; interface-
#     focused pair attention (ligand-receptor + ligand-ligand pairs only);
#     dual regression/binary heads.
#   nesso-1 — prediction uncertainty via MC-dropout (mean/std over stochastic
#     passes), a two-path value ensemble, and input-condition robustness (the
#     training script randomises use_msa so the head generalises with or
#     without MSAs, mirroring nesso's MSA-free generalisation).
#
# Fusion (not a bolt-on pipeline stage): the head is invoked inside
# Protenix._main_inference_loop right after the confidence head and merges its
# per-sample outputs into pred_dict["summary_confidence"], which the stock
# dumper already writes — no external post-processing pass over output files.

from __future__ import annotations

from functools import partial
from typing import Any, Optional

import torch
import torch.nn as nn
import torch.nn.functional as F

from protenix.model.modules.primitives import Transition
from protenix.model.triangular.triangular import TriangleAttention
from protenix.model.modules.fused_ops import dropout_add_rowwise
from protenix.model.modules.pairformer import _chunked_transition
from protenix.model.utils import checkpoint_blocks


class _InterfacePairBlock(nn.Module):
    """Interface pair transformer block — reuses Protenix's own pairformer
    primitives (TriangleAttention + Transition) instead of hand-rolled MHA.

    TriangleAttention implements the same row-then-column pair attention our
    design needs, but its queries are chunked via protenix.utils.chunk_layer so
    attention weights are materialised chunk-by-chunk (chunk*heads*N^2), not as
    one [B, heads, N, N] tensor — the stock nn.MultiheadAttention slow path
    materialises the full tensor and OOMs at N ~ 1000. The transition's 4x
    intermediate is chunked the same way.

    The whole block runs under activation checkpointing (checkpoint_blocks,
    blocks_per_ckpt=1) during training: backward recomputes block forwards so
    peak memory stays bounded by a single block, not the full stack.
    """

    def __init__(
        self,
        c_z: int,
        num_heads: int = 4,
        c_hidden_pair_att: Optional[int] = None,
        dropout: float = 0.1,
    ):
        super().__init__()
        # Per-head hidden width = c_z / heads so the attention total width
        # matches the pair embedding (same convention as PairformerBlock).
        if c_hidden_pair_att is None:
            c_hidden_pair_att = c_z // num_heads
        self.starting_node_triangle_attention = TriangleAttention(
            c_in=c_z, c_hidden=c_hidden_pair_att, no_heads=num_heads,
            starting=True,
        )
        self.ending_node_triangle_attention = TriangleAttention(
            c_in=c_z, c_hidden=c_hidden_pair_att, no_heads=num_heads,
            starting=False,
        )
        self.p_drop = dropout
        self.transition = Transition(c_in=c_z, n=4)

    def forward(
        self,
        z: torch.Tensor,
        pair_mask: torch.Tensor,
        chunk_size: Optional[int] = None,
    ) -> torch.Tensor:
        # TriangleAttention computes `inf * (mask - 1)` biases, so the mask
        # must be float 0/1 (1 = attend). The diagonal keeps every row live:
        # a fully-masked row softmaxes to NaN otherwise.
        mask = pair_mask.float()
        row_update = self.starting_node_triangle_attention(
            z, mask=mask, chunk_size=chunk_size,
        )
        z = dropout_add_rowwise(z, row_update, self.p_drop, self.training)
        z = z.transpose(-2, -3).contiguous()
        col_update = self.ending_node_triangle_attention(
            z, mask=mask.transpose(-1, -2), chunk_size=chunk_size,
        )
        z = dropout_add_rowwise(z, col_update, self.p_drop, self.training)
        z = z.transpose(-2, -3).contiguous()
        z = z + _chunked_transition(self.transition, z, chunk_size)
        return z


class ProtenixAffinityHead(nn.Module):
    """Native affinity head fused into the Protenix forward pass.

    forward(s_inputs, z_trunk, x_pred, atom_to_token_idx, atom_is_ligand,
            token_pad_mask) -> list[per-sample dict] with
    affinity_pred_value / value1 / value2 / probability_binary /
    affinity_pred_score and (MC mode) affinity_pred_std.
    """

    def __init__(
        self,
        c_s: int = 384,
        c_z: int = 128,
        num_blocks: int = 2,
        num_heads: int = 4,
        num_dist_bins: int = 64,
        max_dist: float = 22.0,
        dropout: float = 0.1,
        mc_samples: int = 4,
        pocket_cond: bool = False,
        ipsae_pool_v3: bool = False,
        full_dist_cond: bool = False,
        pae_aux: bool = False,
    ):
        super().__init__()
        self.num_dist_bins = num_dist_bins
        self.max_dist = max_dist
        self.mc_samples = max(1, int(mc_samples))
        self.dropout_p = float(dropout)

        boundaries = torch.linspace(2.0, max_dist, num_dist_bins - 1)
        self.register_buffer("boundaries", boundaries)
        self.dist_bin_pairwise_embed = nn.Embedding(num_dist_bins, c_z)

        self.s_to_z_1 = nn.Linear(c_s, c_z, bias=False)
        self.s_to_z_2 = nn.Linear(c_s, c_z, bias=False)
        self.z_norm = nn.LayerNorm(c_z)
        self.z_linear = nn.Linear(c_z, c_z, bias=False)

        self.blocks = nn.ModuleList(
            [
                _InterfacePairBlock(c_z=c_z, num_heads=num_heads, dropout=dropout)
                for _ in range(num_blocks)
            ]
        )

        # Pocket-conditioned readout: the value head also sees mean-pooled
        # ligand and receptor single representations, so it can separate
        # "big ligand" bulk effects from interface quality and calibrate to
        # the pocket (between-pocket variance is ~36% of label variance).
        self.pocket_cond = pocket_cond
        in_dim = c_z + (2 * c_s if pocket_cond else 0)
        # Full-token distogram conditioning: embed the trunk's expected
        # distance map on ALL pair cells, not just the lig×rec interface.
        # Interface-only embeddings leave the blocks without a non-interface
        # reference — they cannot represent "globally correct fold, wrong
        # ligand placement" (audit 2026-10-06; ConfidenceHead conditions every
        # cell this way). Reuses dist_bin_pairwise_embed: zero new parameters,
        # warm-start compatible with existing checkpoints.
        self.full_dist_cond = full_dist_cond
        # Per-pair PAE auxiliary head (v7): the blocks have only ever seen a
        # pooled scalar gradient; a per-interface-cell regression onto the
        # frozen confidence head's expected PAE gives them pair-level
        # supervision (the research audit's root-cause fix). Bias starts at
        # the trunk's global mean PAE so the aux term is small at step 0.
        self.pae_aux = pae_aux
        if self.pae_aux:
            self.pae_aux_head = nn.Linear(c_z, 1)
            nn.init.zeros_(self.pae_aux_head.weight)
            with torch.no_grad():
                self.pae_aux_head.bias.fill_(20.0)
        self.ipsae_pool_v3 = ipsae_pool_v3
        if self.ipsae_pool_v3:
            self.log_sigma_dist = nn.Parameter(torch.zeros(1))
            self.log_T_anchor = nn.Parameter(torch.zeros(1))
            self.gate_mlp_v3 = nn.Sequential(
                nn.Linear(c_z + 1, 32), nn.GELU(), nn.Linear(32, 1),
            )
            nn.init.zeros_(self.gate_mlp_v3[-1].weight)
            nn.init.zeros_(self.gate_mlp_v3[-1].bias)
            in_dim = in_dim + c_z
        self.out_mlp = nn.Sequential(
            nn.Linear(in_dim, c_z), nn.GELU(), nn.Dropout(dropout),
            nn.Linear(c_z, c_s), nn.GELU(),
        )
        self.to_value_a = self._scalar_head(c_s)
        self.to_value_b = self._scalar_head(c_s)
        self.to_score = self._scalar_head(c_s)
        self.to_binary = nn.Linear(1, 1)

    @staticmethod
    def _scalar_head(c: int) -> nn.Sequential:
        return nn.Sequential(
            nn.Linear(c, c), nn.GELU(),
            nn.Linear(c, c), nn.GELU(),
            nn.Linear(c, 1),
        )

    def _token_masks(
        self,
        atom_to_token_idx: torch.Tensor,
        atom_is_ligand: torch.Tensor,
        n_token: int,
        token_pad_mask: Optional[torch.Tensor] = None,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        device = atom_to_token_idx.device
        if token_pad_mask is None:
            pad = torch.ones(n_token, dtype=torch.bool, device=device)
        else:
            pad = token_pad_mask[0] if token_pad_mask.dim() > 1 else token_pad_mask
            pad = pad.to(torch.bool)
        lig_atom = atom_is_ligand.to(torch.bool)
        lig_tokens = torch.zeros(n_token, dtype=torch.bool, device=device)
        if lig_atom.any():
            lig_tokens.index_add_(
                0,
                atom_to_token_idx[lig_atom].long(),
                torch.ones(int(lig_atom.sum()), dtype=torch.bool, device=device),
            )
        rec_tokens = pad & ~lig_tokens
        return rec_tokens, lig_tokens

    def _interface_pair_mask(self, rec, lig, token_pad_mask=None) -> torch.Tensor:
        pad = rec.new_ones(rec.shape)  # rec already carries pad semantics
        pair = (
            lig[:, None] * rec[None, :]
            + rec[:, None] * lig[None, :]
            + lig[:, None] * lig[None, :]
        )
        pair = (pair > 0) & pad[:, None] & pad[None, :]
        # Keep the diagonal live: TriangleAttention softmaxes each row
        # independently and a fully-masked row (no interface contact, no
        # self-pair) would produce NaN. Protenix's own pair masks always
        # include the diagonal for the same reason.
        return pair | torch.eye(lig.shape[0], dtype=torch.bool, device=lig.device)

    def _pair_min_distances(
        self,
        x_single: torch.Tensor,
        atom_to_token_idx: torch.Tensor,
        atom_is_ligand: torch.Tensor,
        lt_u: torch.Tensor,
        rt_u: torch.Tensor,
        lt_inv: torch.Tensor,
        rt_inv: torch.Tensor,
    ) -> torch.Tensor:
        """Minimal atom distance per (lig token, rec token) pair.

        Distances beyond the last boundary clamp into the furthest bin; token
        pairs with no direct atoms report max_dist*2 (farthest bin).
        """
        lig_atom = atom_is_ligand.to(torch.bool)
        d = torch.cdist(x_single[lig_atom].float(), x_single[~lig_atom].float())
        out = torch.full(
            (len(lt_u), len(rt_u)), self.max_dist * 2.0,
            device=x_single.device, dtype=d.dtype,
        )
        flat = out.reshape(-1)
        idx = (lt_inv.unsqueeze(1) * len(rt_u) + rt_inv.unsqueeze(0)).reshape(-1)
        flat.index_reduce_(0, idx, d.reshape(-1), "amin", include_self=True)
        return flat.view(len(lt_u), len(rt_u))

    def _forward_single(
        self,
        s_inputs: torch.Tensor,
        z_trunk: torch.Tensor,
        dmin: torch.Tensor,
        lt_u: torch.Tensor,
        rt_u: torch.Tensor,
        rec: torch.Tensor,
        lig: torch.Tensor,
        pair_mask: torch.Tensor,
        mc_samples: int,
        expected_pae=None,
        contact_stats=None,
        return_tensors: bool = False,
        full_dist=None,
        true_pae=None,
    ) -> dict[str, Any]:
        return self._forward_single_impl(
            s_inputs, z_trunk, dmin, lt_u, rt_u, rec, lig,
            pair_mask, mc_samples, expected_pae, contact_stats, return_tensors,
            full_dist, true_pae,
        )

    def _forward_single_impl(
        self,
        s_inputs: torch.Tensor,
        z_trunk: torch.Tensor,
        dmin: torch.Tensor,
        lt_u: torch.Tensor,
        rt_u: torch.Tensor,
        rec: torch.Tensor,
        lig: torch.Tensor,
        pair_mask: torch.Tensor,
        mc_samples: int,
        expected_pae=None,
        contact_stats=None,
        return_tensors: bool = False,
        full_dist=None,
        true_pae=None,
    ) -> dict[str, Any]:
        B, N = z_trunk.shape[:2]
        if B != 1:
            # The head is single-structure by construction: the caller loops
            # over x_pred samples against one shared trunk representation.
            raise ValueError(f"affinity head expects batch size 1, got {B}")

        z = self.z_linear(self.z_norm(z_trunk))
        # Broadcast the two s_to_z terms into one [1, N, 1, c] + [1, 1, N, c]
        # pair first, then a single add — one big intermediate instead of two.
        sz = (
            self.s_to_z_1(s_inputs)[:, :, None, :]
            + self.s_to_z_2(s_inputs)[:, None, :, :]
        )
        z = z + sz

        z0 = z[0].reshape(N * N, -1)
        # Full-token trunk-distogram embedding (trunk geometric belief on
        # every cell); the interface pose embedding below adds on top, so an
        # interface cell sees belief + realized pose distance.
        if self.full_dist_cond and full_dist is not None:
            fd = full_dist.to(z0.device)
            if fd.dim() == 2 and fd.shape[0] == N and fd.shape[1] == N:
                db_f = (fd.float().reshape(-1).unsqueeze(-1)
                        > self.boundaries).sum(-1).long()
                db_f = db_f.clamp(0, self.num_dist_bins - 1)
                z0 = z0 + self.dist_bin_pairwise_embed(db_f)

        # Distances come prepared per sample: crystal pose, diffusion sample,
        # or trunk distogram expected distances — one interface matrix.
        rows, cols = torch.meshgrid(
            torch.arange(len(lt_u), device=dmin.device),
            torch.arange(len(rt_u), device=dmin.device),
            indexing="ij",
        )
        db = (dmin.reshape(-1).unsqueeze(-1) > self.boundaries).sum(-1).long()
        db = db.clamp(0, self.num_dist_bins - 1)
        emb = self.dist_bin_pairwise_embed(db)
        flat_rows = lt_u[rows.reshape(-1)]
        flat_cols = rt_u[cols.reshape(-1)]
        z0 = z0.index_add(0, (flat_rows * N + flat_cols), emb)
        z0 = z0.index_add(0, (flat_cols * N + flat_rows), emb)
        z = z0.view(N, N, -1).unsqueeze(0)

        pm = pair_mask.unsqueeze(0)
        # Chunk attention along the query dim and checkpoint each block
        # (backward recomputes forwards) so peak memory stays bounded — same
        # mechanism as the trunk's pairformer. The old `N > 640` guard never
        # fired for max_seq_len<=600 training sets, leaving N~550 triangle
        # attention unchunked at ~20-25GB. Training mode now ALWAYS chunks +
        # checkpoints; inference keeps the size-based rule. Training chunks
        # at 16: the largest samples transiently peaked ~21GB at chunk 32
        # against a 23.5GB GPU (OOM crash-loop, 2026-10-07).
        chunk_size = 16 if (self.training or N > 640) else None
        blocks = [
            partial(block, pair_mask=pm, chunk_size=chunk_size)
            for block in self.blocks
        ]
        (z,) = checkpoint_blocks(
            blocks, args=(z,),
            blocks_per_ckpt=1 if (self.training or N > 640) else None,
        )

        pmf = pm.unsqueeze(-1).to(z.dtype)
        g = (z * pmf).sum(dim=(1, 2)) / (pmf.sum(dim=(1, 2)) + 1e-7)

        # Per-pair PAE auxiliary supervision (v7): regress the frozen
        # confidence head's expected PAE on the interface grid from the
        # post-block pair representation. This is the pair-level gradient
        # the blocks never had (scalar readout made per-pair structure
        # unidentifiable). true_pae arrives as the full [N, N] matrix or
        # pre-indexed [n_lt, n_rt] (same convention as expected_pae).
        pae_aux_loss = None
        if self.pae_aux and true_pae is not None:
            tp = true_pae.to(z.device, z.dtype)
            if tp.dim() == 2 and tp.shape[0] == N and tp.shape[1] == N:
                tgt = tp[lt_u][:, rt_u]
            else:
                tgt = tp
            pred = self.pae_aux_head(z[0][lt_u][:, rt_u]).squeeze(-1)
            pae_aux_loss = F.huber_loss(pred, tgt)

        # v3: IPSAE-kernel weighted pooling. Three audit fixes (2026-10-06):
        #   1. d0 follows ipSAE convention — receptor token count, not
        #      interface cell mass (cell mass made the ptm decay nearly flat).
        #   2. expected_pae arrives either as the full [N, N] matrix or
        #      pre-indexed to the interface grid; index once, before the
        #      kernel, so shapes never broadcast against [N, N].
        #   3. The kernel is scattered into an [n_lt, N] matrix so pooling
        #      reads z rows over all tokens with off-interface mass zero.
        if self.ipsae_pool_v3 and expected_pae is not None:
            n_rec = int(rec.sum().item())
            d0_conf = max(1.0, 1.24 * (n_rec - 15) ** (1 / 3) - 1.8) if n_rec > 15 else 1.0
            if expected_pae.dim() == 2 and expected_pae.shape[0] == N \
                    and expected_pae.shape[1] == N:
                pae_pair = expected_pae[lt_u][:, rt_u]
            else:
                pae_pair = expected_pae
            ptm_pae = 1.0 / (1.0 + (pae_pair.to(z.dtype) / d0_conf) ** 2)
            sigma = torch.exp(self.log_sigma_dist.clamp(-3, 3))
            dist_gate = torch.exp(-dmin ** 2 / (2 * sigma ** 2))
            K = z.new_zeros(len(lt_u), N)
            K.index_copy_(1, rt_u, ptm_pae * dist_gate)
            K = K * pm[0][lt_u].to(z.dtype)
            Ksum_l = K.sum(dim=1).clamp(min=1e-7).unsqueeze(-1)
            e_l = (K.unsqueeze(-1) * z[0][lt_u]).sum(dim=1) / Ksum_l
            k_mass = (K.sum(dim=1) / N).unsqueeze(-1)
            gate_in = torch.cat([e_l, k_mass], dim=-1)
            gate_w = torch.sigmoid(self.gate_mlp_v3(gate_in))
            s_l = e_l * gate_w
            T_a = torch.exp(self.log_T_anchor.clamp(-3, 3))
            s_scaled = s_l / T_a
            s_max = s_scaled.max(dim=0).values
            g_anchor = s_max + torch.log(torch.exp(s_scaled - s_max).sum(dim=0))
            g = torch.cat([g, g_anchor.unsqueeze(0)], dim=-1)
        if self.pocket_cond:
            s_flat = s_inputs[0].float()  # [N, c_s]
            n_rec = rec.sum().clamp(min=1)
            n_lig = lig.sum().clamp(min=1)
            rec_ctx = (s_flat * rec.unsqueeze(-1).float()).sum(0) / n_rec
            lig_ctx = (s_flat * lig.unsqueeze(-1).float()).sum(0) / n_lig
            g = torch.cat([g[0], lig_ctx, rec_ctx], dim=-1).unsqueeze(0)

        vals_a, vals_b, scores = [], [], []
        for _ in range(max(1, mc_samples)):
            h = F.dropout(g, p=self.dropout_p, training=self.training or mc_samples > 1)
            h = self.out_mlp(h)
            vals_a.append(self.to_value_a(h).reshape(-1)[0])
            vals_b.append(self.to_value_b(h).reshape(-1)[0])
            scores.append(self.to_score(h).reshape(-1)[0])
        va = torch.stack(vals_a)
        vb = torch.stack(vals_b)
        sc = torch.stack(scores)
        logit = self.to_binary(sc.mean().reshape(1, 1)).reshape(-1)[0]
        value = 0.5 * (va + vb).mean()
        # One code path for training and inference: return_tensors keeps the
        # autograd graph (the trainer in train_affinity.py reads *_t keys);
        # otherwise values are detached Python floats for the dumper.
        entry: dict[str, Any] = {
            "affinity_pred_value_t": value,
            "affinity_logits_binary_t": logit,
            "affinity_probability_binary_t": torch.sigmoid(logit),
        }
        if pae_aux_loss is not None:
            entry["pae_aux_loss_t"] = pae_aux_loss
        if not return_tensors:
            entry = {
                "affinity_pred_value": value.item(),
                "affinity_pred_value1": va.mean().item(),
                "affinity_pred_value2": vb.mean().item(),
                "affinity_pred_score": sc.mean().item(),
                "affinity_logits_binary": logit.item(),
                "affinity_probability_binary": torch.sigmoid(logit).item(),
            }
            if mc_samples > 1:
                entry["affinity_pred_std"] = torch.cat([va, vb]).std().item()
        return entry

    def forward(
        self,
        s_inputs: torch.Tensor,
        z_trunk: torch.Tensor,
        x_pred: Optional[torch.Tensor] = None,
        atom_to_token_idx: Optional[torch.Tensor] = None,
        atom_is_ligand: Optional[torch.Tensor] = None,
        token_pad_mask: Optional[torch.Tensor] = None,
        mc_samples: Optional[int] = None,
        expected_dist: Optional[torch.Tensor] = None,
        expected_pae: Optional[torch.Tensor] = None,
        contact_stats: Optional[torch.Tensor] = None,
        plddt_lig: Optional[torch.Tensor] = None,
        return_tensors: bool = False,
        full_dist: Optional[torch.Tensor] = None,
        true_pae: Optional[torch.Tensor] = None,
    ) -> list[dict[str, Any]]:
        """x_pred path (inference, structure supervision) or expected_dist path.

        expected_dist: [N, N] trunk-distogram expected distances (Å) — the
        structure-free training channel (Nesso-1 style: the trunk's own
        predicted interface geometry, no pose required). May also be passed
        pre-indexed as [len(lt_u), len(rt_u)].
        """
        n_token = z_trunk.shape[1]
        if atom_to_token_idx is None or atom_is_ligand is None:
            raise ValueError("atom_to_token_idx and atom_is_ligand are required")
        rec, lig = self._token_masks(atom_to_token_idx, atom_is_ligand, n_token, token_pad_mask)
        if not lig.any():
            return [{} for _ in range(x_pred.shape[0] if x_pred is not None else 1)]
        # Inference passes trunk tensors without a batch dim (batch_shape=());
        # normalise to [1, N, c] / [1, N, N, c] so downstream indexing is uniform.
        if s_inputs.dim() == 2:
            s_inputs = s_inputs.unsqueeze(0)
        if z_trunk.dim() == 3:
            z_trunk = z_trunk.unsqueeze(0)
        # The trunk may run bf16 (PROTENIX_LOW_VRAM); the head always computes
        # in fp32 (index_add_/attention kernels need matching dtypes).
        s_inputs = s_inputs.float()
        z_trunk = z_trunk.float()
        pair_mask = self._interface_pair_mask(rec, lig, token_pad_mask)
        n_mc = mc_samples if mc_samples is not None else (self.mc_samples if not self.training else 1)

        lig_atom = atom_is_ligand.to(torch.bool)
        rec_atom = ~lig_atom
        lt = atom_to_token_idx[lig_atom].long()
        rt = atom_to_token_idx[rec_atom].long()
        lt_u, lt_inv = torch.unique(lt, return_inverse=True)
        rt_u, rt_inv = torch.unique(rt, return_inverse=True)

        results = []
        # The runner wraps forward in bf16 autocast; the head computes in
        # strict fp32 (mixed index_add_/embedding dtypes otherwise mismatch).
        with torch.autocast(device_type=z_trunk.device.type, enabled=False):
            if expected_dist is not None:
                # Trunk distogram expected distances (token granularity, N x N)
                # or a pre-indexed interface matrix — either way index out the
                # lig x rec grid matching _forward_single's lt_u/rt_u.
                ed = expected_dist.to(z_trunk.device, torch.float32)
                if ed.shape[0] == n_token:
                    grid = ed[lt_u][:, rt_u]
                else:
                    grid = ed
                results.append(
                    self._forward_single(
                        s_inputs, z_trunk, grid, lt_u, rt_u, rec, lig,
                        pair_mask, n_mc, expected_pae, contact_stats,
                        return_tensors, full_dist=ed, true_pae=true_pae,
                    )
                )
            else:
                for s_i in range(x_pred.shape[0]):
                    dmin = self._pair_min_distances(
                        x_pred[s_i], atom_to_token_idx, atom_is_ligand,
                        lt_u, rt_u, lt_inv, rt_inv,
                    )
                    results.append(
                        self._forward_single(
                            s_inputs, z_trunk, dmin, lt_u, rt_u, rec, lig,
                            pair_mask, n_mc, expected_pae, contact_stats,
                            return_tensors, full_dist=full_dist,
                            true_pae=true_pae,
                        )
                    )
        return results
