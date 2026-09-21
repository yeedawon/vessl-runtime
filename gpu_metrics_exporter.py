#!/usr/bin/env python3
"""
VESSL 워크스페이스 GPU 메트릭 exporter

Prometheus 가 긁어갈 수 있게 GPU 상태를 텍스트 포맷으로 노출한다.
워크스페이스 안에 상주하며, bootstrap 이 VESSL_METRICS_PORT 가 설정된 경우에만 띄운다.

왜 DCGM Exporter 가 아닌가
    DCGM 이 VESSL 컨테이너에서 드라이버 접근 없이 뜨는지 미확인이다.
    반면 gpu_idle_watchdog.py 의 nvidia-smi 파싱은 이미 검증돼 있다.
    그래서 watchdog 을 import 해 같은 함수를 쓴다 — 판정 기준이 갈리지 않는다.

⚠️ 이 포트는 인터넷에 열린다
    workspace 의 --port ...:tcp 는 프록시를 거치지 않아 인증이 없다.
    그래서 exporter 가 직접 Bearer 토큰을 검사한다.
    토큰이 없으면 뜨지 않는다(--insecure 로만 우회 가능).

사용
    python3 gpu_metrics_exporter.py --port 9400 --workspace <slug> --owner <이름>
    # 토큰은 VESSL_METRICS_TOKEN 환경변수

확인
    curl -H "Authorization: Bearer $VESSL_METRICS_TOKEN" localhost:9400/metrics

제어 엔드포인트 (2026-09-05 추가 — PROJECT_CONTEXT §6 과제 4 "B 구조")
    같은 포트·같은 토큰으로 이 워크스페이스 *자기 자신*을 pause / terminate 한다.
    KETI 의 Slack 디스패처(vessl_slack_app.py)가 여기로 중계만 하고, 실행 자격은
    워크스페이스 안의 vesslctl 인증(소유자)이다. 그래서 KETI 에는 vesslctl 이 필요 없다.
        POST /pause      {"by": "<slack user id>"}   → vesslctl workspace pause <slug>
        POST /terminate  {"by": "<slack user id>"}   → vesslctl workspace terminate <slug> -y
        GET  /info                                   → slug·owner·인증 여부
    인증이 없으면 409 를 돌려주고, Slack 에는 "워크스페이스 안에서 로그인" 안내가 간다.
    VESSL_CONTROL=0 이면 제어 엔드포인트를 끈다(메트릭만).

Grafana Cloud push (2026-09-07 추가 — KETI 는 아웃바운드 80·443 뿐이라 pull 이 불가, §7)
    GRAFANA_PUSH_URL · GRAFANA_PUSH_USER(인스턴스 ID) · GRAFANA_PUSH_TOKEN(metrics:write) 이 모두 있으면
    GRAFANA_PUSH_INTERVAL(기본 30초)마다 render() 결과를 Influx line protocol 로 바꿔 HTTPS POST 한다.
    Basic 인증(ID:토큰). 타임스탬프는 붙이지 않는다(서버 수신 시각). 실패해도 죽지 않고 카운터만 올린다.
    셋 중 하나라도 없으면 push 는 조용히 꺼진다(메트릭 포트는 그대로).

기동 시 등록 게시
    vesslctl 인증이 있으면 `workspace show` 로 공개 host:port 를 읽어 Slack Webhook 에
    "등록" 한 줄을 남긴다. 디스패처가 채널 기록에서 이 줄을 읽어 중계 대상을 안다.
    인증이 없으면 생략한다 — create_workspace.sh 가 생성 직후 같은 형식으로 게시한다.
"""

import argparse
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
import base64
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# watchdog 과 같은 판정 로직을 쓴다. bootstrap 이 /root 에 배치한다.
sys.path.insert(0, "/root")
try:
    import gpu_idle_watchdog as _wd
except Exception:                                     # 없으면 자체 구현으로 폴백
    _wd = None

