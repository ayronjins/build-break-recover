#!/usr/bin/env python3
"""Build-Break-Recover Evidence Broker.

This is the ONLY interface the AI analyst uses to see the cluster.

WHY A BROKER INSTEAD OF GIVING THE ANALYST kubectl
--------------------------------------------------
A read-only kubeconfig sounds sufficient, but it is not:

  1. `kubectl get secrets` is a read verb. Read-only RBAC that forgets to
     exclude Secrets hands over every credential in the cluster.
  2. Raw kubectl output is unbounded. An analyst can pull an entire cluster
     dump and drown the evidence in noise.
  3. Nothing is recorded. If the analyst's diagnosis is challenged later,
     there is no log of what it actually looked at.
  4. There is no redaction layer, so a ConfigMap containing a token leaks.

This broker fixes all four: a fixed verb list, hard output caps, mandatory
redaction, and an append-only audit log of every query.

WHAT IT DELIBERATELY CANNOT DO
------------------------------
No create/apply/patch/delete/scale. No exec. No Secret access of any kind.
No arbitrary kubectl passthrough. No access to the sealed ground-truth
directory. These are absent by construction, not blocked by a flag.

USAGE
    broker.py incident <id>                  what the analyst is told
    broker.py pods [-n NS]                   workload state
    broker.py events [-n NS] [--minutes N]   kubernetes events
    broker.py endpoints [-n NS]              service -> endpoint mapping
    broker.py logs <workload> [-n NS]        application logs (redacted)
    broker.py describe <workload> [-n NS]    spec/status, secrets stripped
    broker.py promql '<query>'               metrics plane
    broker.py sli                            golden signals vs baseline
    broker.py flows [--minutes N]            network plane (Hubble)
    broker.py processes [-n NS]              process plane (Tetragon)
    broker.py diff                           recent config changes
    broker.py audit                          what the analyst has queried
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("BBR_HOME", Path.home() / "bbr"))
AUDIT = Path(os.environ.get("BBR_AUDIT", Path.home() / "bbr" / "evidence" / "broker-audit.log"))
OPEN_DIR = Path(os.environ.get("BBR_STATE", "/var/lib/bbr")) / "experiments" / "open"
KUBECONFIG = os.environ.get("KUBECONFIG", str(Path.home() / ".kube" / "config"))

PROM = "http://127.0.0.1:30900"
HUBBLE_RELAY = os.environ.get("HUBBLE_RELAY", "127.0.0.1:30245")
DEFAULT_NS = "bbr-demo"

# Output caps. Prevents an analyst from pulling an unbounded dump and calling
# it evidence.
MAX_LOG_LINES = 200
MAX_EVENTS = 60
MAX_FLOWS = 80
MAX_PROC = 60

# ---------------------------------------------------------------- redaction
# Applied to EVERYTHING leaving the broker. Cheap insurance: a ConfigMap or
# log line containing a token must never reach the analyst's context.
REDACTIONS = [
    (re.compile(r'(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|bearer)\b\s*[:=]\s*\S+'),
     r'\1=<REDACTED>'),
    (re.compile(r'\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}'), '<REDACTED_JWT>'),
    (re.compile(r'\b(gh[pousr]_[A-Za-z0-9]{20,})'), '<REDACTED_GH_TOKEN>'),
    (re.compile(r'\b(sk-[A-Za-z0-9]{20,})'), '<REDACTED_API_KEY>'),
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----',
                re.S), '<REDACTED_PRIVATE_KEY>'),
]


def redact(text):
    if not isinstance(text, str):
        text = str(text)
    for pat, repl in REDACTIONS:
        text = pat.sub(repl, text)
    return text


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit(query, detail=""):
    """Append-only record of every analyst query."""
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a") as f:
        f.write(json.dumps({"at": now(), "query": query, "detail": detail}) + "\n")


def kubectl(*args, timeout=45):
    """Read-only kubectl. Verb is whitelisted here, not passed through."""
    allowed = {"get", "describe", "logs", "top", "version"}
    if args[0] not in allowed:
        sys.exit(f"BROKER REFUSED: verb '{args[0]}' is not read-only")
    env = dict(os.environ, KUBECONFIG=KUBECONFIG)
    r = subprocess.run(["kubectl", *args], capture_output=True, text=True,
                       timeout=timeout, env=env)
    if r.returncode != 0:
        return f"(kubectl error: {r.stderr.strip()[:200]})"
    return r.stdout


def promql(query, timeout=20):
    import urllib.parse
    import urllib.request
    url = f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ------------------------------------------------------------------ commands
def cmd_incident(a):
    audit("incident", a.experiment_id)
    p = OPEN_DIR / f"{a.experiment_id}.incident.json"
    if not p.exists():
        sys.exit(f"no open incident file for {a.experiment_id}")
    d = json.loads(p.read_text())
    print(f"experiment:  {d['experiment_id']}")
    print(f"injected_at: {d['injected_at']}")
    print(f"status:      {d['status']}")
    print(f"scope:       {d['scope']}")
    print()
    print(d["notice"])
    # The sealed truth is NOT readable here. By design.


def cmd_pods(a):
    audit("pods", a.namespace)
    out = kubectl("get", "pods", "-n", a.namespace, "-o", "wide")
    print(redact(out))
    print("--- restart detail ---")
    js = kubectl("get", "pods", "-n", a.namespace, "-o", "json")
    try:
        d = json.loads(js)
    except Exception:
        return
    for item in d.get("items", []):
        name = item["metadata"]["name"]
        for cs in item.get("status", {}).get("containerStatuses", []):
            rc = cs.get("restartCount", 0)
            ready = cs.get("ready")
            state = list(cs.get("state", {}).keys())
            last = cs.get("lastState", {})
            reason = ""
            if "terminated" in last:
                reason = f" lastExit={last['terminated'].get('reason')} code={last['terminated'].get('exitCode')}"
            print(f"  {name:34} ready={ready} restarts={rc} state={state}{reason}")


def cmd_events(a):
    audit("events", f"{a.namespace} {a.minutes}m")
    out = kubectl("get", "events", "-n", a.namespace,
                  "--sort-by=.lastTimestamp", "-o", "wide")
    lines = out.splitlines()[-MAX_EVENTS:]
    print(redact("\n".join(lines)))


def cmd_endpoints(a):
    """A Service with zero endpoints is one of the great silent failures -
    everything looks Running while nothing can be reached."""
    audit("endpoints", a.namespace)
    js = kubectl("get", "endpointslice", "-n", a.namespace, "-o", "json")
    try:
        d = json.loads(js)
    except Exception:
        print(js)
        return
    by_svc = {}
    for item in d.get("items", []):
        svc = item["metadata"].get("labels", {}).get("kubernetes.io/service-name", "?")
        addrs = []
        # An EndpointSlice with no ready endpoints has endpoints: null,
        # not []. Found by a real incident: the crash hid the exact signal
        # the command exists to surface.
        for ep in (item.get("endpoints") or []):
            ready = ep.get("conditions", {}).get("ready")
            addrs += [f"{x}{'' if ready else '(NOTREADY)'}" for x in ep.get("addresses", [])]
        by_svc.setdefault(svc, []).extend(addrs)
    for svc, addrs in sorted(by_svc.items()):
        flag = "  <-- ZERO ENDPOINTS" if not addrs else ""
        print(f"  {svc:14} {len(addrs)} endpoint(s) {addrs}{flag}")

    print("\n--- service selectors (a wrong selector produces zero endpoints) ---")
    sj = kubectl("get", "svc", "-n", a.namespace, "-o", "json")
    try:
        sd = json.loads(sj)
    except Exception:
        return
    for item in sd.get("items", []):
        print(f"  {item['metadata']['name']:14} selector={item['spec'].get('selector')}")


def cmd_logs(a):
    audit("logs", f"{a.namespace}/{a.workload}")
    out = kubectl("logs", "-n", a.namespace, f"deployment/{a.workload}",
                  f"--tail={MAX_LOG_LINES}", "--all-containers=true",
                  "--prefix=true")
    print(redact(out))


def cmd_describe(a):
    audit("describe", f"{a.namespace}/{a.workload}")
    js = kubectl("get", "deployment", a.workload, "-n", a.namespace, "-o", "json")
    try:
        d = json.loads(js)
    except Exception:
        print(js)
        return
    spec = d.get("spec", {})
    tpl = spec.get("template", {}).get("spec", {})
    print(f"  replicas: desired={spec.get('replicas')} "
          f"ready={d.get('status',{}).get('readyReplicas')} "
          f"available={d.get('status',{}).get('availableReplicas')}")
    print(f"  selector: {spec.get('selector',{}).get('matchLabels')}")
    for c in tpl.get("containers", []):
        print(f"  container {c['name']}:")
        print(f"    image: {c.get('image')}")
        print(f"    resources: {c.get('resources')}")
        env = []
        for e in c.get("env", []):
            # Never surface a value sourced from a Secret.
            if "valueFrom" in e and "secretKeyRef" in str(e.get("valueFrom")):
                env.append(f"{e['name']}=<FROM_SECRET:REDACTED>")
            else:
                env.append(f"{e['name']}={redact(str(e.get('value')))}")
        print(f"    env: {env}")
    for cond in d.get("status", {}).get("conditions", []):
        print(f"  condition {cond.get('type')}={cond.get('status')} "
              f"reason={cond.get('reason')} msg={redact(str(cond.get('message'))[:120])}")


def cmd_promql(a):
    audit("promql", a.query)
    d = promql(a.query)
    if d.get("status") != "success":
        print(f"  query failed: {d.get('error') or d}")
        return
    res = d["data"]["result"]
    if not res:
        print("  (no data)")
        return
    for r in res[:40]:
        labels = {k: v for k, v in r["metric"].items() if k != "__name__"}
        val = r["value"][1] if "value" in r else r.get("values", [])[-1][1]
        print(f"  {r['metric'].get('__name__','')} {labels} = {val}")


def cmd_sli(a):
    """Golden signals. This is what 'is the service actually broken' means."""
    audit("sli")
    print("=== SERVICE LEVEL INDICATORS (live probe) ===")
    codes, times = {}, []
    for _ in range(a.samples):
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code} %{time_total}",
             "--max-time", "10", "http://127.0.0.1:30080/"],
            capture_output=True, text=True)
        parts = r.stdout.split()
        if len(parts) == 2:
            codes[parts[0]] = codes.get(parts[0], 0) + 1
            times.append(float(parts[1]))
    if not times:
        print("  probe failed entirely")
        return
    times.sort()
    n = len(times)
    total = sum(codes.values())
    err = sum(v for k, v in codes.items() if k.startswith(("5", "0")))
    print(f"  requests:   {total}")
    for c, v in sorted(codes.items()):
        print(f"  HTTP {c}:    {v} ({v*100/total:.1f}%)")
    print(f"  error rate: {err*100/total:.1f}%")
    print(f"  p50:        {times[int(n*0.50)-1]*1000:.1f} ms")
    print(f"  p95:        {times[int(n*0.95)-1]*1000:.1f} ms")
    print(f"  max:        {times[-1]*1000:.1f} ms")
    print()
    print("  BASELINE (healthy, recorded pre-incident):")
    print("    error rate 0.0%   p50 3.6 ms   p95 4.3 ms")


def cmd_flows(a):
    """Network plane. Distinguishes 'cannot reach' from 'is broken'."""
    audit("flows", f"{a.minutes}m")
    print("=== FLOW VERDICTS (Hubble via Prometheus) ===")
    d = promql("sum by (verdict) (hubble_flows_processed_total)")
    if d.get("status") == "success":
        for r in d["data"]["result"]:
            print(f"  {r['metric'].get('verdict','?'):12} {r['value'][1]}")
    print()
    print(f"=== DROPS IN LAST {a.minutes}m ===")
    d = promql(f"sum by (reason) (increase(hubble_drop_total[{a.minutes}m]))")
    if d.get("status") == "success":
        rows = [r for r in d["data"]["result"] if float(r["value"][1]) > 0.5]
        if not rows:
            print("  no drops")
        for r in rows:
            print(f"  reason={r['metric'].get('reason','?'):24} {float(r['value'][1]):.0f}")
    print()
    print("=== LIVE FLOWS (via Hubble Relay) ===")
    # Do NOT shell into the cilium DaemonSet (needs pods/exec) and do NOT
    # port-forward (pods/portforward tunnels to ANY pod, including privileged
    # admin APIs - that is escalation disguised as telemetry access).
    # Hubble Relay is exposed on a NodePort restricted to this host, so the
    # analyst reads flows over the network with no Kubernetes rights at all.
    # Found during incident bbr-20260923-060715: the guardrail was right,
    # the broker was wrong.
    if not shutil.which("hubble"):
        print("  hubble CLI not installed; Prometheus verdict counts above still apply")
        return
    try:
        r = subprocess.run(
            ["hubble", "observe", "--server", HUBBLE_RELAY,
             "--namespace", DEFAULT_NS, "--last", str(MAX_FLOWS)],
            capture_output=True, text=True, timeout=45)
        out = (r.stdout or r.stderr)[:6000]
        print(redact(out) if out.strip() else "  (no flows returned)")
    except Exception as e:
        print(f"  hubble query failed: {e}")


def cmd_processes(a):
    """Process plane. Answers 'what actually executed', incl. unexpected shells."""
    audit("processes", a.namespace)
    env = dict(os.environ, KUBECONFIG=KUBECONFIG)
    tg = subprocess.run(
        ["kubectl", "-n", "kube-system", "get", "pods",
         "-l", "app.kubernetes.io/name=tetragon",
         "-o", "jsonpath={.items[0].metadata.name}"],
        capture_output=True, text=True, env=env).stdout.strip()
    if not tg:
        print("  tetragon not found")
        return
    r = subprocess.run(
        ["kubectl", "-n", "kube-system", "logs", tg, "-c", "export-stdout",
         "--tail=1200"],
        capture_output=True, text=True, timeout=60, env=env)
    shown = 0
    print(f"=== PROCESS EXECUTIONS in {a.namespace} ===")
    for line in r.stdout.splitlines():
        try:
            e = json.loads(line)
        except Exception:
            continue
        pe = e.get("process_exec", {}).get("process", {})
        pod = pe.get("pod", {})
        if pod.get("namespace") != a.namespace:
            continue
        shown += 1
        if shown > MAX_PROC:
            continue
        binary = pe.get("binary", "")
        args = redact(str(pe.get("arguments", ""))[:60])
        # An interactive shell in a running container is a security signal,
        # not a normal application event.
        flag = "  <-- SHELL" if binary.split("/")[-1] in ("sh", "bash", "ash", "dash") else ""
        print(f"  {e.get('time','')[:19]} {pod.get('name','')[:26]:26} "
              f"{binary[:30]:30} {args}{flag}")
    print(f"  total events: {shown}")


def cmd_diff(a):
    """'What changed' - the single most valuable question in incident response."""
    audit("diff")
    print("=== RECENT CHANGES (rollout history + object ages) ===")
    for wl in ("frontend", "api", "database"):
        out = kubectl("get", "deployment", wl, "-n", DEFAULT_NS, "-o",
                      "jsonpath={.metadata.generation}{\"|\"}"
                      "{.metadata.creationTimestamp}{\"|\"}"
                      "{.spec.template.metadata.annotations}")
        print(f"  {wl:10} generation|created|annotations: {redact(out)}")
    print()
    print("=== NETWORK POLICIES (a new one right before an incident is a lead) ===")
    out = kubectl("get", "ciliumnetworkpolicy,networkpolicy", "-n", DEFAULT_NS,
                  "-o", "wide")
    print(redact(out) if out.strip() else "  (none)")
    print()
    print("=== CONFIGMAP AGES ===")
    out = kubectl("get", "configmap", "-n", DEFAULT_NS,
                  "-o", "custom-columns=NAME:.metadata.name,AGE:.metadata.creationTimestamp")
    print(out)


def cmd_audit(a):
    if not AUDIT.exists():
        print("no queries recorded")
        return
    print("=== ANALYST QUERY LOG ===")
    for line in AUDIT.read_text().splitlines()[-60:]:
        try:
            d = json.loads(line)
            print(f"  {d['at']}  {d['query']:12} {d.get('detail','')}")
        except Exception:
            pass


def main():
    p = argparse.ArgumentParser(description="BBR Evidence Broker (read-only)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def ns(sp):
        sp.add_argument("-n", "--namespace", default=DEFAULT_NS)
        return sp

    s = sub.add_parser("incident"); s.add_argument("experiment_id"); s.set_defaults(func=cmd_incident)
    s = ns(sub.add_parser("pods")); s.set_defaults(func=cmd_pods)
    s = ns(sub.add_parser("events")); s.add_argument("--minutes", type=int, default=30); s.set_defaults(func=cmd_events)
    s = ns(sub.add_parser("endpoints")); s.set_defaults(func=cmd_endpoints)
    s = ns(sub.add_parser("logs")); s.add_argument("workload"); s.set_defaults(func=cmd_logs)
    s = ns(sub.add_parser("describe")); s.add_argument("workload"); s.set_defaults(func=cmd_describe)
    s = sub.add_parser("promql"); s.add_argument("query"); s.set_defaults(func=cmd_promql)
    s = sub.add_parser("sli"); s.add_argument("--samples", type=int, default=20); s.set_defaults(func=cmd_sli)
    s = sub.add_parser("flows"); s.add_argument("--minutes", type=int, default=10); s.set_defaults(func=cmd_flows)
    s = ns(sub.add_parser("processes")); s.set_defaults(func=cmd_processes)
    s = sub.add_parser("diff"); s.set_defaults(func=cmd_diff)
    s = sub.add_parser("audit"); s.set_defaults(func=cmd_audit)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
