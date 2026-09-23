# Security model

BBR deliberately injects failures. Its safety comes from constrained scope and separated authority, not from trusting the AI to behave.

## Trust boundaries

### Breaker

The breaker may execute only a fixed catalogue of namespace-scoped faults. It owns sealed ground truth and cannot act as the analyst during a scored experiment.

### Analyst

The analyst receives a Kubernetes service-account kubeconfig with `get`, `list`, and `watch` access to selected non-secret resources. It cannot:

- read Secrets
- create, patch, update, delete, or scale resources
- exec or port-forward into pods
- change RBAC
- enter the sealed-truth directory

### Evidence Broker

The broker further narrows read access:

- fixed verbs and query types
- hard output caps
- token/key/password redaction
- no arbitrary kubectl arguments
- append-only query audit log

### Policy gate

Kyverno rejects shortcuts that would trade security for availability:

- privileged containers
- privilege escalation
- host PID/network/IPC namespaces
- dangerous Linux capabilities
- hostPath volumes
- untagged or `latest` images
- workloads without CPU and memory limits
- mutation of protected observability resources from the demo namespace

## Scope rules

- Use a dedicated, disposable test cluster.
- Apply the chaos opt-in label only to namespaces that may be broken.
- Never target control-plane, observability, policy, or evidence systems.
- Do not add kernel or host-reboot faults unless telemetry and recovery controls are outside that failure domain.
- Verify each repair using the same service-level probe used to establish the healthy baseline.

## Secrets

This repository includes no live credentials. A real deployment must keep kubeconfigs, tokens, SSH keys, passwords, and sealed experiment truth out of source control.

Recommended controls:

- SSH key authentication only
- host firewall restricting admin interfaces to the controller
- short-lived service-account tokens when possible
- `automountServiceAccountToken: false` for workloads that do not call the Kubernetes API
- encrypted Kubernetes Secrets at rest
- secret scanning before every push

## Responsible use

The included fault injector is not an exploitation framework. It is intended for systems you own or have explicit authorization to test. Never point it at shared or production infrastructure.
