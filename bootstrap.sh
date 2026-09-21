#!/bin/bash
# ============================================================================
# VESSL 워크스페이스 부트스트랩 — initScript 에 붙여넣어 사용
#
# 워크스페이스가 생성될 때 자동으로:
#   1) sshd 설치·기동 (VS Code Remote-SSH 용)
#   2) Python 의존성 설치
#   3) 유휴 감시 스크립트 배치
#   4) 워크스페이스 slug 자동 탐지
#   5) 감시 데몬 기동
#
# 사전 준비 (관리자 1회):
#   - 조직 Secret 에 SLACK_WEBHOOK_URL 등록 → 모든 워크로드에 환경변수로 주입됨
#
# 사전 준비 (사용자 1회):
#   - 워크스페이스에서 `vesslctl auth login --password` 실행
#     /root 는 cluster storage 라 인증이 다음 워크스페이스에도 유지된다
#     인증이 없으면 자동 pause 없이 "알림 전용" 모드로 동작한다
# ============================================================================

set -u

WATCHDOG=/root/gpu_idle_watchdog.py
EMBEDDED=/root/.gpu_idle_watchdog.embedded.py

# 감시 스크립트 자동 갱신 (선택) ─────────────────────────────────────────────
# 공개 저장소의 raw URL 을 넣어두면 워크스페이스가 시작될 때마다 최신본을 받는다.
# 비워두면 생성 시점에 내장된 사본을 그대로 쓴다.
#   예) https://raw.githubusercontent.com/<org>/<repo>/main/gpu_idle_watchdog.py
LOG=/root/watchdog.log
NOTICE_MIN="${VESSL_NOTICE_MIN:-0}"    # 1차 유휴 알림(분). 기본 0 = 생략 (2026-09-10: 유휴 알람은 60분 예고 1회 + pause 완료만). 30 으로 되돌릴 수 있다
IDLE_MIN="${VESSL_IDLE_MIN:-60}"
OWNER="${VESSL_OWNER:-}"               # 알림에 표시할 이름. 비우면 워크스페이스 slug 로 구분
GRACE_MIN="${VESSL_GRACE_MIN:-10}"

log() { echo "[bootstrap $(date +%H:%M:%S)] $*"; }

# ── 0. 런타임 최신본 (2026-09-21) ──────────────────────────────────────────
# initScript 는 생성 시점에 고정된다(수정 API 없음). 그래서 기동할 때마다 공개 런타임 저장소에서 현재 코드 세 파일
# (bootstrap.sh · gpu_idle_watchdog.py · gpu_metrics_exporter.py)을 받아 그것으로 실행한다 → 재가동 = 항상 최신 코드.
# 받기·검증에 실패하면 이 initScript 에 박힌 사본으로 계속한다(폴백). 공개 저장소에는 비밀값이 없다(값은 위 export 블록에).
# 끄기: VESSL_RUNTIME_URL=off. 배포: 관리자가 ./publish_runtime.sh (stable 태그를 옮긴다).
RUNTIME_URL="${VESSL_RUNTIME_URL:-https://raw.githubusercontent.com/yeedawon/vessl-runtime/stable}"
RUNTIME_BASE="${VESSL_RUNTIME_DIR:-/root/.vessl-runtime}"
_rt_py() { local c; for c in /opt/conda/bin/python3 /opt/conda/bin/python /usr/local/bin/python3 /usr/bin/python3; do [ -x "$c" ] && { echo "$c"; return; }; done; command -v python3; }
_rt_fetch() {   # <url> <dest> — curl 이 없는 이미지(python:*-slim)는 python 으로
    if command -v curl >/dev/null 2>&1; then curl -fsSL -m 20 --retry 2 "$1" -o "$2"
    else "$(_rt_py)" - "$1" "$2" <<'PYF'
import sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=20) as r, open(sys.argv[2], "wb") as f: f.write(r.read())
PYF
    fi
}
case "$RUNTIME_URL" in off|0|"") RUNTIME_URL="" ;; esac
if [ -n "$RUNTIME_URL" ] && [ -z "${VESSL_RUNTIME_ACTIVE:-}" ]; then
    rm -rf "$RUNTIME_BASE.new"; mkdir -p "$RUNTIME_BASE.new"; _rt_ok=yes
    # raw.githubusercontent.com 은 CDN 이 수 분 캐시한다 — 배포 2분 뒤 재가동이 옛 판을 받았다(2026-09-21 실측). 시각 쿼리로 매번 원본을 받는다.
    _rt_q="?t=$(date +%s)"
    for _f in bootstrap.sh gpu_idle_watchdog.py gpu_metrics_exporter.py; do
        _rt_fetch "$RUNTIME_URL/$_f$_rt_q" "$RUNTIME_BASE.new/$_f" 2>/dev/null || { _rt_ok=no; log "런타임 받기 실패: $_f"; break; }
    done
    _rt_fetch "$RUNTIME_URL/VERSION$_rt_q" "$RUNTIME_BASE.new/VERSION" 2>/dev/null || echo "?" > "$RUNTIME_BASE.new/VERSION"
    if [ "$_rt_ok" = yes ]; then
        bash -n "$RUNTIME_BASE.new/bootstrap.sh" 2>/dev/null || { _rt_ok=no; log "런타임 검증 실패: bootstrap.sh 문법"; }
    fi
    if [ "$_rt_ok" = yes ]; then
        "$(_rt_py)" -m py_compile "$RUNTIME_BASE.new/gpu_idle_watchdog.py" "$RUNTIME_BASE.new/gpu_metrics_exporter.py" 2>/dev/null \
            || { _rt_ok=no; log "런타임 검증 실패: python 문법"; }
    fi
    if [ "$_rt_ok" = yes ]; then
        rm -rf "$RUNTIME_BASE"; mv "$RUNTIME_BASE.new" "$RUNTIME_BASE"
        log "런타임 최신본으로 실행 — $(head -1 "$RUNTIME_BASE/VERSION") ($RUNTIME_URL)"
        export VESSL_RUNTIME_ACTIVE=1 VESSL_RUNTIME_SRC="$RUNTIME_BASE"
        exec bash "$RUNTIME_BASE/bootstrap.sh"
    fi
    rm -rf "$RUNTIME_BASE.new"
    log "→ initScript 에 박힌 사본으로 진행 (생성 시점 코드)"
