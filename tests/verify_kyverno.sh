#!/usr/bin/env bash
### Adversarially verify the Kyverno policy gate.
###
### Each test submits a manifest representing an unsafe "fix" an agent might
### propose under pressure, and confirms admission REJECTS it. A policy that
### has never rejected anything is an untested assumption.
set -uo pipefail
export KUBECONFIG="$HOME/.kube/config"

PASS=0; FAIL=0
try() { # try <desc> <yaml>
  local desc="$1" yaml="$2"
  if echo "$yaml" | kubectl apply --dry-run=server -f - >/tmp/kv.out 2>&1; then
    echo "  FAIL  $desc -- ADMITTED (should have been denied)"; FAIL=$((FAIL+1))
  else
    echo "  PASS  $desc"
    sed -n 's/.*DENIED: \(.*\)/          reason: \1/p' /tmp/kv.out | head -1
    PASS=$((PASS+1))
  fi
}

echo "=== UNSAFE MANIFESTS MUST BE REJECTED AT ADMISSION ==="

try "privileged container" 'apiVersion: v1
kind: Pod
metadata: {name: t-priv, namespace: bbr-demo}
spec:
  containers:
  - name: c
    image: nginx:1.29-alpine
    securityContext: {privileged: true, allowPrivilegeEscalation: false}
    resources: {limits: {cpu: 100m, memory: 64Mi}}'

try "privilege escalation allowed" 'apiVersion: v1
kind: Pod
metadata: {name: t-esc, namespace: bbr-demo}
spec:
  containers:
  - name: c
    image: nginx:1.29-alpine
    securityContext: {allowPrivilegeEscalation: true}
    resources: {limits: {cpu: 100m, memory: 64Mi}}'

try "hostNetwork" 'apiVersion: v1
kind: Pod
metadata: {name: t-hostnet, namespace: bbr-demo}
spec:
  hostNetwork: true
  containers:
  - name: c
    image: nginx:1.29-alpine
    securityContext: {allowPrivilegeEscalation: false}
    resources: {limits: {cpu: 100m, memory: 64Mi}}'

try "hostPID" 'apiVersion: v1
kind: Pod
metadata: {name: t-hostpid, namespace: bbr-demo}
spec:
  hostPID: true
  containers:
  - name: c
    image: nginx:1.29-alpine
    securityContext: {allowPrivilegeEscalation: false}
    resources: {limits: {cpu: 100m, memory: 64Mi}}'

try "hostPath volume (node filesystem escape)" 'apiVersion: v1
kind: Pod
metadata: {name: t-hostpath, namespace: bbr-demo}
spec:
  volumes: [{name: v, hostPath: {path: /}}]
  containers:
  - name: c
    image: nginx:1.29-alpine
    securityContext: {allowPrivilegeEscalation: false}
    resources: {limits: {cpu: 100m, memory: 64Mi}}
    volumeMounts: [{name: v, mountPath: /host}]'

try "SYS_ADMIN capability" 'apiVersion: v1
kind: Pod
metadata: {name: t-cap, namespace: bbr-demo}
spec:
  containers:
  - name: c
    image: nginx:1.29-alpine
    securityContext:
      allowPrivilegeEscalation: false
      capabilities: {add: [SYS_ADMIN]}
    resources: {limits: {cpu: 100m, memory: 64Mi}}'

try "NET_ADMIN capability (would let a pod rewrite networking)" 'apiVersion: v1
kind: Pod
metadata: {name: t-netadm, namespace: bbr-demo}
spec:
  containers:
  - name: c
    image: nginx:1.29-alpine
    securityContext:
      allowPrivilegeEscalation: false
      capabilities: {add: [NET_ADMIN]}
    resources: {limits: {cpu: 100m, memory: 64Mi}}'

try ":latest image tag" 'apiVersion: v1
kind: Pod
metadata: {name: t-latest, namespace: bbr-demo}
spec:
  containers:
  - name: c
    image: nginx:latest
    securityContext: {allowPrivilegeEscalation: false}
    resources: {limits: {cpu: 100m, memory: 64Mi}}'

try "missing resource limits" 'apiVersion: v1
kind: Pod
metadata: {name: t-nolimit, namespace: bbr-demo}
spec:
  containers:
  - name: c
    image: nginx:1.29-alpine
    securityContext: {allowPrivilegeEscalation: false}'

echo
echo "=== A LEGITIMATE WORKLOAD MUST STILL BE ADMITTED ==="
if echo 'apiVersion: v1
kind: Pod
metadata: {name: t-good, namespace: bbr-demo}
spec:
  containers:
  - name: c
    image: nginx:1.29-alpine
    securityContext:
      allowPrivilegeEscalation: false
      capabilities: {drop: [ALL]}
    resources: {limits: {cpu: 100m, memory: 64Mi}}' | kubectl apply --dry-run=server -f - >/dev/null 2>&1; then
  echo "  PASS  compliant pod admitted (policies are not just blocking everything)"
  PASS=$((PASS+1))
else
  echo "  FAIL  compliant pod was REJECTED -- policies are too strict"
  FAIL=$((FAIL+1))
fi

echo
echo "=== THE RUNNING APPLICATION STILL COMPLIES ==="
if kubectl get pods -n bbr-demo --no-headers | grep -qv Running; then
  echo "  WARN  some demo pods are not Running"
else
  echo "  PASS  all demo pods still Running under enforced policy"
  PASS=$((PASS+1))
fi

echo
echo "================================================="
echo "  PASSED: $PASS    FAILED: $FAIL"
echo "================================================="
