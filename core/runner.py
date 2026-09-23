#!/usr/bin/env python3
"""Build-Break-Recover experiment runner — the BREAKER.

This is deliberately a SEPARATE program from anything the analyst runs.

THE PROBLEM IT SOLVES
---------------------
The report requires that the analyst not know which fault was injected,
so that root-cause accuracy can be scored honestly. Stating that as a rule
is not enough: if one process both injects the fault and investigates it,
"don't look" is an instruction, not a boundary.

THE MECHANISM
-------------
1. The runner picks a fault (or is told one) and writes TWO files:
     - sealed/<id>.truth.json   mode 0600, owned by the breaker
     - open/<id>.incident.json  mode 0644, what the analyst may read
2. The incident file contains ONLY: experiment id, start time, and the
   fact that something was injected. No fault type, no target, no hint.
3. The truth file is written with restrictive permissions and a hash
   commitment is published in the open file. The analyst can verify AFTER
   scoring that the truth was not altered to match its answer.

The hash commitment is the important part. Without it, a truth file could
be edited post-hoc to make a wrong diagnosis look right. With it, the
answer is cryptographically fixed at injection time.

USAGE
    runner.py inject --fault pod-kill --target api
    runner.py inject --random
    runner.py list
    runner.py reveal <experiment-id>     # only after diagnosis is recorded
"""
import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# System-wide paths with REAL unix ownership separation:
#   sealed/    bbr-breaker:bbr-breaker 0700  <- analyst cannot enter
#   open/      world-readable                <- what the analyst may see
#   diagnoses/ analyst-writable              <- analyst records its answer
ROOT = Path(os.environ.get("BBR_STATE", "/var/lib/bbr"))
SEALED = ROOT / "experiments" / "sealed"
OPEN = ROOT / "experiments" / "open"
DIAG = ROOT / "experiments" / "diagnoses"

