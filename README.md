# Build-Break-Recover (BBR)

A Kubernetes reliability lab that tests whether an AI can diagnose a failure from evidence instead of reacting blindly.

**Live console:** [bbr.ayron.in](https://bbr.ayron.in/)
**Built by:** [Ayron Jins](https://ayron.in/)

## What BBR does

BBR injects a controlled fault into an isolated Kubernetes test system. A separate analyst identity investigates through a constrained evidence broker, records a diagnosis before seeing the answer, proposes the smallest safe repair, and verifies recovery against a healthy baseline.

```text
Build → Break → Observe → Understand → Propose → Enforce → Repair → Verify
```

The project is deliberately not an unrestricted auto-remediation bot. The AI is the investigator and repair proposer. Access control, admission policy, and a separate execution path decide what it can see and what may change.

## The important experiment

A deleted pod is not automatically a failure. If Kubernetes replaces it and no request fails, the correct diagnosis is **no repair required**. BBR scores that as a success. Changing a healthy system just to look active is a failure of judgment.

## Architecture

```text
┌──────────────────────── Controller / evidence plane ────────────────────────┐
│ Read-only collector → SQLite → FastAPI → browser console                    │
│                                                                            │
│ AI analyst → Evidence Broker → bounded/redacted telemetry                   │
│                                      │                                     │
│ Human-approved execution path ← proposed minimal repair                     │
└──────────────────────────────────────┼─────────────────────────────────────┘
                                       │ SSH / Kubernetes API
┌────────────────────── Isolated Kubernetes test system ──────────────────────┐
│ frontend → api → database                                                   │
│                                                                            │
│ K3s + Cilium/Hubble + Tetragon + Prometheus + Kyverno                       │
│                                                                            │
│ bbr-breaker: owns sealed truth       bbr-analyst: read-only evidence        │
│       │                                      │                             │
│ injects controlled fault              cannot read answer or mutate cluster │
└────────────────────────────────────────────────────────────────────────────┘
```

The console reads SQLite rather than contacting the test system during a page request. If the test system dies, the console still serves the last known snapshot and marks it stale.

## Structural blind diagnosis

"Do not look at the answer" is an instruction, not a control. BBR separates the breaker and analyst with Unix ownership and Kubernetes RBAC:

- `bbr-breaker` writes ground truth into a `0700` directory.
- `bbr-analyst` cannot enter that directory, read Kubernetes Secrets, exec into pods, or mutate resources.
- The open incident contains only the experiment ID, timestamp, and a SHA-256 commitment to the sealed truth.
- The analyst records its diagnosis before reveal.
- Reveal verifies the commitment before showing the answer.

The included boundary test has 23 checks. The Kyverno admission test has 11 checks.

## Fault catalogue

The reference runner implements six bounded fault classes:

| Fault | What it tests | Useful discriminator |
|---|---|---|
| `pod-kill` | Kubernetes self-healing | Same ReplicaSet hash; often no user impact |
| `db-unreachable` | Missing dependency capacity | Zero endpoints and desired replicas = 0 |
| `bad-service-selector` | Routing metadata | Healthy pods but Service has zero endpoints |
| `bad-configmap` | Invalid dependency hostname | DNS failure in milliseconds |
| `cpu-stress` | Latency without errors | One replica at its CPU limit; error rate stays near zero |
| `network-policy-deny` | Silent network drop | Failure latency pins to the client timeout |

Three faults can produce the same visible HTTP 502. Evidence from routing, latency, process, and network planes separates them.

## Evidence Broker

`core/broker.py` is the analyst's only cluster interface. It provides a fixed command set:

- workload and endpoint state
- bounded, redacted logs
- Kubernetes events and recent configuration changes
- Prometheus queries
- Hubble network flows
- Tetragon process events
- service-level probes against a recorded baseline
- an audit trail of every analyst query

It has no arbitrary `kubectl` passthrough and no mutation verbs.

## Repository layout

```text
core/
  runner.py          controlled fault injection + sealed truth
  broker.py          bounded, redacted evidence interface
  score_all.py       post-reveal accuracy and calibration scoring
manifests/
  demo-app.yaml      secured three-tier demo workload
  kyverno-policies.yaml
console/
  api/app.py         SQLite-backed read-only API
  web/               responsive console UI
tests/
  verify_boundary.sh 23 adversarial identity/RBAC tests
  verify_kyverno.sh  11 unsafe-manifest admission tests
docs/
  architecture.md
  security-model.md
```

## Safety model

This repository is for a **dedicated disposable lab**, not a production cluster.

- Faults are namespace-scoped and selected from a fixed catalogue.
- System and observability namespaces are excluded from fault targeting.
- Repairs should be minimal and evidence-backed.
- Never disable telemetry to silence an alert.
- Never grant privilege just to make an error disappear.
- Kernel-level chaos is intentionally excluded from the single-node reference lab.

Read [docs/security-model.md](docs/security-model.md) before running anything.

## Quick start

This is a reference implementation, not a one-command installer. You need an isolated Kubernetes cluster with Cilium, Hubble, Tetragon, Prometheus, and Kyverno already available.

```bash
kubectl apply -f manifests/demo-app.yaml
kubectl apply -f manifests/kyverno-policies.yaml

# Create the identity/RBAC separation described in docs/security-model.md.
# Then install runner.py as bbr-breaker and broker.py as bbr-analyst.

sudo -u bbr-breaker BBR_STATE=/var/lib/bbr \
  python3 core/runner.py inject --fault pod-kill --target api

sudo -u bbr-analyst KUBECONFIG=/home/bbr-analyst/.kube/config \
  python3 core/broker.py sli
```

Do not run the fault injector against a cluster you are unwilling to rebuild.

## What is intentionally not included

- credentials, kubeconfigs, SSH keys, tokens, or Secrets
- private network addresses and host-specific service files
- sealed ground-truth files or raw experiment evidence
- production deployment automation
- kernel panic, disk destruction, or host-reboot fault classes

## Current limitations

- Single-node reference environment
- No distributed tracing
- Telemetry must live off the failure domain before host/kernel chaos is safe
- The same project author built the catalogue and evaluated it; structural answer isolation helps, but it does not replace an independent benchmark designer

## License

MIT — see [LICENSE](LICENSE).
