"""Standard UCP computation (RFE Annexure 11): UCP = UUCP x TCF x ECF."""
from __future__ import annotations
from .agent import TCF_W, ECF_W


def compute(mapping: dict) -> dict:
    uaw_c = mapping["uaw"]
    uucw_c = mapping["uucw"]
    uaw = uaw_c["simple"] * 1 + uaw_c["average"] * 2 + uaw_c["complex"] * 3
    uucw = uucw_c["simple"] * 5 + uucw_c["average"] * 10 + uucw_c["complex"] * 15
    uucp = uaw + uucw

    tf = sum(r * w for r, w in zip(mapping["tcf"], TCF_W))
    tcf = 0.60 + 0.01 * tf
    ef = sum(r * w for r, w in zip(mapping["ecf"], ECF_W))
    ecf = 1.40 - 0.03 * ef

    ucp = uucp * tcf * ecf
    band = "Small" if ucp <= 200 else "Medium" if ucp <= 1000 else "Large"

    return {
        "uaw": uaw, "uucw": uucw, "uucp": uucp,
        "tfactor": round(tf, 2), "tcf": round(tcf, 4),
        "efactor": round(ef, 2), "ecf": round(ecf, 4),
        "ucp": round(ucp, 2), "band": band,
        "dm_band": mapping.get("dm_band", "-"),
    }
