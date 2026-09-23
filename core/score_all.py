#!/usr/bin/env python3
"""Score every experiment that has both a sealed truth and a recorded diagnosis.

Runs as bbr-breaker, the only identity that can read sealed ground truth.
The analyst never sees this file's output before diagnosing.
"""
import json, glob, os, hashlib

D = "/var/lib/bbr/experiments"
rows = []

for s in sorted(glob.glob(f"{D}/sealed/*.truth.json")):
    eid = os.path.basename(s).replace(".truth.json", "")
    truth = json.load(open(s))
    dp = f"{D}/diagnoses/{eid}.diagnosis.json"
    if not os.path.exists(dp):
        continue
    diag = json.load(open(dp))

    actual = truth.get("fault", "?")
    # Diagnoses were written across two schema versions: some use
    # "hypotheses", others "ranked_hypotheses", and confidence sits either at
    # root level or inside the top hypothesis. Reading only one shape made
    # the scorer report "no confidence recorded" for half the experiments,
    # which quietly disabled the calibration check - the one column that
    # catches a confidently wrong diagnosis.
    hyps = diag.get("hypotheses") or diag.get("ranked_hypotheses") or [{}]
    top = hyps[0] if hyps else {}
    conf = diag.get("confidence")
    if conf is None:
        conf = top.get("confidence")
    text = json.dumps(diag).lower()

    # A diagnosis counts as correct when it identifies the mechanism, not
    # when it happens to echo the catalogue's label. Match on the signature
    # each fault actually produces.
    signature = {
        "db-unreachable":       ["scaled", "replica", "zero", "desired=0"],
        "network-policy-deny":  ["network policy", "policy", "blocked", "denied"],
        "bad-service-selector": ["selector"],
        "pod-kill":             ["deleted", "killed", "replaced", "recreat"],
        "bad-configmap":        ["configmap", "hostname", "dns", "db_url", "typo"],
        "cpu-stress":           ["cpu", "busy", "loop", "saturat", "throttl"],
    }.get(actual, [actual.replace("-", " ")])

    correct = any(k in text for k in signature)

    # Calibration: high confidence should mean high accuracy.
    if conf is None:
        cal = "?"
    elif correct and conf >= 0.9:   cal = "well-calibrated"
    elif correct and conf < 0.6:    cal = "under-confident"
    elif not correct and conf >= 0.9: cal = "OVER-CONFIDENT"
    else: cal = "acceptable"

    rows.append({
        # Named experiment_id, not experiment - the API's sync_incidents()
        # reads this exact key and threw a KeyError when it didn't match,
        # which made /api/refresh return 500 on every call including from
        # the site's own Refresh button.
        "experiment_id": eid,
        "actual_fault": actual,
        "confidence": conf,
        "correct": correct,
        "calibration": cal,
        "no_repair_proposed": "none" in str(diag.get("proposed_repair", "")).lower(),
    })

n = len(rows)
ok = sum(1 for r in rows if r["correct"])
print(f"SCORE: {ok}/{n} correct")
print()
for r in rows:
    mark = "OK " if r["correct"] else "MISS"
    nr = "  [no repair proposed]" if r["no_repair_proposed"] else ""
    print(f"  {mark} {r['experiment_id']}  {r['actual_fault']:22} "
          f"conf={r['confidence']}  {r['calibration']}{nr}")

over = [r for r in rows if r["calibration"] == "OVER-CONFIDENT"]
print()
print(f"  over-confident failures: {len(over)}  (these are the dangerous ones)")

os.makedirs(f"{D}/scores", exist_ok=True)
json.dump({"scored": n, "correct": ok, "results": rows},
          open(f"{D}/scores/scorecard.json", "w"), indent=2)
print(f"  written to {D}/scores/scorecard.json")
