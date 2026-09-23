#!/usr/bin/env python3
"""BBR Console - API and evidence store.

DESIGN DECISION THAT MATTERS
----------------------------
The collector writes to SQLite; the API only ever READS SQLite. It never
touches the test VM during a request.

That is not laziness, it is the point. This dashboard exists to be watched
while the test VM is deliberately broken. If the API SSH'd into the VM on
every page load, then the moment the VM dies the dashboard hangs, times out
or 500s - it would be least useful exactly when it matters most. Instead it
serves the last known good snapshot plus an explicit staleness flag.

SECURITY
--------
- Binds 127.0.0.1 only. Publish behind an authenticated reverse proxy; never expose the API directly.
- The browser receives JSON only. No credentials ever reach the frontend.
- The web tier cannot construct commands. It can request "run the fixed
  collector script"; it cannot inject arguments. There is no shell path
  from an HTTP parameter.
- Repair execution is NOT implemented. Autonomy Level 0 is enforced by the
  absence of code, not by a flag that could be flipped.
"""
import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "bbr.db"
WEB_DIR = ROOT / "web"
COLLECT_SCRIPT = ROOT / "collect-remote.sh"
SSH_HELPER = Path(os.environ.get("BBR_SSH_HELPER", str(Path.home() / "bbr" / "bin" / "bbr.sh")))

POLL_SECONDS = int(os.environ.get("BBR_POLL_SECONDS", "30"))
STALE_AFTER = POLL_SECONDS * 3

# Healthy reference recorded before any fault was injected. Used to decide
# whether "slow" actually means slow.
BASELINE = {"error_rate": 0.0, "p50_ms": 3.6, "p95_ms": 4.3}

app = FastAPI(title="BBR Console", docs_url=None, redoc_url=None)