fi
# ── 0 끝 ──

# ── 1. PATH ────────────────────────────────────────────────────────────────
export PATH="$HOME/.local/bin:$PATH"
grep -q '.local/bin' /root/.bashrc 2>/dev/null || \
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> /root/.bashrc

# ── 2. SSH 서버 준비 ───────────────────────────────────────────────────────
# NGC 계열 이미지(nvcr.io/nvidia/*)에는 sshd 가 없어 VS Code Remote-SSH 가
# "Connection refused" 로 실패한다. Jupyter 만 뜨는 이유가 이것이다.
# apt 설치분은 /root 밖이라 pause 하면 사라지므로 startup 마다 확인·기동한다.
if [ "${VESSL_ENABLE_SSHD:-1}" = "1" ]; then
    if ! command -v sshd >/dev/null 2>&1 && [ ! -x /usr/sbin/sshd ]; then
        log "sshd 설치 중..."
        apt-get update -qq >/dev/null 2>&1 && \
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
            openssh-server >/dev/null 2>&1 || log "WARN: sshd 설치 실패"
    fi

    if [ -x /usr/sbin/sshd ]; then
        mkdir -p /run/sshd /root/.ssh
        chmod 700 /root/.ssh
        [ -f /root/.ssh/authorized_keys ] && chmod 600 /root/.ssh/authorized_keys
        ssh-keygen -A >/dev/null 2>&1

        if pgrep -x sshd >/dev/null 2>&1; then
            log "sshd 이미 실행 중"
        else
            nohup /usr/sbin/sshd -D -e > /root/sshd.log 2>&1 &
            sleep 1
            if pgrep -x sshd >/dev/null 2>&1; then
                log "sshd 기동 완료"
            else
                log "WARN: sshd 기동 실패 — /root/sshd.log 확인"
            fi
        fi

        # 공개키가 없으면 접속이 불가능하므로 알려준다
        if [ ! -s /root/.ssh/authorized_keys ]; then
            log "WARN: /root/.ssh/authorized_keys 가 비어 있어 SSH 접속이 불가합니다."
            log "      워크스페이스 생성 시 SSH 키를 지정했는지 확인하거나,"
            log "      로컬에서 'ssh-keygen -y -f ~/.ssh/<키>.pem' 결과를 여기에 추가하세요."
        fi
    fi
else
    log "sshd 준비 건너뜀 (VESSL_ENABLE_SSHD=0)"
fi