TOKEN = os.environ.get("VESSL_METRICS_TOKEN", "")
WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
CONTROL = os.environ.get("VESSL_CONTROL", "1") != "0"
PUSH_URL = os.environ.get("GRAFANA_PUSH_URL", "").strip()
PUSH_USER = os.environ.get("GRAFANA_PUSH_USER", "").strip()
PUSH_TOKEN = os.environ.get("GRAFANA_PUSH_TOKEN", "").strip()
PUSH_INTERVAL = float(os.environ.get("GRAFANA_PUSH_INTERVAL") or 30)
_push_failures = 0
# 크레딧 (2026-09-08): create_workspace.sh 가 spec 의 시간당 크레딧을 VESSL_HOURLY_CREDITS 로 넘긴다.
# render() 가 호출될 때마다 경과 시간 × 시간당 크레딧을 누적한다(총·유휴 별도). Grafana 에서 increase() 로 기간별 사용·낭비 크레딧.
try:
    HOURLY_CREDITS = float(os.environ.get("VESSL_HOURLY_CREDITS") or 0)
except ValueError:
    HOURLY_CREDITS = 0.0
SPEC_SLUG = os.environ.get("VESSL_SPEC_SLUG", "")
_credit_last_ts = None
_credits_total = 0.0
_credits_idle = 0.0
IDLE_UTIL = 5.0
_scrape_errors = 0
_cpu_prev = None                                      # (total, idle) — 스크레이프 간 델타


# ─────────────────────────── 수집 ───────────────────────────

