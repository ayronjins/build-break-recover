#!/usr/bin/env bash
# shellcheck disable=SC2034
# Remote evidence collector for the BBR Console.
#
# Runs ON the test VM and emits ONE JSON document to stdout. Single SSH
# round-trip per poll instead of a dozen.
#
# Every command here is READ-ONLY. There is no parameter interpolation from
# the web layer into this script - the web tier can only trigger "run the
# fixed script", never "run this command".
set -uo pipefail
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

j() { python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'; }

echo "{"
echo "\"collected_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
echo "\"hostname\": \"$(hostname)\","

# ---- host ----
echo "\"host\": {"
echo "  \"uptime_seconds\": $(awk '{print int($1)}' /proc/uptime),"
echo "  \"load1\": $(awk '{print $1}' /proc/loadavg),"
read -r _ tot used free shared buff avail < <(free -m | awk 'NR==2')
echo "  \"mem_total_mb\": ${tot}, \"mem_used_mb\": ${used}, \"mem_avail_mb\": ${avail},"
read -r _ size dused davail dpct _ < <(df -BG / | awk 'NR==2')
echo "  \"disk_used\": \"${dused}\", \"disk_avail\": \"${davail}\", \"disk_pct\": \"${dpct}\","
echo "  \"kernel\": \"$(uname -r)\""
echo "},"

# ---- lvm snapshots (rollback capability) ----
echo "\"snapshots\": ["
sudo lvs --noheadings -o lv_name,lv_size,data_percent,lv_time --select 'lv_attr=~^s' 2>/dev/null \
 | awk '{printf "%s{\"name\":\"%s\",\"size\":\"%s\",\"used_pct\":\"%s\"}", (NR>1?",":""), $1, $2, $3}'
echo "],"
echo "\"vg_free\": \"$(sudo vgs --noheadings -o vg_free 2>/dev/null | tr -d ' ')\","

# ---- kubernetes ----
echo "\"k8s_nodes\": $(kubectl get nodes -o json 2>/dev/null | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except: print("[]"); raise SystemExit
out=[]
for n in d.get("items",[]):
    conds={c["type"]:c["status"] for c in n["status"].get("conditions",[])}
    out.append({"name":n["metadata"]["name"],"ready":conds.get("Ready"),
                "version":n["status"]["nodeInfo"]["kubeletVersion"]})
print(json.dumps(out))' || echo '[]'),"

echo "\"pods\": $(kubectl get pods -A -o json 2>/dev/null | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except: print("[]"); raise SystemExit
out=[]
for p in d.get("items",[]):
    cs=p.get("status",{}).get("containerStatuses") or []
    out.append({
      "ns":p["metadata"]["namespace"],
      "name":p["metadata"]["name"],
      "phase":p.get("status",{}).get("phase"),
      "ready":sum(1 for c in cs if c.get("ready")),
      "total":len(cs),
      "restarts":sum(c.get("restartCount",0) for c in cs),
    })
print(json.dumps(out))' || echo '[]'),"

echo "\"deployments\": $(kubectl get deploy -n bbr-demo -o json 2>/dev/null | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except: print("[]"); raise SystemExit
out=[]
for x in d.get("items",[]):
    out.append({"name":x["metadata"]["name"],
                "generation":x["metadata"].get("generation"),
                "desired":x["spec"].get("replicas"),
                "ready":x.get("status",{}).get("readyReplicas") or 0})
print(json.dumps(out))' || echo '[]'),"

echo "\"endpoints\": $(kubectl get endpointslice -n bbr-demo -o json 2>/dev/null | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except: print("{}"); raise SystemExit
agg={}
for i in d.get("items",[]):
    svc=i["metadata"].get("labels",{}).get("kubernetes.io/service-name","?")
    n=0
    for ep in (i.get("endpoints") or []):
        if ep.get("conditions",{}).get("ready"): n+=len(ep.get("addresses") or [])
    agg[svc]=agg.get(svc,0)+n
print(json.dumps(agg))' || echo '{}'),"

echo "\"policies\": $(kubectl get clusterpolicy -o json 2>/dev/null | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except: print("[]"); raise SystemExit
out=[]
for p in d.get("items",[]):
    if not p["metadata"]["name"].startswith("bbr-"): continue
    ready=[c for c in p.get("status",{}).get("conditions",[]) if c.get("type")=="Ready"]
    out.append({"name":p["metadata"]["name"],
                "action":p["spec"].get("validationFailureAction"),
                "ready":(ready[0]["status"] if ready else "?")})
print(json.dumps(out))' || echo '[]'),"

echo "\"netpol\": $(kubectl get cnp -n bbr-demo -o json 2>/dev/null | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except: print("[]"); raise SystemExit
print(json.dumps([{"name":x["metadata"]["name"],
                   "created":x["metadata"].get("creationTimestamp")}
                  for x in d.get("items",[])]))' || echo '[]'),"

# ---- SLI probe ----
CODES=""; TIMES=""
for i in 1 2 3 4 5 6 7 8; do
  r=$(curl -s -o /dev/null -w '%{http_code}:%{time_total}' --max-time 6 http://127.0.0.1:30080/ 2>/dev/null)
  CODES="${CODES}${CODES:+,}\"${r%%:*}\""
  TIMES="${TIMES}${TIMES:+,}${r##*:}"
done
echo "\"sli\": {\"codes\": [${CODES}], \"times\": [${TIMES}]},"

# ---- hubble verdicts (network plane) ----
echo "\"verdicts\": $(curl -s --max-time 8 --data-urlencode \
  'query=sum by (verdict) (hubble_flows_processed_total)' \
  http://127.0.0.1:30900/api/v1/query 2>/dev/null | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except: print("{}"); raise SystemExit
print(json.dumps({r["metric"].get("verdict","?"): float(r["value"][1])
                  for r in d.get("data",{}).get("result",[])}))' || echo '{}'),"

echo "\"drops\": $(curl -s --max-time 8 --data-urlencode \
  'query=sum by (reason) (increase(hubble_drop_total[10m]))' \
  http://127.0.0.1:30900/api/v1/query 2>/dev/null | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except: print("{}"); raise SystemExit
print(json.dumps({r["metric"].get("reason","?"): round(float(r["value"][1]))
                  for r in d.get("data",{}).get("result",[])
                  if float(r["value"][1])>0.5}))' || echo '{}'),"

# ---- experiments (OPEN files only - sealed truth is unreadable here) ----
echo "\"experiments\": $(python3 -c '
import json,os,glob
out=[]
for f in sorted(glob.glob("/var/lib/bbr/experiments/open/*.incident.json")):
    try:
        d=json.load(open(f))
        eid=d["experiment_id"]
        dp=f"/var/lib/bbr/experiments/diagnoses/{eid}.diagnosis.json"
        d["diagnosed"]=os.path.exists(dp)
        if d["diagnosed"]:
            try: d["diagnosis"]=json.load(open(dp))
            except Exception: pass
        out.append(d)
    except Exception: pass
print(json.dumps(out))' 2>/dev/null || echo '[]'),"

# ---- scorecard (published by the breaker; contains verdicts, not answers) ----
echo "\"scorecard\": $(cat /var/lib/bbr/experiments/scores/scorecard.json 2>/dev/null || echo 'null'),"

# ---- kernel / boot / failed units (crash forensics) ----
echo "\"kernel_events\": $(sudo journalctl -k -p warning --since '-2h' -o json --no-pager 2>/dev/null \
  | python3 -c '
import json,sys
out=[]
for line in sys.stdin:
    try: e=json.loads(line)
    except: continue
    out.append({"t":e.get("__REALTIME_TIMESTAMP"),"msg":(e.get("MESSAGE") or "")[:180]})
print(json.dumps(out[-40:]))' 2>/dev/null || echo '[]'),"

OOM=$(sudo journalctl -k --since '-24h' --no-pager 2>/dev/null | grep -ci 'out of memory\|oom-kill')
echo "\"oom_events\": ${OOM:-0},"

FU=$(systemctl list-units --state=failed --no-legend --no-pager --plain 2>/dev/null \
  | awk '{printf "%s\"%s\"", (NR>1?",":""), $1}')
echo "\"failed_units\": [${FU}],"

echo "\"boot_count\": $(sudo journalctl --list-boots --no-pager 2>/dev/null | wc -l)"
echo "}"