# ── 3. Python 준비 ─────────────────────────────────────────────────────────
# 이미지마다 torch 가 들어 있는 python 이 다르다 (conda / 시스템 / 기타).
# torch 를 import 할 수 있는 python 을 베이스로 골라야 venv 에서도 torch 가 보인다.
pick_base_python() {
    local c
    # 1순위: torch 가 실제로 import 되는 python
    for c in /opt/conda/bin/python3 /opt/conda/bin/python \
             /usr/local/bin/python3 /usr/bin/python3; do
        [ -x "$c" ] || continue
        if "$c" -c 'import torch' >/dev/null 2>&1; then echo "$c"; return 0; fi
    done
    # 2순위: torch 가 없더라도 쓸 수 있는 python
    for c in /opt/conda/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
        [ -x "$c" ] && { echo "$c"; return 0; }
    done
    command -v python3 2>/dev/null
}

BASE_PY="$(pick_base_python)"
[ -n "$BASE_PY" ] || { log "python3 을 찾을 수 없음 — 부트스트랩 중단"; exit 0; }

PYVER="$("$BASE_PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
VENV=/root/envs/py$PYVER
PY=$VENV/bin/python

if "$BASE_PY" -c 'import torch' >/dev/null 2>&1; then
    log "베이스 python: $BASE_PY (torch 있음, $PYVER)"
else
    log "베이스 python: $BASE_PY (torch 없음, $PYVER)"
fi

# /root 는 cluster storage 라 venv 가 다음 워크스페이스에도 남는다.
# 이미지의 Python 버전이 바뀌면 기존 venv 가 깨지므로 버전별로 분리한다.
if [ ! -x "$PY" ]; then
    log "가상환경 생성: $VENV"
    # --system-site-packages : 이미지에 설치된 torch 등을 그대로 쓰기 위함.
    #   빼면 venv 안에서 torch 가 안 보여 flash-attn 빌드 등이 실패한다.
    "$BASE_PY" -m venv --system-site-packages "$VENV"
else
    log "기존 가상환경 재사용: $VENV"
    # 예전에 격리 모드로 만들어진 venv 는 torch 를 못 본다 — 감지해서 안내
    if "$BASE_PY" -c 'import torch' >/dev/null 2>&1 \
       && ! "$PY" -c 'import torch' >/dev/null 2>&1; then
        log "WARN: venv 에서 torch 가 보이지 않습니다 (--system-site-packages 누락)."
        log "      다시 만들려면:"
        log "        mv $VENV $VENV.bak"
        log "        $BASE_PY -m venv --system-site-packages $VENV"
    fi
fi

"$PY" -m pip install --quiet --upgrade pip setuptools wheel requests 2>/dev/null || \
    log "WARN: 기본 패키지 설치 실패 — Slack 알림이나 빌드가 안 될 수 있음"

# venv 를 기본 PATH 에 올려 pip / python 을 바로 쓸 수 있게 한다.
# Python 버전이 바뀌어도 자동으로 맞는 venv 를 고르도록 동적으로 작성한다.
if ! grep -q '>>> vessl venv >>>' /root/.bashrc 2>/dev/null; then
    cat >> /root/.bashrc << 'BASHRC_EOF'

# >>> vessl venv >>>
# 이미지의 Python 버전에 맞는 가상환경을 PATH 앞에 붙인다.
# 덕분에 pip / python 을 절대경로 없이 바로 쓸 수 있다.
for __vessl_p in /opt/conda/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [ -x "$__vessl_p" ] || continue
    __vessl_v=$("$__vessl_p" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)
    if [ -n "$__vessl_v" ] && [ -d "/root/envs/py$__vessl_v/bin" ]; then
        export PATH="/root/envs/py$__vessl_v/bin:$PATH"
        break
    fi
done
unset __vessl_p __vessl_v
# <<< vessl venv <<<
BASHRC_EOF
    log ".bashrc 에 venv PATH 등록 (다음 접속부터 pip/python 바로 사용 가능)"
fi

# ── 4. 감시 스크립트 배치 ──────────────────────────────────────────────────
# create_workspace.sh 가 아래 마커 자리에 gpu_idle_watchdog.py 내용을 심는다.
# (워크스페이스는 KETI 등 외부 파일시스템을 볼 수 없으므로 자체 포함이 필요)
#__WATCHDOG_EMBED__

