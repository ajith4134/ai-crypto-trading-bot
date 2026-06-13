"""Smoke for the shared-latent challenger (ml/shared_latent.py) — assembles real modalities from a LIVE
decision + runs the SSL forward. Run in a bot container: python -m ml._smoke_shared_latent"""
from __future__ import annotations


def main() -> int:
    import json
    import numpy as np
    import torch
    import redis_client
    from ml import shared_latent as SL
    r = redis_client.get()
    fails: list[str] = []

    # 1. architecture instantiates; shapes are the declared fixed dims
    model = SL.SharedLatentChallenger()
    print(f"[1] SharedLatentChallenger: input_dim={SL.INPUT_DIM} latent_dim={model.latent_dim} "
          f"n_params={model.n_params()} (modules {SL.MODULES_DIM} + regime {SL.REGIME_DIM} + belief {SL.BELIEF_DIM})")
    if SL.MODULES_DIM + SL.REGIME_DIM + SL.BELIEF_DIM != SL.INPUT_DIM:
        fails.append("modality dims don't sum to INPUT_DIM")

    # 2. assemble REAL modalities from a live decision
    sym = r.zrevrange("scibrain:last_decisions", 0, 0)
    if not sym:
        print("[!] no live decisions — using a synthetic decision")
        dec = {"regime": "trending", "modules": [
            {"module": "koopman", "role": "direction", "direction": 0.6, "conviction": 0.7, "ok": True},
            {"module": "hmm_regime", "role": "context", "direction": 0.0, "conviction": 0.4, "ok": True}]}
    else:
        sym = sym[0].decode() if isinstance(sym[0], bytes) else sym[0]
        dec = json.loads(r.get(f"scibrain:{sym}:decision"))
    belief = None
    try:
        from signals.scibrain import workspace
        ws = workspace.build_workspace(r, symbol=(sym if isinstance(sym, str) else None), publish=False)
        belief = ws.get("belief")
    except Exception:
        pass
    mods = SL.assemble_modalities(dec, belief)
    if mods["modules"].shape[0] != SL.MODULES_DIM or mods["regime"].shape[0] != SL.REGIME_DIM \
       or mods["belief"].shape[0] != SL.BELIEF_DIM:
        fails.append(f"assembled modality shapes wrong: {[v.shape for v in mods.values()]}")
    if any(not np.all(np.isfinite(v)) for v in mods.values()):
        fails.append("assembled modalities contain NaN/inf")
    print(f"[2] assembled modalities from {'live '+str(sym) if isinstance(sym,str) else 'synthetic'}: "
          f"modules{mods['modules'].shape} regime{mods['regime'].shape} belief{mods['belief'].shape}")

    # 3. full SSL forward: latent (24), recon (== input dim), jepa_pred (== latent), no NaN
    model.eval()
    with torch.no_grad():
        out = model(mods)
    z, recon, jp, tgt = out["latent"], out["recon"], out["jepa_pred"], out["target_input"]
    if z.shape[-1] != SL.LATENT_DIM:
        fails.append(f"latent dim {z.shape} != {SL.LATENT_DIM}")
    if recon.shape[-1] != SL.INPUT_DIM:
        fails.append(f"recon dim {recon.shape} != input {SL.INPUT_DIM}")
    if jp.shape[-1] != SL.LATENT_DIM:
        fails.append(f"jepa_pred dim {jp.shape} != {SL.LATENT_DIM}")
    if tgt.shape[-1] != SL.INPUT_DIM:
        fails.append("target_input dim wrong")
    for name, t in (("latent", z), ("recon", recon), ("jepa_pred", jp)):
        if not torch.all(torch.isfinite(t)):
            fails.append(f"{name} has NaN/inf")
    print(f"[3] forward: latent{tuple(z.shape)} recon{tuple(recon.shape)} jepa_pred{tuple(jp.shape)} all finite")

    # 4. masked forward (MAE objective): masking zeroes inputs, output still finite + right shape
    mask = torch.ones(SL.INPUT_DIM)
    mask[:10] = 0.0
    with torch.no_grad():
        out_m = model(mods, mask=mask)
    if not torch.all(torch.isfinite(out_m["recon"])):
        fails.append("masked recon has NaN")
    if torch.allclose(out_m["latent"], z):
        fails.append("masking did not change the latent (mask ineffective)")
    print(f"[4] masked forward: latent differs={not torch.allclose(out_m['latent'], z)}, recon finite")

    # 5. build_latent convenience returns a bounded latent
    bl = SL.build_latent(dec, belief)
    if not bl.get("available") or bl.get("dim") != SL.LATENT_DIM or len(bl.get("latent", [])) != SL.LATENT_DIM:
        fails.append(f"build_latent wrong: {bl}")
    print(f"[5] build_latent: dim={bl.get('dim')} n_params={bl.get('n_params')} "
          f"latent[:4]={bl.get('latent', [])[:4]}")

    if fails:
        print("\nFAIL:")
        for f in fails:
            print("  -", f)
        return 1
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
