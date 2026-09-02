"""Online surrogate for the Boltz2Score oracle.

ECFP fingerprints -> ensemble MLP predicting (affinity_pic50, ipsae, ligand_plddt_mean)
with epistemic uncertainty used to decide which candidates deserve real
(expensive) structure-based scoring. Ensembles are retrained from a replay
buffer each round; a linear error-vs-sigma calibration is refit on holdouts.
"""

from __future__ import annotations

import numpy as np
import torch
import torch.nn as nn
from rdkit import Chem
from rdkit.Chem import rdFingerprintGenerator

TARGET_KEYS = ("affinity_pic50", "ipsae", "ligand_plddt_mean")


def smiles_to_fp(smiles: str, bits=2048, radius=2) -> np.ndarray | None:
    m = Chem.MolFromSmiles(smiles)
    if m is None:
        return None
    gen = rdFingerprintGenerator.GetMorganGenerator(radius=radius, fpSize=bits)
    return np.asarray(gen.GetFingerprintAsNumPy(m), dtype=np.float32)


class EnsembleMLP(nn.Module):
    def __init__(self, in_dim=2048, hidden=(1024, 512), out_dim=3, dropout=0.1):
        super().__init__()
        layers, d = [], in_dim
        for h in hidden:
            layers += [nn.Linear(d, h), nn.ReLU(), nn.Dropout(dropout)]
            d = h
        layers.append(nn.Linear(d, out_dim))
        self.net = nn.Sequential(*layers)

    def forward(self, x):
        return self.net(x)