# 런타임 최신본(§0 에서 받은 것)이 있으면 그것을, 없으면 initScript 에 박힌 사본을 쓴다.
if [ -n "${VESSL_RUNTIME_SRC:-}" ] && [ -f "$VESSL_RUNTIME_SRC/gpu_idle_watchdog.py" ]; then
    cp "$VESSL_RUNTIME_SRC/gpu_idle_watchdog.py" "$WATCHDOG"
elif [ -f "$EMBEDDED" ]; then
    cp "$EMBEDDED" "$WATCHDOG"
fi

[ -f "$WATCHDOG" ] || { log "감시 스크립트 없음 — 부트스트랩 중단"; exit 0; }

# ── 5. vesslctl 확인 (자동 pause 가능 여부) ────────────────────────────────
AUTO_PAUSE=no
if command -v vesslctl >/dev/null 2>&1; then
    # auth status 는 로그아웃이어도 exit 0 → 출력으로 판정(2026-09-09). 워크스페이스 안은 보통 workload token.
    if vesslctl auth status 2>/dev/null | grep -qiE 'Username:|workload token' ; then
        AUTO_PAUSE=yes
    else
        log "vesslctl 인증 없음 → 알림 전용 모드"
        log "  자동 pause 를 쓰려면: vesslctl auth login --password"
    fi
else
    log "vesslctl 미설치 → 설치 시도"
    # python:3.11-slim 같은 CPU 이미지는 curl 이 없어 설치 스크립트를 받지 못했다(2026-09-08 실측 → CPU 워크스페이스 자동 pause 불가).
    if ! command -v curl >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
        log "curl 없음 → apt 로 설치"
        (apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends curl ca-certificates) >/dev/null 2>&1 \
            || log "WARN: curl 설치 실패 — vesslctl 을 설치할 수 없어 알림 전용 모드"
    fi
    export PATH="$HOME/.local/bin:$PATH"
    curl -fsSL https://api.cloud.vessl.ai/cli/install.sh | bash >/dev/null 2>&1
    command -v vesslctl >/dev/null 2>&1 && vesslctl auth status 2>/dev/null | grep -qiE 'Username:|workload token' && AUTO_PAUSE=yes
    command -v vesslctl >/dev/null 2>&1 || log "WARN: vesslctl 설치 실패 → 알림 전용 모드"
fi

# ── 6. 워크스페이스 slug 탐지 ──────────────────────────────────────────────
SLUG="${VESSL_WORKSPACE:-}"

# 6-1. hostname 에서 직접 추출 (main-wsp-xxxxxxxx-0 → wsp-xxxxxxxx)
#      vesslctl 인증이 없어도 동작하므로 이 방법을 우선한다
if [ -z "$SLUG" ]; then
    SLUG=$(hostname | grep -oE 'wsp-[a-z0-9]+' | head -1)
    [ -n "$SLUG" ] && log "hostname 에서 slug 탐지: $SLUG"
fi

# 6-2. 그래도 없으면 vesslctl 로 조회 (인증 필요)
if [ -z "$SLUG" ] && [ "$AUTO_PAUSE" = "yes" ]; then
    SLUG=$("$PY" - <<'PYEOF' 2>/dev/null
import json, socket, subprocess, sys
host = socket.gethostname()
try:
    out = subprocess.check_output(["vesslctl","workspace","list","-o","json"], text=True)
    items = json.loads(out or "[]")
except Exception:
    sys.exit(0)

ws = [w.get("workspace", w) for w in items]
running = [w for w in ws if str(w.get("state","")).lower() == "running"]

for w in running:
    s = w.get("slug") or ""
    if s and (s in host or host in s):
        print(s); sys.exit(0)
if len(running) == 1:
    print(running[0].get("slug","")); sys.exit(0)
PYEOF
)
    [ -n "$SLUG" ] && log "vesslctl 조회로 slug 탐지: $SLUG"
fi

# ── 7. 감시 기동 ───────────────────────────────────────────────────────────
pkill -f gpu_idle_watchdog.py 2>/dev/null

if [ -z "$SLUG" ]; then
    log "slug 탐지 실패 → 알림 전용(dry-run) 으로 기동"
    log "  수동 지정: pkill -f gpu_idle_watchdog.py && \\"
    log "    nohup $PY -u $WATCHDOG --workspace <slug> --idle-min $IDLE_MIN > $LOG 2>&1 &"
    nohup "$PY" -u "$WATCHDOG" --workspace unknown \
        --notice-min "$NOTICE_MIN" --idle-min "$IDLE_MIN" --grace-min "$GRACE_MIN" \
        ${OWNER:+--owner "$OWNER"} --dry-run > "$LOG" 2>&1 &
