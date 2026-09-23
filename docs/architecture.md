# Architecture

## Failure-domain separation

BBR uses two machines or VMs:

1. **Controller/evidence plane** — runs the collector, console, and analyst workflow. It stays outside the fault domain.
2. **Disposable Kubernetes target** — runs the workload and telemetry agents. This is the only system the breaker is allowed to mutate.

Keeping the console outside the target matters. The dashboard must remain available when the target is unreachable. The API therefore reads SQLite snapshots; it does not initiate SSH during a page request.

## Data flow

```text
Target telemetry
  ├── Kubernetes API: pods, endpoints, events, configuration
  ├── Prometheus: service and resource metrics
  ├── Hubble: network verdicts and flow context
  └── Tetragon: process execution evidence
          │
          ▼
Fixed read-only collector / Evidence Broker
          │
          ├── bounded + redacted analyst output
          └── timestamped SQLite snapshots
                         │
                         ▼
                     Console API
                         │
                         ▼
                   Responsive browser UI
```

## Experiment lifecycle

1. Confirm a healthy baseline.
2. Breaker selects a fault and writes sealed truth.
3. Open incident publishes only an ID, timestamp, and hash commitment.
4. Analyst queries the Evidence Broker.
5. Analyst records ranked hypotheses, evidence, confidence, and proposed repair.
6. Breaker reveals truth only after a diagnosis file exists.
7. The repair path applies the smallest approved change.
8. Service-level probes verify recovery.
9. Scorer compares diagnosis to sealed truth and records calibration.

## Console design

The console stores snapshots in SQLite and exposes read-only views for:

- problems found and fixed
- current health and service-level indicators
- experiments and scoring
- workloads and endpoints
- network verdicts and policies
- kernel/process evidence
- safety controls and boundary test results

When the target cannot be reached, the last snapshot remains available with a stale/unreachable marker. Unreachability is data, not a 500 response.