def gpu_rows() -> list:
    """GPU 별 지표. watchdog 의 query 에 전력을 더했다."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,"
             "temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"], text=True, timeout=15)
    except Exception:
        return []
    rows = []
    for line in out.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 7:
            continue
        try:
            rows.append({
                "idx": f[0], "name": f[1],
                "util": float(f[2]),
                "used_mib": float(f[3]), "total_mib": float(f[4]),
                "temp": float(f[5]),
                # power.draw 는 일부 카드에서 [N/A] 로 나온다
                "power": float(f[6]) if f[6].replace(".", "").isdigit() else -1.0,
            })
        except ValueError:
            continue
    return rows


def compute_procs() -> int:
    if _wd is not None:
        try:
            return _wd.compute_procs()
        except Exception:
            return -1
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"],
            text=True, timeout=15)
        return len([ln for ln in out.strip().splitlines() if ln.strip()])
    except Exception:
        return -1


def load_avg() -> float:
    if _wd is not None:
        try:
            return _wd.load_avg()
        except Exception:
            pass
    try:
        return os.getloadavg()[0]
    except Exception:
        return 0.0


_cg_prev = None                                       # (cpu_seconds, wall) — 스크레이프 간 델타


def container_cpu_cores():
    """이 컨테이너(cgroup)의 CPU 사용 코어 수, 이전 스크레이프와의 델타. 못 읽으면 None."""
    global _cg_prev
    secs = None
    if _wd is not None:
        try:
            secs = _wd._cgroup_cpu_seconds()
        except Exception:
            secs = None
    if secs is None:
        try:
            with open("/sys/fs/cgroup/cpu.stat") as f:
                for line in f:
                    if line.startswith("usage_usec"):
                        secs = int(line.split()[1]) / 1e6
        except Exception:
            return None
    now = time.time()
    prev, _cg_prev = _cg_prev, (secs, now)
    if prev is None or now - prev[1] <= 0:
        return None
    return max(0.0, (secs - prev[0]) / (now - prev[1]))


def cpu_percent() -> float:
    """스크레이프 간 델타로 계산한다.

    watchdog 의 cpu_percent() 는 1초 sleep 을 쓰는데, 그대로 쓰면 매 스크레이프가
    1초씩 블록된다. 여기서는 이전 스크레이프 값과 비교해 즉시 반환한다.
    """
    global _cpu_prev
    try:
        with open("/proc/stat") as f:
            parts = [float(x) for x in f.readline().split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0.0)
        cur = (sum(parts), idle)
    except Exception:
        return -1.0
    prev, _cpu_prev = _cpu_prev, cur
    if prev is None:
        return -1.0                                   # 첫 스크레이프는 기준점이 없다
    dt, di = cur[0] - prev[0], cur[1] - prev[1]
    if dt <= 0:
        return -1.0
    return max(0.0, min(100.0, (1 - di / dt) * 100))


# ─────────────────────────── 포맷 ───────────────────────────

def esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def num(v: float) -> str:
    """Prometheus 값 포맷.

    %g 를 쓰면 유효숫자 6자리에서 잘려 VRAM 같은 큰 값이 어긋난다
    (65,372,487,680 → 6.53735e+10 → 65,373,500,000). 정수는 정수로 낸다.
    """
    if v == int(v):
        return str(int(v))
    return f"{v:.4f}".rstrip("0").rstrip(".")


WS_NAME = os.environ.get("VESSL_WS_NAME", "")


def render(workspace: str, owner: str) -> str:
    global _scrape_errors
    # 워크스페이스 이름은 ws_name — GPU 별 지표의 name(모델명) 라벨과 충돌하지 않게(2026-09-10 실측: 일간 보고 이름 자리에 GPU 모델명이 들어갔다)
    base = f'workspace="{esc(workspace)}",owner="{esc(owner)}"' + (f',ws_name="{esc(WS_NAME)}"' if WS_NAME else "")
    out = []

    def add(name, typ, help_, samples):
        if not samples:
            return
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {typ}")
        out.extend(samples)

    rows = gpu_rows()
    if not rows:
        _scrape_errors += 1

    for key, metric, typ, help_, scale in (
        ("util",      "vessl_gpu_utilization_percent", "gauge", "GPU 사용률(%)", 1),
        ("used_mib",  "vessl_gpu_memory_used_bytes",   "gauge", "VRAM 사용량", 1024 * 1024),
        ("total_mib", "vessl_gpu_memory_total_bytes",  "gauge", "VRAM 총량", 1024 * 1024),
        ("temp",      "vessl_gpu_temperature_celsius", "gauge", "GPU 온도", 1),
        ("power",     "vessl_gpu_power_watts",         "gauge", "GPU 전력(W)", 1),
    ):
        samples = []
        for r in rows:
            v = r[key]
            if v < 0:                                  # power [N/A] 등은 내보내지 않는다
                continue
            lbl = f'{base},gpu="{esc(r["idx"])}",name="{esc(r["name"])}"'
            samples.append(f"{metric}{{{lbl}}} {num(v * scale)}")
        add(metric, typ, help_, samples)

    procs = compute_procs()
    if procs >= 0:
        add("vessl_gpu_compute_processes", "gauge", "GPU 연산 프로세스 수",
            [f"vessl_gpu_compute_processes{{{base}}} {procs}"])

    cpu = cpu_percent()
    if cpu >= 0:
        add("vessl_cpu_utilization_percent", "gauge", "호스트 전체 CPU 사용률(%) — 컨테이너 안에서는 노드 값",
            [f"vessl_cpu_utilization_percent{{{base}}} {cpu:.2f}"])
    cores = container_cpu_cores()
    if cores is not None:
        add("vessl_container_cpu_cores", "gauge", "이 워크스페이스가 쓰는 CPU 코어 수 (cgroup, 스크레이프 간 평균)",
            [f"vessl_container_cpu_cores{{{base}}} {cores:.3f}"])

    add("vessl_load_average_1m", "gauge", "1분 load average — 단일 스레드 작업 감지용",
        [f"vessl_load_average_1m{{{base}}} {load_avg():.2f}"])
    add("vessl_gpu_count", "gauge", "감지된 GPU 개수",
        [f"vessl_gpu_count{{{base}}} {len(rows)}"])
    add("vessl_exporter_scrape_errors_total", "counter", "nvidia-smi 수집 실패 누적",
        [f"vessl_exporter_scrape_errors_total{{{base}}} {_scrape_errors}"])
    # 크레딧: 마지막 render 이후 경과분을 누적. 유휴(사용률<5% 이고 연산 프로세스 0)면 유휴 누적에도 더한다.
    global _credit_last_ts, _credits_total, _credits_idle
    now = time.time()
    if HOURLY_CREDITS > 0:
        if _credit_last_ts is not None:
            dt_h = max(0.0, min(now - _credit_last_ts, 600.0)) / 3600.0     # 10분 넘는 공백은 잘라 낸다(정지 구간)
            inc = HOURLY_CREDITS * dt_h
            _credits_total += inc
            utils = [r["util"] for r in rows]
            if rows and max(utils) < IDLE_UTIL and procs == 0:
                _credits_idle += inc
        _credit_last_ts = now
        lbl = f'{base},spec="{esc(SPEC_SLUG)}"'
        add("vessl_workspace_hourly_credits", "gauge", "이 워크스페이스의 시간당 크레딧(spec 단가)",
            [f"vessl_workspace_hourly_credits{{{lbl}}} {HOURLY_CREDITS}"])
        add("vessl_workspace_credits_total", "counter", "기동 후 누적 사용 크레딧 (increase() 로 기간별)",
            [f"vessl_workspace_credits_total{{{base}}} {_credits_total:.6f}"])
        add("vessl_workspace_idle_credits_total", "counter", "기동 후 GPU 유휴 상태로 흘린 크레딧 누적",
            [f"vessl_workspace_idle_credits_total{{{base}}} {_credits_idle:.6f}"])

    add("vessl_exporter_push_failures_total", "counter", "Grafana push 실패 누적",
        [f"vessl_exporter_push_failures_total{{{base}}} {_push_failures}"])
    add("vessl_exporter_up", "gauge", "exporter 동작 여부",
        [f"vessl_exporter_up{{{base}}} 1"])

    return "\n".join(out) + "\n"


# ─────────────────────────── Grafana Cloud push ───────────────────────────
# Prometheus 텍스트 한 줄  metric{a="x",b="y"} 3
# → Influx line            metric,a=x,b=y value=3
# 태그 값의 공백·쉼표·등호는 백슬래시로 이스케이프한다(Influx 규칙). 빈 태그 값은 뺀다(Influx 가 거부).

_SAMPLE_RE = re.compile(r'^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<val>\S+)\s*$')
_LABEL_RE = re.compile(r'(?P<k>[A-Za-z_][A-Za-z0-9_]*)="(?P<v>(?:[^"\\]|\\.)*)"')


def _influx_tag(v: str) -> str:
    return v.replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def to_influx(prom_text: str) -> str:
    """Prometheus 텍스트 포맷 → Influx line protocol. # 줄과 파싱 불가 줄은 버린다."""
    out = []
    for line in prom_text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE_RE.match(line)
        if not m:
            continue
        try:
            val = float(m["val"])
        except ValueError:
            continue
        tags = []
        for lm in _LABEL_RE.finditer(m["labels"] or ""):
            v = lm["v"].replace('\\"', '"').replace("\\\\", "\\")
            if v != "":
                tags.append(f"{lm['k']}={_influx_tag(v)}")
        head = m["name"] + ("," + ",".join(tags) if tags else "")
        out.append(f"{head} value={num(val)}")
    return "\n".join(out) + ("\n" if out else "")