elif [ "$AUTO_PAUSE" = "yes" ]; then
    log "자동 pause 모드 — slug=$SLUG idle=${IDLE_MIN}분 grace=${GRACE_MIN}분"
    nohup "$PY" -u "$WATCHDOG" --workspace "$SLUG" \
        --notice-min "$NOTICE_MIN" --idle-min "$IDLE_MIN" --grace-min "$GRACE_MIN" \
        ${OWNER:+--owner "$OWNER"} > "$LOG" 2>&1 &
else
    log "알림 전용 모드 — slug=$SLUG (인증 없어 pause 불가)"
    nohup "$PY" -u "$WATCHDOG" --workspace "$SLUG" \
        --notice-min "$NOTICE_MIN" --idle-min "$IDLE_MIN" --grace-min "$GRACE_MIN" \
        ${OWNER:+--owner "$OWNER"} --dry-run > "$LOG" 2>&1 &
fi

# ── 8. GPU 메트릭 exporter (선택) ──────────────────────────────────────────
# VESSL_METRICS_PORT 가 설정된 경우에만 동작한다. 미설정이 기본 = 기존과 동일.
#
# 워크스페이스 생성 시 --port metrics:<포트>:tcp 를 함께 주어야 외부에서 닿는다.
# tcp 로 연 포트는 VESSL 프록시를 거치지 않아 인증이 없으므로(= 인터넷에 열린다)
# exporter 가 직접 Bearer 토큰을 검사하고, VESSL_METRICS_TOKEN 이 없으면 뜨지 않는다.
#
# nvidia-smi 파싱은 위에서 배치한 gpu_idle_watchdog.py 를 import 해 재사용한다.
# 유휴 판정 기준이 watchdog 과 갈리지 않게 하려는 것이다.
METRICS_PORT="${VESSL_METRICS_PORT:-}"
EXPORTER=/root/gpu_metrics_exporter.py
METRICS_LOG=/root/metrics_exporter.log

if [ -n "$METRICS_PORT" ]; then
    # create_workspace.sh 가 아래 마커 자리에 gpu_metrics_exporter.py 를 심는다.
    # (§4 의 watchdog 과 같은 방식. 본문을 여기 복사해 두지 않는다 — 두 벌이 갈라진다)
    #__EXPORTER_EMBED__

    # 런타임 최신본(§0)이 있으면 그것으로 덮는다(폴백 사본은 위 마커가 이미 썼다).
    if [ -n "${VESSL_RUNTIME_SRC:-}" ] && [ -f "$VESSL_RUNTIME_SRC/gpu_metrics_exporter.py" ]; then
        cp "$VESSL_RUNTIME_SRC/gpu_metrics_exporter.py" "$EXPORTER"
    fi
    [ -f "$EXPORTER" ] || { log "WARN: exporter 파일 없음 — 건너뜀"; METRICS_PORT=""; }
fi
if [ -n "$METRICS_PORT" ]; then
    chmod +x "$EXPORTER"
    pkill -f gpu_metrics_exporter.py 2>/dev/null
    nohup "$PY" -u "$EXPORTER" --port "$METRICS_PORT" \
        --workspace "${SLUG:-unknown}" ${OWNER:+--owner "$OWNER"} \
        > "$METRICS_LOG" 2>&1 &
    METRICS_PID=$!
    sleep 1
    # 파일명이 아니라 PID 로 확인한다. pgrep -f 는 경로가 바뀌면 놓친다.
    if kill -0 "$METRICS_PID" 2>/dev/null; then
        log "메트릭 exporter 기동 — 포트 $METRICS_PORT"
        [ -n "${VESSL_METRICS_TOKEN:-}" ] || log "WARN: 토큰 없이 떴습니다"
    else
        # 대개 VESSL_METRICS_TOKEN 미설정. 인증 없이 열리는 것을 일부러 막는다.
        log "WARN: exporter 기동 실패 — $METRICS_LOG 확인"
    fi
fi

sleep 1
log "완료 — 로그: tail -f $LOG"
[ -n "${SLACK_WEBHOOK_URL:-}" ] || log "WARN: SLACK_WEBHOOK_URL 없음 → 로그에만 기록됨"