# Fault catalogue. Each entry is what the RUNNER knows; the analyst sees none
# of this. 'signature' records what SHOULD be observable, used only at scoring
# time to judge whether the analyst's evidence trail was plausible.
FAULTS = {
    "pod-kill": {
        "tier": 1,
        "description": "Delete a running pod; Kubernetes should recreate it.",
        "expected_signature": [
            "pod restart / recreation event",
            "brief endpoint removal",
            "short error window or none at all",
        ],
        "correct_action": "usually none - self-healing is working as designed",
    },
    "db-unreachable": {
        "tier": 2,
        "description": "Scale the database to zero so the api's upstream disappears.",
        "expected_signature": [
            "api returns 503 upstream_unavailable",
            "api PODS REMAIN HEALTHY (the key discriminator)",
            "database endpoints empty",
            "frontend returns 502",
        ],
        "correct_action": "restore database replicas",
    },
    "bad-service-selector": {
        "tier": 1,
        "description": "Point the api Service at a label no pod carries.",
        "expected_signature": [
            "api Service has zero endpoints",
            "api pods healthy and serving if called directly",
            "frontend cannot reach api",
        ],
        "correct_action": "restore the Service selector",
    },
    "bad-configmap": {
        "tier": 1,
        "description": "Point the api at a database URL that does not resolve.",
        "expected_signature": [
            "api 503 with DNS resolution failure in detail",
            "database itself healthy",
            "config change timestamp correlates with incident start",
        ],
        "correct_action": "revert the ConfigMap / env value",
    },
    "cpu-stress": {
        "tier": 2,
        "description": "Saturate api CPU so latency climbs without errors.",
        "expected_signature": [
            "p95 latency rises sharply",
            "error rate stays near zero (distinguishes from outage)",
            "CPU throttling visible in metrics",
            "no restarts",
        ],
        "correct_action": "remove the stressor; resource limits may need review",
    },
    "network-policy-deny": {
        "tier": 2,
        "description": "Apply a CiliumNetworkPolicy blocking api -> database.",
        "expected_signature": [
            "Hubble shows DENIED verdicts api->database",
            "both pods healthy",
            "api 503 on upstream timeout/refusal",
            "policy object created shortly before incident",
        ],
        "correct_action": "remove or correct the offending network policy",
    },
}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id():
    return "bbr-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def kubectl(*args, check=True):
    env = dict(os.environ)
    env.setdefault("KUBECONFIG", str(Path.home() / ".kube" / "config"))
    cmd = ["kubectl", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl failed: {r.stderr.strip()}")
    return r.stdout.strip()


def inject(fault, target):
    """Apply the fault. Returns a dict of what was actually done."""
    ns = "bbr-demo"
    if fault == "pod-kill":
        pods = kubectl("get", "pods", "-n", ns, "-l", f"app={target}",
                       "-o", "jsonpath={.items[*].metadata.name}").split()
        if not pods:
            raise RuntimeError(f"no pods for app={target}")
        victim = random.choice(pods)
        kubectl("delete", "pod", "-n", ns, victim, "--wait=false")
        return {"action": "deleted pod", "object": victim}

    if fault == "db-unreachable":
        kubectl("scale", "deployment", "database", "-n", ns, "--replicas=0")
        return {"action": "scaled database to 0", "object": "deployment/database"}

    if fault == "bad-service-selector":
        kubectl("patch", "service", "api", "-n", ns, "--type=merge",
                "-p", '{"spec":{"selector":{"app":"api-does-not-exist"}}}')
        return {"action": "broke Service selector", "object": "service/api"}

    if fault == "bad-configmap":
        kubectl("set", "env", "deployment/api", "-n", ns,
                "DB_URL=http://database-typo.bbr-demo.svc.cluster.local:8080/index.json")
        return {"action": "pointed api at non-resolving host", "object": "deployment/api"}

    if fault == "cpu-stress":
        # No Chaos Mesh yet; use a busy loop sidecar-free approach.
        pods = kubectl("get", "pods", "-n", ns, "-l", "app=api",
                       "-o", "jsonpath={.items[*].metadata.name}").split()
        if not pods:
            raise RuntimeError("no api pods")
        victim = pods[0]
        env = dict(os.environ)
        env.setdefault("KUBECONFIG", str(Path.home() / ".kube" / "config"))
        subprocess.Popen(
            ["kubectl", "exec", "-n", ns, victim, "--",
             "sh", "-c", "for i in 1 2 3; do (while :; do :; done) & done; sleep 600"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        return {"action": "started CPU burn", "object": victim}

    if fault == "network-policy-deny":
        policy = """apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: bbr-injected-deny
  namespace: bbr-demo
spec:
  endpointSelector:
    matchLabels:
      app: database
  ingress:
    - fromEndpoints:
        - matchLabels:
            app: frontend
"""
        p = Path("/tmp/bbr-policy.yaml")
        p.write_text(policy)
        kubectl("apply", "-f", str(p))
        return {"action": "applied deny policy", "object": "ciliumnetworkpolicy/bbr-injected-deny"}

    raise RuntimeError(f"unknown fault {fault}")


def cmd_inject(args):
    # Directories are created by 08-separate-identities.sh with correct
    # ownership. Do not recreate them here - that would silently reset perms.
    if not SEALED.is_dir():
        sys.exit(f"sealed dir missing: {SEALED} (run 08-separate-identities.sh)")

    fault = args.fault
    if args.random or not fault:
        fault = random.choice(list(FAULTS))
    if fault not in FAULTS:
        sys.exit(f"unknown fault: {fault}")

    target = args.target or "api"
    exp_id = new_id()
    t0 = now()

    result = inject(fault, target)

    truth = {
        "experiment_id": exp_id,
        "injected_at": t0,
        "fault": fault,
        "target": target,
        "tier": FAULTS[fault]["tier"],
        "description": FAULTS[fault]["description"],
        "expected_signature": FAULTS[fault]["expected_signature"],
        "correct_action": FAULTS[fault]["correct_action"],
        "injection_result": result,
    }
    blob = json.dumps(truth, indent=2, sort_keys=True)
    commitment = hashlib.sha256(blob.encode()).hexdigest()

    tp = SEALED / f"{exp_id}.truth.json"
    tp.write_text(blob)
    tp.chmod(0o600)

    # What the analyst is allowed to see: that an experiment started. Nothing else.
    incident = {
        "experiment_id": exp_id,
        "injected_at": t0,
        "status": "ACTIVE",
        "notice": "A fault was injected into namespace bbr-demo. "
                  "The fault type, target and mechanism are sealed. "
                  "Diagnose from telemetry only.",
        "truth_commitment_sha256": commitment,
        "scope": "namespace bbr-demo only",
    }
    op = OPEN / f"{exp_id}.incident.json"
    op.write_text(json.dumps(incident, indent=2))
    op.chmod(0o644)

    print(f"experiment:  {exp_id}")
    print(f"injected_at: {t0}")
    print(f"commitment:  {commitment[:32]}...")
    print(f"sealed:      {tp}  (0600)")
    print(f"open:        {op}  (0644)")
    print()
    print("The analyst may read the open file only.")


def cmd_list(args):
    OPEN.mkdir(parents=True, exist_ok=True)
    rows = sorted(OPEN.glob("*.incident.json"))
    if not rows:
        print("no experiments")
        return
    for f in rows:
        d = json.loads(f.read_text())
        sealed = (SEALED / f"{d['experiment_id']}.truth.json").exists()
        diag = (DIAG / f"{d['experiment_id']}.diagnosis.json").exists()
        print(f"  {d['experiment_id']}  {d['status']:10} "
              f"sealed={'yes' if sealed else 'NO':3} diagnosed={'yes' if diag else 'no'}")


def cmd_reveal(args):
    exp = args.experiment_id
    dp = DIAG / f"{exp}.diagnosis.json"
    if not dp.exists() and not args.force:
        sys.exit(f"REFUSING: no diagnosis recorded at {dp}\n"
                 "Record the analyst's diagnosis before revealing ground truth.\n"
                 "Use --force only if deliberately abandoning the experiment.")
    tp = SEALED / f"{exp}.truth.json"
    if not tp.exists():
        sys.exit(f"no sealed truth for {exp}")
    blob = tp.read_text()
    actual = hashlib.sha256(blob.encode()).hexdigest()
    op = json.loads((OPEN / f"{exp}.incident.json").read_text())
    expected = op["truth_commitment_sha256"]
    print(f"commitment verify: {'OK - truth unaltered' if actual == expected else 'FAILED - TRUTH WAS MODIFIED'}")
    print()
    print(blob)


def main():
    p = argparse.ArgumentParser(description="BBR experiment runner (breaker)")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("inject")
    i.add_argument("--fault", choices=list(FAULTS))
    i.add_argument("--target", default="api")
    i.add_argument("--random", action="store_true")
    i.set_defaults(func=cmd_inject)

    l = sub.add_parser("list")
    l.set_defaults(func=cmd_list)

    r = sub.add_parser("reveal")
    r.add_argument("experiment_id")
    r.add_argument("--force", action="store_true")
    r.set_defaults(func=cmd_reveal)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