def push_once(workspace: str, owner: str) -> bool:
    """render() → Influx → POST. 성공 True. 실패는 카운터만 올리고 True/False 로 알린다."""
    global _push_failures
    try:
        body = to_influx(render(workspace, owner)).encode()
        if not body:
            return True
        auth = base64.b64encode(f"{PUSH_USER}:{PUSH_TOKEN}".encode()).decode()
        req = urllib.request.Request(
            PUSH_URL, data=body, method="POST",
            headers={"Content-Type": "text/plain; charset=utf-8", "Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            if 200 <= r.status < 300:
                return True
            raise RuntimeError(f"HTTP {r.status}")
    except urllib.error.HTTPError as e:
        _push_failures += 1
        print(f"[exporter] push 실패 HTTP {e.code}: {e.read()[:200]!r}", flush=True)
    except Exception as e:
        _push_failures += 1
        print(f"[exporter] push 실패: {e}", flush=True)
    return False


def push_loop(workspace: str, owner: str) -> None:
    """PUSH_INTERVAL 마다 push. 첫 회는 CPU 델타 기준점이 잡히도록 한 주기 뒤에 시작한다."""
    time.sleep(min(PUSH_INTERVAL, 5))
    fails = 0
    while True:
        ok = push_once(workspace, owner)
        fails = 0 if ok else fails + 1
        if fails in (3, 20):                             # 로그가 넘치지 않게 두 번만 경고
            print(f"[exporter] push 연속 실패 {fails}회 — GRAFANA_PUSH_* 값과 아웃바운드를 확인", flush=True)
        time.sleep(PUSH_INTERVAL)


def push_enabled() -> bool:
    return bool(PUSH_URL and PUSH_USER and PUSH_TOKEN)

# ─────────────────────────── 제어 · 등록 ───────────────────────────


def _inherit_pid1_env(prefixes=("VESSLCTL_",)) -> None:
    """ssh 셸 등 PID 1 환경을 물려받지 못한 곳에서 실행돼도 vesslctl 이 org/team/토큰을 찾게 한다(watchdog 과 동일)."""
    try:
        with open("/proc/1/environ", "rb") as f:
            for item in f.read().split(b"\0"):
                if b"=" not in item:
                    continue
                k, v = item.split(b"=", 1)
                k = k.decode("utf-8", "replace")
                if k.startswith(prefixes) and k not in os.environ:
                    os.environ[k] = v.decode("utf-8", "replace")
    except Exception:
        pass


_inherit_pid1_env()

def run_vesslctl(args: list, timeout: int = 60) -> tuple:
    """(rc, stdout, stderr). stdin 을 닫아 프롬프트에 걸리지 않게 한다(CLAUDE.md §4)."""
    try:
        r = subprocess.run(["vesslctl"] + args, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return 127, "", "vesslctl 을 찾을 수 없다"
    except subprocess.TimeoutExpired:
        return 124, "", f"시간 초과({timeout}s)"


def vesslctl_authed() -> bool:
    rc, _, _ = run_vesslctl(["auth", "status"], timeout=20)
    return rc == 0


def tag(owner: str) -> str:
    return f"[{owner}] " if owner else ""


def notify(text: str) -> None:
    """Slack Webhook 발송. 없으면 로그만. requests 에 의존하지 않는다(이미지에 없을 수 있다)."""
    print(f"[exporter] {text}", flush=True)
    if not WEBHOOK_URL:
        return
    try:
        req = urllib.request.Request(
            WEBHOOK_URL, data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[exporter] WARN: Slack 발송 실패 — {e}", flush=True)


def public_endpoint(slug: str, port_name: str = "metrics") -> str:
    """`workspace show` 의 endpoints[] 에서 이 포트의 공개 host:port. 못 읽으면 빈 문자열.

    실측 스키마(HANDOFF_connect.md §1.3): endpoints 는 최상위, 항목은
    {"host","name","port","protocol","targetPort"}. 필드명은 단정하지 않고 후보를 둔다.
    """
    rc, out, _ = run_vesslctl(["workspace", "show", slug, "-o", "json"], timeout=30)
    if rc != 0 or not out:
        return ""
    try:
        data = json.loads(out)
    except Exception:
        return ""
    eps = data.get("endpoints") or (data.get("workspace") or {}).get("endpoints") or []
    for e in eps:
        if not isinstance(e, dict):
            continue
        if e.get("name") == port_name and e.get("host") and e.get("port"):
            return f"{e['host']}:{e['port']}"
    return ""


# 디스패처(vessl_slack_app.py 의 REG_RE)가 이 형식을 읽는다. 바꾸면 양쪽을 함께 바꿀 것.
def registration_line(slug: str, owner: str, endpoint: str, name: str = "") -> str:
    line = f":satellite: {tag(owner)}등록 slug={slug} ctl={endpoint}"
    if name:
        line += f" name={name}"
    return line


def register(slug: str, owner: str, tries: int = 12, wait: float = 30.0) -> None:
    """기동 시 등록. endpoints 는 기동 직후 아직 배정되지 않았을 수 있어(2026-09-08 실측) 최대 tries 회 재시도한다.
    인증이 없으면(workload 토큰 미주입 등) 끝까지 못 읽고 생략 — create_workspace.sh 의 게시에 의존."""
    if slug in ("", "unknown"):
        print("[exporter] 등록 생략 — slug 미확정", flush=True)
        return
    post = os.environ.get("VESSL_REGISTER", "0") == "1"   # 기본은 로그만. 중계 호스트가 있을 때만 Slack 에 게시(2026-09-08, 알림 과다)
    for i in range(1, tries + 1):
        ep = public_endpoint(slug)
        if ep:
            line = registration_line(slug, owner, ep)
            notify(line) if post else print(f"[exporter] {line}  (VESSL_REGISTER=1 이면 Slack 게시)", flush=True)
            if i > 1:
                print(f"[exporter] 등록 완료 — {i}번째 시도", flush=True)
            return
        if i == 1:
            print("[exporter] endpoints 아직 없음 — 등록을 재시도한다", flush=True)
        time.sleep(wait)
    print("[exporter] 등록 생략 — 워크스페이스 안에 vesslctl 인증이 없거나 endpoints 를 끝내 읽지 못했다. "
          "Slack 중계는 create_workspace.sh 가 게시한 등록에 의존한다.", flush=True)


def control(action: str, slug: str, owner: str, by: str) -> dict:
    """pause / terminate 실행. 반환 dict 의 code 가 HTTP 상태.

    terminate 는 이 컨테이너를 죽이므로 응답을 먼저 보내고 1초 뒤 실행한다.
    실패는 Slack 으로 따로 알린다(응답을 받을 프로세스가 없을 수 있다).
    """
    who = f"<@{by}>" if by else "(알 수 없음)"
    if slug in ("", "unknown"):
        return {"code": 500, "ok": False, "error": "slug 미확정 — 이 워크스페이스는 자기 slug 를 모른다"}
    if not vesslctl_authed():
        return {"code": 409, "ok": False,
                "error": "워크스페이스 안에 vesslctl 인증이 없다. 워크스페이스 터미널에서 "
                         "`vesslctl auth login --password` 후 다시 시도."}
    if action == "pause":
        rc, out, err = run_vesslctl(["workspace", "pause", slug])
        if rc != 0:
            return {"code": 502, "ok": False, "error": (err or out or f"rc={rc}")[:300]}
        notify(f":double_vertical_bar: {tag(owner)}*Slack 요청으로 pause* — `{slug}` · 요청 {who}")
        return {"code": 200, "ok": True}
    if action == "terminate":
        notify(f":skull_and_crossbones: {tag(owner)}*Slack 요청으로 terminate 실행* — `{slug}` · 요청 {who}")

        def later():
            time.sleep(1)
            rc, out, err = run_vesslctl(["workspace", "terminate", slug, "-y"])
            if rc != 0:
                notify(f":x: {tag(owner)}*terminate 실패* — `{slug}`\n```{(err or out or f'rc={rc}')[:300]}```")
        threading.Thread(target=later, daemon=True).start()
        return {"code": 202, "ok": True, "note": "terminate 실행 중. 실패하면 Slack 에 별도 알림."}
    return {"code": 404, "ok": False, "error": f"알 수 없는 동작 {action}"}


# ─────────────────────────── HTTP ───────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    workspace = "unknown"
    owner = ""
    require_token = True

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self) -> bool:
        if not self.require_token:
            return True
        got = self.headers.get("Authorization", "")
        if not got.startswith("Bearer "):
            return False
        # 타이밍 공격 방지. 이 포트는 인터넷에 열려 있다.
        return hmac.compare_digest(got[7:].strip(), TOKEN)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 4096:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}") or {}
        except Exception:
            return {}

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/metrics", "/", "/info"):
            self._send(404, b"not found\n")
            return
        if not self._authorized():
            self._send(401, b"unauthorized\n")
            return
        if path == "/info":
            self._json(200, {"slug": self.workspace, "owner": self.owner,
                             "control": CONTROL, "authed": vesslctl_authed()})
            return
        try:
            body = render(self.workspace, self.owner).encode()
        except Exception as e:                          # 죽지 않는다
            body = f"# exporter error: {e}\n".encode()
        self._send(200, body)

    do_HEAD = do_GET

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/pause", "/terminate"):
            self._send(404, b"not found\n")
            return
        if not self._authorized():
            self._send(401, b"unauthorized\n")
            return
        if not CONTROL:
            self._json(403, {"ok": False, "error": "제어 엔드포인트가 꺼져 있다(VESSL_CONTROL=0)"})
            return
        by = str(self._read_json().get("by", ""))[:64]
        try:
            result = control(path[1:], self.workspace, self.owner, by)
        except Exception as e:                          # 죽지 않는다
            result = {"code": 500, "ok": False, "error": str(e)[:300]}
        code = result.pop("code", 500)
        self._json(code, result)

    def log_message(self, fmt, *args):
        pass                                            # 스크레이프마다 찍히면 로그가 넘친다


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("VESSL_METRICS_PORT") or 9400))
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--workspace", default=os.environ.get("VESSL_WORKSPACE_NAME", "unknown"))
    ap.add_argument("--owner", default=os.environ.get("VESSL_OWNER", ""))
    ap.add_argument("--insecure", action="store_true",
                    help="토큰 검사 없이 기동 (인터넷 노출 — 권장하지 않음)")
    ap.add_argument("--no-register", action="store_true",
                    help="기동 시 Slack 등록 게시를 하지 않는다")
    a = ap.parse_args()

    if not TOKEN and not a.insecure:
        print("[exporter] VESSL_METRICS_TOKEN 이 없다. 이 포트는 인증 없이 인터넷에 "
              "열리므로 기동하지 않는다. 토큰을 설정하거나 --insecure 를 줄 것.",
              file=sys.stderr)
        return 1

    Handler.workspace = a.workspace
    Handler.owner = a.owner
    Handler.require_token = not a.insecure

    cpu_percent()                                       # CPU 델타 기준점을 미리 잡는다

    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    srv.daemon_threads = True
    mode = "토큰 인증" if Handler.require_token else "인증 없음(insecure)"
    ctl = "제어 ON(/pause /terminate)" if CONTROL else "제어 OFF"
    print(f"[exporter] {a.bind}:{a.port}/metrics 기동 — workspace={a.workspace} "
          f"owner={a.owner} {mode} {ctl}", flush=True)
    if not a.no_register:
        threading.Thread(target=register, args=(a.workspace, a.owner), daemon=True).start()
    if push_enabled():
        print(f"[exporter] Grafana push ON — {PUSH_URL} 매 {PUSH_INTERVAL:g}s", flush=True)
        threading.Thread(target=push_loop, args=(a.workspace, a.owner), daemon=True).start()
    else:
        print("[exporter] Grafana push OFF (GRAFANA_PUSH_URL/USER/TOKEN 중 빠진 값 있음)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