class Surrogate:
    """Deep-ensemble surrogate with uncertainty + calibration."""

    def __init__(self, cfg=None, device=None):
        from halo.config import SurrogateConfig

        self.cfg = cfg or SurrogateConfig()
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.members: list[EnsembleMLP] = []
        self.buffer: list[dict] = []  # {"smiles", affinity_pic50, ipsae, ligand_plddt_mean}
        self.cal_a, self.cal_b = 1.0, 0.0  # |err| ~ a*sigma + b
        self._mean = np.zeros(len(TARGET_KEYS), dtype=np.float32)
        self._std = np.ones(len(TARGET_KEYS), dtype=np.float32)
        self._build()

    def _build(self):
        self.members = [
            EnsembleMLP(self.cfg.fp_bits, tuple(self.cfg.hidden), len(TARGET_KEYS), self.cfg.dropout).to(self.device)
            for _ in range(self.cfg.ensemble_size)
        ]

    # ---- data ---------------------------------------------------------------
    def add_observations(self, rows: list[dict]) -> None:
        for r in rows:
            if all(k in r and r[k] is not None and np.isfinite(r[k]) for k in TARGET_KEYS):
                self.buffer.append({k: float(r[k]) for k in TARGET_KEYS} | {"smiles": r.get("smiles")})
        # normalize stats
        if len(self.buffer) >= 8:
            arr = np.array([[b[k] for k in TARGET_KEYS] for b in self.buffer], dtype=np.float32)
            self._mean = arr.mean(0)
            self._std = np.clip(arr.std(0), 1e-3, None)

    @property
    def n_obs(self) -> int:
        return len(self.buffer)

    # ---- training -----------------------------------------------------------
    def fit(self, verbose=False) -> dict:
        n = len(self.buffer)
        if n < 12:
            return {"n": n, "trained": False}
        X = np.stack([smiles_to_fp(b["smiles"], self.cfg.fp_bits, self.cfg.fp_radius) for b in self.buffer])
        Y = np.array([[b[k] for k in TARGET_KEYS] for b in self.buffer], dtype=np.float32)
        Yn = (Y - self._mean) / self._std
        X = torch.from_numpy(X).to(self.device)
        Yn = torch.from_numpy(Yn).to(self.device)
        idx = np.arange(n)
        val_mask = torch.zeros(n, dtype=torch.bool)
        if n >= 30:
            val_idx = np.random.RandomState(0).choice(n, size=max(6, n // 6), replace=False)
            val_mask[val_idx] = True
        Xv, Yv = X[val_mask], Yn[val_mask]
        Xt, Yt = X[~val_mask], Yn[~val_mask]
        # fresh bagged members each refit keep the ensemble spread honest
        self._build()
        for m_i, model in enumerate(self.members):
            if len(Xt) == 0:
                Xt, Yt = X, Yn
            boot = torch.randint(0, len(Xt), (len(Xt),), device=self.device)
            Xb, Yb = Xt[boot], Yt[boot]
            opt = torch.optim.Adam(model.parameters(), lr=self.cfg.lr, weight_decay=self.cfg.weight_decay)
            loss_fn = nn.SmoothL1Loss()
            model.train()
            for _ in range(self.cfg.epochs):
                perm = torch.randperm(len(Xb), device=self.device)
                for i in range(0, len(Xb), self.cfg.batch_size):
                    sel = perm[i : i + self.cfg.batch_size]
                    pred = model(Xb[sel])
                    loss = loss_fn(pred, Yb[sel])
                    opt.zero_grad()
                    loss.backward()
                    opt.step()
            model.eval()
        # calibration: ensemble sigma vs |error| on val fold
        if len(Xv) >= 5:
            with torch.no_grad():
                preds = torch.stack([m(Xv) for m in self.members])  # (E, n, 3)
                mu = preds.mean(0)
                sig = preds.std(0)
                err = (mu - Yv).abs()
                s = sig.flatten().cpu().numpy()
                e = err.flatten().cpu().numpy()
                ok = s > 1e-6
                if ok.sum() >= 5:
                    a, b = np.polyfit(s[ok], e[ok], 1)
                    self.cal_a, self.cal_b = float(np.clip(a, 0.05, 5.0)), float(max(b, 0.0))
            with torch.no_grad():
                val_err = float((mu - Yv).abs().mean())
        else:
            val_err = float("nan")
        if verbose:
            print(f"[surrogate] n={n} val_mae(norm)={val_err:.3f} cal=({self.cal_a:.2f}*sig+{self.cal_b:.2f})")
        return {"n": n, "trained": True, "val_err": val_err, "cal_a": self.cal_a, "cal_b": self.cal_b}

    # ---- prediction ---------------------------------------------------------
    @torch.no_grad()
    def predict(self, smiles_list: list[str]) -> tuple[np.ndarray, np.ndarray]:
        """Returns (mu, sigma) arrays (N, 3) in raw units; sigma calibrated."""
        n = len(smiles_list)
        mu = np.tile(self._mean, (n, 1))
        sig = np.full((n, 3), 5.0, dtype=np.float32)  # untrained -> max uncertainty
        if self.n_obs < 12 or not self.members:
            return mu, sig
        fps = [smiles_to_fp(s, self.cfg.fp_bits, self.cfg.fp_radius) for s in smiles_list]
        keep = [i for i, f in enumerate(fps) if f is not None]
        if not keep:
            return mu, sig
        X = torch.from_numpy(np.stack([fps[i] for i in keep])).to(self.device)
        preds = torch.stack([m(X) for m in self.members])  # (E, n, 3)
        m = preds.mean(0).cpu().numpy() * self._std + self._mean
        s = preds.std(0).cpu().numpy() * self._std
        for row, i in enumerate(keep):
            mu[i] = m[row]
            sig[i] = np.clip(self.cal_a * s[row] + self.cal_b, 0.0, 5.0)
        return mu, sig

    def save(self, path):
        torch.save(
            {"members": [m.state_dict() for m in self.members], "buffer": self.buffer,
             "mean": self._mean, "std": self._std, "cal": (self.cal_a, self.cal_b)},
            str(path),
        )

    def load(self, path):
        d = torch.load(str(path), map_location=self.device, weights_only=False)
        for m, sd in zip(self.members, d["members"]):
            m.load_state_dict(sd)
            m.eval()
        self.buffer = d["buffer"]
        self._mean, self._std = d["mean"], d["std"]
        self.cal_a, self.cal_b = d["cal"]
