#!/usr/bin/env bash
### Adversarially verify the analyst boundary.
###
### The report's premise depends on these guarantees. Asserting them is not
### enough - each one is tested by ATTEMPTING the forbidden action and
### confirming it fails. A control that has never been tested is a guess.
set -uo pipefail

PASS=0; FAIL=0
check() { # check <description> <expect-fail|expect-ok> <command...>
  local desc="$1" mode="$2"; shift 2
  if out=$("$@" 2>&1); then rc=0; else rc=1; fi
  if [ "$mode" = "expect-fail" ]; then
    if [ $rc -ne 0 ]; then echo "  PASS  $desc (correctly denied)"; PASS=$((PASS+1));
    else echo "  FAIL  $desc -- SUCCEEDED BUT SHOULD HAVE BEEN DENIED"; FAIL=$((FAIL+1)); fi
  else
    if [ $rc -eq 0 ]; then echo "  PASS  $desc"; PASS=$((PASS+1));
    else echo "  FAIL  $desc -- $(echo "$out" | head -1)"; FAIL=$((FAIL+1)); fi
  fi
}

A=(sudo -u bbr-analyst env KUBECONFIG=/home/bbr-analyst/.kube/config)

echo "=== 1. SEALED GROUND TRUTH IS UNREACHABLE BY THE ANALYST ==="
check "analyst cannot list sealed dir"      expect-fail sudo -u bbr-analyst ls /var/lib/bbr/experiments/sealed
check "analyst cannot cd into sealed dir"   expect-fail sudo -u bbr-analyst bash -c 'cd /var/lib/bbr/experiments/sealed'
check "analyst CAN read open incidents"     expect-ok   sudo -u bbr-analyst ls /var/lib/bbr/experiments/open

echo
echo "=== 2. ANALYST CANNOT MUTATE THE CLUSTER ==="
check "cannot delete a pod"        expect-fail "${A[@]}" kubectl delete pod -n bbr-demo --all --dry-run=server
check "cannot scale a deployment"  expect-fail "${A[@]}" kubectl scale deployment api -n bbr-demo --replicas=5
check "cannot apply manifests"     expect-fail "${A[@]}" kubectl create deployment evil --image=nginx -n bbr-demo
check "cannot patch a service"     expect-fail "${A[@]}" kubectl patch svc api -n bbr-demo --type=merge -p '{"spec":{"selector":{"x":"y"}}}'
check "cannot exec into a pod"     expect-fail "${A[@]}" kubectl auth can-i create pods/exec -n bbr-demo --quiet

echo
echo "=== 3. ANALYST CANNOT READ SECRETS (the classic read-only mistake) ==="
check "cannot list secrets"            expect-fail "${A[@]}" kubectl get secrets -n default
check "cannot read the analyst token"  expect-fail "${A[@]}" kubectl get secret bbr-analyst-token -n default
check "cannot list secrets cluster-wide" expect-fail "${A[@]}" kubectl get secrets -A

echo
echo "=== 4. ANALYST CANNOT TOUCH POLICY / SECURITY OBJECTS ==="
check "cannot delete network policies" expect-fail "${A[@]}" kubectl auth can-i delete ciliumnetworkpolicies -n bbr-demo --quiet
check "cannot modify RBAC"             expect-fail "${A[@]}" kubectl auth can-i create clusterrolebindings --quiet
check "cannot read its own RBAC to edit" expect-fail "${A[@]}" kubectl auth can-i update clusterroles --quiet

echo
echo "=== 5. ANALYST *CAN* DO ITS ACTUAL JOB (guardrails must not block work) ==="
check "can list pods"           expect-ok "${A[@]}" kubectl get pods -n bbr-demo
check "can read pod logs"       expect-ok "${A[@]}" kubectl logs -n bbr-demo deployment/api --tail=5
check "can list events"         expect-ok "${A[@]}" kubectl get events -n bbr-demo
check "can read endpointslices" expect-ok "${A[@]}" kubectl get endpointslices -n bbr-demo
check "can read configmaps"     expect-ok "${A[@]}" kubectl get configmap api-code -n bbr-demo
check "can read network policy objects" expect-ok "${A[@]}" kubectl get ciliumnetworkpolicies -A
check "can reach Prometheus"    expect-ok sudo -u bbr-analyst curl -sf --max-time 10 http://127.0.0.1:30900/-/healthy

echo
echo "=== 6. BREAKER RETAINS THE ABILITY TO INJECT ==="
check "breaker can read sealed dir" expect-ok sudo -u bbr-breaker ls /var/lib/bbr/experiments/sealed
check "breaker can scale (dry-run)" expect-ok sudo -u bbr-breaker env KUBECONFIG=/home/bbr-breaker/.kube/config kubectl scale deployment database -n bbr-demo --replicas=1 --dry-run=server

echo
echo "================================================="
echo "  PASSED: $PASS    FAILED: $FAIL"
echo "================================================="
[ "$FAIL" -eq 0 ] || echo "  *** BOUNDARY IS NOT SOUND - DO NOT RUN EXPERIMENTS ***"