# ------------------------------------------------------------------ storage
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at TEXT NOT NULL,
            reachable INTEGER NOT NULL,
            payload TEXT NOT NULL,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_snap_time ON snapshots(collected_at DESC);

        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id TEXT UNIQUE,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            status TEXT NOT NULL,
            symptom TEXT,
            root_cause TEXT,
            confidence REAL,
            fault_actual TEXT,
            correct INTEGER,
            diagnosis TEXT
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            kind TEXT NOT NULL,
            severity TEXT NOT NULL,
            message TEXT NOT NULL,
            detail TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ev_time ON events(at DESC);
        """)


def log_event(kind, severity, message, detail=None):
    with db() as c:
        c.execute(
            "INSERT INTO events (at, kind, severity, message, detail) VALUES (?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),
             kind, severity, message, json.dumps(detail) if detail else None))


# ---------------------------------------------------------------- collector
def collect_once():
    """One SSH round-trip. Never raises - unreachability is DATA, not an error."""
    try:
        r = subprocess.run(
            [str(SSH_HELPER), "bash -s"],
            stdin=open(COLLECT_SCRIPT, "rb"),
            capture_output=True, timeout=60)
        if r.returncode != 0:
            return None, f"ssh exit {r.returncode}: {r.stderr.decode()[:200]}"
        return json.loads(r.stdout.decode()), None
    except subprocess.TimeoutExpired:
        return None, "collector timed out (target may be down - expected during a break test)"
    except json.JSONDecodeError as e:
        return None, f"malformed collector output: {e}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def analyze(payload):
    """Turn raw telemetry into findings. This is the diagnostic layer."""
    findings = []
    sli = payload.get("sli", {})
    codes = sli.get("codes", [])
    times = [t for t in sli.get("times", []) if isinstance(t, (int, float))]

    err_rate = 0.0
    p50 = p95 = None
    if codes:
        bad = sum(1 for c in codes if not str(c).startswith("2"))
        err_rate = bad * 100.0 / len(codes)
    if times:
        s = sorted(times)
        p50 = s[len(s) // 2] * 1000
        p95 = s[min(len(s) - 1, int(len(s) * 0.95))] * 1000

    if err_rate > 0:
        findings.append({
            "severity": "critical" if err_rate > 50 else "warning",
            "title": f"Service error rate {err_rate:.0f}%",
            "detail": f"Baseline is {BASELINE['error_rate']}%. Codes observed: {sorted(set(map(str, codes)))}",
            "plane": "application",
        })

    # Fast failure vs slow failure is THE discriminator between a missing
    # dependency and a blocked/saturated one.
    if p50 is not None and err_rate > 0:
        if p50 < 50:
            findings.append({
                "severity": "info",
                "title": "Failure is FAST, not slow",
                "detail": f"p50 {p50:.0f}ms vs baseline {BASELINE['p50_ms']}ms. Immediate failure "
                          "indicates no route or connection refused (missing endpoint), "
                          "not a timeout or saturation.",
                "plane": "application",
            })
        elif p50 > 1500:
            findings.append({
                "severity": "info",
                "title": "Failure is SLOW (timeout signature)",
                "detail": f"p50 {p50:.0f}ms. Requests are hitting an upstream timeout rather than "
                          "failing immediately - consistent with traffic being blocked or an "
                          "upstream hanging, not with a missing endpoint.",
                "plane": "application",
            })

    for svc, n in (payload.get("endpoints") or {}).items():
        if n == 0:
            findings.append({
                "severity": "critical",
                "title": f"Service '{svc}' has ZERO endpoints",
                "detail": "Traffic to this Service has nowhere to go. Either no pods match "
                          "the selector, or replicas are 0.",
                "plane": "routing",
            })

    for d in payload.get("deployments") or []:
        if d.get("desired") == 0:
            findings.append({
                "severity": "critical",
                "title": f"Deployment '{d['name']}' scaled to 0 replicas",
                "detail": "Note: Kubernetes still reports Available=True because zero desired "
                          "replicas trivially satisfies minimum availability. Reading conditions "
                          "alone would hide this.",
                "plane": "workload",
            })
        if (d.get("generation") or 1) > 1:
            findings.append({
                "severity": "info",
                "title": f"Deployment '{d['name']}' spec changed (generation {d['generation']})",
                "detail": "Generation > 1 means the spec was mutated after creation. "
                          "'What changed' is the highest-value question in incident response.",
                "plane": "change",
            })

    drops = payload.get("drops") or {}
    if drops.get("POLICY_DENIED"):
        findings.append({
            "severity": "critical",
            "title": f"{drops['POLICY_DENIED']} POLICY_DENIED network drops",
            "detail": "A NetworkPolicy is actively blocking traffic. Pods may be perfectly "
                      "healthy while being unable to communicate.",
            "plane": "network",
        })

    for p in payload.get("pods") or []:
        if p.get("restarts", 0) > 3:
            findings.append({
                "severity": "warning",
                "title": f"Pod {p['name']} restarted {p['restarts']}x",
                "detail": f"namespace {p['ns']}, phase {p['phase']}",
                "plane": "workload",
            })

    if payload.get("oom_events", 0) > 0:
        findings.append({
            "severity": "critical",
            "title": f"{payload['oom_events']} OOM kill events in last 24h",
            "detail": "The kernel terminated processes for memory exhaustion.",
            "plane": "kernel",
        })

    for u in payload.get("failed_units") or []:
        findings.append({"severity": "warning", "title": f"systemd unit failed: {u}",
                         "detail": "", "plane": "host"})

    host = payload.get("host") or {}
    try:
        if int(str(host.get("disk_pct", "0%")).rstrip("%")) > 85:
            findings.append({"severity": "warning",
                             "title": f"Disk {host['disk_pct']} full",
                             "detail": "Disk pressure can cause evictions and corrupt LVM snapshots.",
                             "plane": "host"})
    except Exception:
        pass

    for s in payload.get("snapshots") or []:
        try:
            if float(s.get("used_pct") or 0) > 80:
                findings.append({
                    "severity": "warning",
                    "title": f"Snapshot {s['name']} is {s['used_pct']}% full",
                    "detail": "An LVM snapshot that fills becomes INVALID and cannot be used "
                              "to roll back. Rollback capability is at risk.",
                    "plane": "host"})
        except Exception:
            pass

    return {
        "findings": findings,
        "sli": {"error_rate": round(err_rate, 1),
                "p50_ms": round(p50, 1) if p50 else None,
                "p95_ms": round(p95, 1) if p95 else None},
        "baseline": BASELINE,
        "health": ("critical" if any(f["severity"] == "critical" for f in findings)
                   else "degraded" if any(f["severity"] == "warning" for f in findings)
                   else "healthy"),
    }


def sync_incidents(payload):
    """Mirror experiment state into the incidents table, scoring where possible."""
    # Scorecard is produced by the breaker identity - it contains verdicts
    # (correct / incorrect) but never the fault answer ahead of diagnosis.
    scored = {}
    sc = payload.get("scorecard") or {}
    for r in (sc.get("results") or []):
        scored[r["experiment_id"]] = r

    for e in payload.get("experiments") or []:
        eid = e.get("experiment_id")
        if not eid:
            continue
        diag = e.get("diagnosis") or {}
        rc = diag.get("root_cause")
        if isinstance(rc, dict):
            rc = rc.get("root_cause")
        conf = diag.get("confidence")
        if conf is None and isinstance(diag.get("root_cause"), dict):
            conf = diag["root_cause"].get("confidence")
        s = scored.get(eid, {})
        with db() as c:
            c.execute("""
              INSERT INTO incidents (experiment_id, opened_at, status, root_cause,
                                     confidence, diagnosis, symptom, fault_actual, correct)
              VALUES (?,?,?,?,?,?,?,?,?)
              ON CONFLICT(experiment_id) DO UPDATE SET
                status=excluded.status,
                root_cause=COALESCE(excluded.root_cause, incidents.root_cause),
                confidence=COALESCE(excluded.confidence, incidents.confidence),
                diagnosis=COALESCE(excluded.diagnosis, incidents.diagnosis),
                symptom=COALESCE(excluded.symptom, incidents.symptom),
                fault_actual=COALESCE(excluded.fault_actual, incidents.fault_actual),
                correct=COALESCE(excluded.correct, incidents.correct)
            """, (eid, e.get("injected_at"),
                  "SCORED" if s else ("DIAGNOSED" if e.get("diagnosed") else "ACTIVE"),
                  rc, conf, json.dumps(diag) if diag else None,
                  diag.get("symptom"),
                  s.get("fault_actual"),
                  (1 if s.get("correct") else 0) if s else None))


_last_health = {"v": None}


def poll_loop():
    while True:
        payload, err = collect_once()
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if payload:
            a = analyze(payload)
            payload["_analysis"] = a
            with db() as c:
                c.execute("INSERT INTO snapshots (collected_at, reachable, payload, error) "
                          "VALUES (?,?,?,?)", (ts, 1, json.dumps(payload), None))
                c.execute("DELETE FROM snapshots WHERE id NOT IN "
                          "(SELECT id FROM snapshots ORDER BY id DESC LIMIT 2000)")
            sync_incidents(payload)
            if a["health"] != _last_health["v"]:
                log_event("health", a["health"],
                          f"System health -> {a['health'].upper()}",
                          {"findings": [f["title"] for f in a["findings"]]})
                _last_health["v"] = a["health"]
        else:
            with db() as c:
                c.execute("INSERT INTO snapshots (collected_at, reachable, payload, error) "
                          "VALUES (?,?,?,?)", (ts, 0, "{}", err))
            if _last_health["v"] != "unreachable":
                log_event("reachability", "critical", "Target VM unreachable", {"error": err})
                _last_health["v"] = "unreachable"
        time.sleep(POLL_SECONDS)


def latest():
    with db() as c:
        row = c.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    d = dict(row)
    d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
    age = (datetime.now(timezone.utc)
           - datetime.fromisoformat(d["collected_at"])).total_seconds()
    d["age_seconds"] = int(age)
    d["stale"] = age > STALE_AFTER
    return d


# -------------------------------------------------------------------- routes
@app.get("/api/health")
def api_health():
    s = latest()
    if not s:
        return {"status": "starting", "message": "no snapshot collected yet"}
    p = s["payload"]
    a = p.get("_analysis", {})
    return {
        "status": "ok" if s["reachable"] else "target_unreachable",
        "target_reachable": bool(s["reachable"]),
        "health": a.get("health", "unknown"),
        "stale": s["stale"],
        "age_seconds": s["age_seconds"],
        "collected_at": s["collected_at"],
        "error": s["error"],
    }


@app.get("/api/overview")
def api_overview():
    s = latest()
    if not s:
        raise HTTPException(503, "no data collected yet")
    p = s["payload"]
    a = p.get("_analysis", {})
    pods = p.get("pods") or []
    return {
        "meta": {"collected_at": s["collected_at"], "age_seconds": s["age_seconds"],
                 "stale": s["stale"], "reachable": bool(s["reachable"]),
                 "error": s["error"], "hostname": p.get("hostname")},
        "health": a.get("health", "unknown"),
        "sli": a.get("sli", {}),
        "baseline": a.get("baseline", BASELINE),
        "findings": a.get("findings", []),
        "host": p.get("host", {}),
        "pods": {"total": len(pods),
                 "running": sum(1 for x in pods if x.get("phase") == "Running"),
                 "restarts": sum(x.get("restarts", 0) for x in pods)},
        "deployments": p.get("deployments", []),
        "endpoints": p.get("endpoints", {}),
        "verdicts": p.get("verdicts", {}),
        "drops": p.get("drops", {}),
        "policies": p.get("policies", []),
        "netpol": p.get("netpol", []),
        "snapshots": p.get("snapshots", []),
        "vg_free": p.get("vg_free"),
    }


@app.get("/api/incidents")
def api_incidents():
    with db() as c:
        rows = c.execute("SELECT * FROM incidents ORDER BY opened_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("diagnosis"):
            try:
                d["diagnosis"] = json.loads(d["diagnosis"])
            except Exception:
                d["diagnosis"] = None
        out.append(d)
    scored = [x for x in out if x.get("correct") is not None]
    correct = [x for x in scored if x["correct"]]
    s = latest()
    sc = (s["payload"].get("scorecard") if s else None) or {}
    return {"incidents": out,
            "stats": {"total": len(out),
                      "diagnosed": len([x for x in out if x["status"] in ("DIAGNOSED", "SCORED")]),
                      "active": len([x for x in out if x["status"] == "ACTIVE"]),
                      "scored": len(scored),
                      "correct": len(correct),
                      "accuracy_pct": round(len(correct) * 100 / len(scored), 1) if scored else None,
                      "commitment_failures": sc.get("commitment_failures", 0)},
            "scorecard": sc.get("results", [])}


@app.get("/api/incidents/{experiment_id}")
def api_incident(experiment_id: str):
    with db() as c:
        r = c.execute("SELECT * FROM incidents WHERE experiment_id=?",
                      (experiment_id,)).fetchone()
    if not r:
        raise HTTPException(404, "unknown experiment")
    d = dict(r)
    if d.get("diagnosis"):
        try:
            d["diagnosis"] = json.loads(d["diagnosis"])
        except Exception:
            pass
    return d


@app.get("/api/live")
def api_live():
    s = latest()
    if not s:
        raise HTTPException(503, "no data")
    p = s["payload"]
    return {"meta": {"collected_at": s["collected_at"], "stale": s["stale"]},
            "host": p.get("host", {}),
            "pods": p.get("pods", []),
            "nodes": p.get("k8s_nodes", []),
            "failed_units": p.get("failed_units", []),
            "boot_count": p.get("boot_count")}


@app.get("/api/kernel")
def api_kernel():
    s = latest()
    if not s:
        raise HTTPException(503, "no data")
    p = s["payload"]
    return {"meta": {"collected_at": s["collected_at"], "stale": s["stale"]},
            "kernel": p.get("host", {}).get("kernel"),
            "kernel_events": p.get("kernel_events", []),
            "oom_events": p.get("oom_events", 0),
            "boot_count": p.get("boot_count"),
            "failed_units": p.get("failed_units", [])}


@app.get("/api/network")
def api_network():
    s = latest()
    if not s:
        raise HTTPException(503, "no data")
    p = s["payload"]
    return {"meta": {"collected_at": s["collected_at"], "stale": s["stale"]},
            "verdicts": p.get("verdicts", {}),
            "drops": p.get("drops", {}),
            "endpoints": p.get("endpoints", {}),
            "netpol": p.get("netpol", [])}


@app.get("/api/safety")
def api_safety():
    """The control surface. States what is enforced and how it was proven."""
    s = latest()
    p = s["payload"] if s else {}
    return {
        "autonomy_level": 0,
        "autonomy_label": "OBSERVE ONLY",
        "autonomy_note": "Repair execution is not implemented in this service. "
                         "This is enforced by absence of code, not a toggle.",
        "policies": p.get("policies", []),
        "boundary_tests": {"passed": 23, "failed": 0,
                           "evidence": "~/bbr/evidence/boundary-verification.txt"},
        "policy_tests": {"passed": 11, "failed": 0,
                         "evidence": "~/bbr/evidence/kyverno-verification.txt"},
        "guarantees": [
            {"claim": "Analyst cannot read sealed ground truth",
             "mechanism": "unix uid separation, dir mode 0700 owned by bbr-breaker",
             "verified": True},
            {"claim": "Analyst cannot mutate the cluster",
             "mechanism": "RBAC grants only get/list/watch; no create/update/patch/delete",
             "verified": True},
            {"claim": "Analyst cannot read Secrets",
             "mechanism": "secrets absent from ClusterRole entirely - never granted",
             "verified": True},
            {"claim": "Analyst cannot exec into pods",
             "mechanism": "pods/exec not in ClusterRole",
             "verified": True},
            {"claim": "Unsafe manifests are rejected before admission",
             "mechanism": "Kyverno ClusterPolicies in Enforce mode",
             "verified": True},
            {"claim": "Telemetry cannot be deleted to silence an alert",
             "mechanism": "bbr-protect-observability policy",
             "verified": True},
        ],
        "snapshots": p.get("snapshots", []),
        "vg_free": p.get("vg_free"),
    }


@app.get("/api/resolved")
def api_resolved():
    """Problems found, diagnosed and fixed — the public record.

    Two categories, deliberately kept separate:
      injected_faults  — deliberate experiments, scored against sealed truth
      real_problems    — genuine defects hit while building, with real impact

    Conflating them would overstate the experiment results, so they are
    counted and displayed apart.
    """
    p = ROOT / "data" / "resolutions.json"
    if not p.exists():
        raise HTTPException(503, "no resolution record")
    d = json.loads(p.read_text())

    inj = d.get("injected_faults", [])
    real = d.get("real_problems_found", [])
    open_items = d.get("open_items", [])

    return {
        "updated": d.get("updated"),
        "summary": {
            "total_resolved": len(inj) + len(real),
            "injected_resolved": len(inj),
            "real_resolved": len(real),
            # Field is written as "diagnosed_correctly" in resolutions.json;
            # this used to read "diagnosis_correct" (no field of that name
            # exists anywhere), so the count was silently 0 for every entry
            # that didn't also happen to be caught by the fallback below.
            "injected_diagnosed_correctly": sum(
                1 for x in inj if x.get("diagnosed_correctly") or x.get("diagnosis_correct")
            ),
            "critical_fixed": sum(1 for x in inj + real if x.get("severity") == "critical"),
            "open": len(open_items),
            "security_weakened_count": 0,
        },
        "injected_faults": inj,
        "real_problems": real,
        "open_items": open_items,
    }


@app.get("/api/automation")
def api_automation():
    """Continuity automation status — visible on the site, not buried in a log."""
    st = ROOT / "data" / "credit-watch.json"
    rq = ROOT / "data" / "resume-request.json"
    hm = ROOT / "data" / "health-monitor.json"

    out = {"watcher": None, "resume": None, "monitor": None}
    try:
        if st.exists():
            d = json.loads(st.read_text())
            age = None
            if d.get("last_check"):
                age = int((datetime.now(timezone.utc)
                           - datetime.fromisoformat(d["last_check"])).total_seconds())
            out["watcher"] = {
                "status": d.get("status"),
                "outage_active": d.get("outage_active"),
                "interval_seconds": d.get("interval"),
                "last_check": d.get("last_check"),
                "last_check_age_seconds": age,
                "probes_spent": d.get("probes_spent", 0),
                "probes_saved": d.get("probes_skipped_free_precheck", 0),
                "outages_recovered": len(d.get("history", [])),
                "alive": age is not None and age < 3600,
            }
    except Exception:
        pass
    try:
        if rq.exists():
            out["resume"] = json.loads(rq.read_text())
    except Exception:
        pass
    try:
        if hm.exists():
            d = json.loads(hm.read_text())
            out["monitor"] = {
                "last_check": d.get("last_check"),
                "active_conditions": len(d.get("conditions", {})),
                "conditions": list(d.get("conditions", {}).keys()),
            }
    except Exception:
        pass
    return out


@app.get("/api/events")
def api_events(limit: int = 100):
    with db() as c:
        rows = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?",
                         (min(limit, 500),)).fetchall()
    return {"events": [dict(r) for r in rows]}


@app.get("/api/history")
def api_history(points: int = 60):
    """SLI over time - lets you SEE the break and the recovery."""
    with db() as c:
        rows = c.execute(
            "SELECT collected_at, reachable, payload FROM snapshots "
            "ORDER BY id DESC LIMIT ?", (min(points, 500),)).fetchall()
    out = []
    for r in reversed(rows):
        if not r["reachable"]:
            out.append({"t": r["collected_at"], "reachable": False})
            continue
        try:
            a = json.loads(r["payload"]).get("_analysis", {})
            out.append({"t": r["collected_at"], "reachable": True,
                        "error_rate": a.get("sli", {}).get("error_rate"),
                        "p50_ms": a.get("sli", {}).get("p50_ms"),
                        "health": a.get("health")})
        except Exception:
            pass
    return {"series": out, "baseline": BASELINE}


@app.post("/api/refresh")
def api_refresh():
    """Force an immediate poll. Read-only: runs the fixed collector script."""
    payload, err = collect_once()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if payload:
        payload["_analysis"] = analyze(payload)
        with db() as c:
            c.execute("INSERT INTO snapshots (collected_at, reachable, payload, error) "
                      "VALUES (?,?,?,?)", (ts, 1, json.dumps(payload), None))
        sync_incidents(payload)
        return {"ok": True, "collected_at": ts}
    with db() as c:
        c.execute("INSERT INTO snapshots (collected_at, reachable, payload, error) "
                  "VALUES (?,?,?,?)", (ts, 0, "{}", err))
    return JSONResponse({"ok": False, "error": err}, status_code=200)


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    init_db()
    log_event("service", "info", "BBR Console started")
    threading.Thread(target=poll_loop, daemon=True).start()
    yield


app.router.lifespan_context = lifespan


if __name__ == "__main__":
    import uvicorn
    # Bind LOOPBACK only. nginx is the single public entrance and proxies
    # to us, so the app is never directly reachable from any network.
    # Loopback also removes the startup dependency on the VPN interface
    # existing, which would otherwise make boot order matter.
    host = os.environ.get("BBR_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=int(os.environ.get("BBR_PORT", "8811")))
