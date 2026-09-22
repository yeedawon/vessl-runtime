#!/usr/bin/env python3
"""
GPU 유휴 감시 → 워크스페이스 자동 pause (워크스페이스 '내부'에서 실행)

목적: 실험이 끝났거나 죽었는데 워크스페이스가 켜져 있어 크레딧이 새는 것을 막는다.

동작:
  1) interval 초마다 nvidia-smi 로 GPU 사용률·연산 프로세스를 확인
  2) 유휴 notice-min 분 → 1차 알림 (기본 30분, 아직 안 끔)
  3) 유휴 idle-min 분   → pause 예고 (기본 60분)
  4) grace-min 분 더    → `vesslctl workspace pause` 실행 (기본 70분 시점)
  5) 중간에 GPU 가 다시 돌면 카운터를 초기화하고 취소 알림

유휴 판정(기본): 아래 셋을 모두 만족해야 유휴로 본다.
  1) GPU 사용률 < util-threshold (기본 5%)
  2) GPU 연산 프로세스 없음
  3) CPU 사용률 < cpu-threshold (기본 20%)
  4) load average < load-threshold (기본 0.5)

  4번이 특히 중요하다. vCPU 가 11 개인 머신에서 컴파일이 직렬로 돌면 CPU 사용률은
  9% 밖에 안 나와 3번을 통과해버린다. load average 는 코어 수와 무관하므로 이를 잡는다.

  3번이 있는 이유: flash-attn 컴파일이나 데이터 전처리는 CPU 만 쓰므로 GPU 가 0% 다.
  이걸 유휴로 보면 한창 진행 중인 작업이 끊긴다.

  --ignore-processes : GPU 프로세스가 있어도 사용률만으로 판정 (멈춘 작업까지 잡음)
  --cpu-threshold 0  : CPU 조건 무시

필요:
  - 워크스페이스 안에 vesslctl 설치 + 인증 (아래 SETUP 참고)
  - SLACK_WEBHOOK_URL (선택) — 없으면 콘솔 출력만
  - VESSLCTL_ACCESS_TOKEN 또는 vesslctl auth login 완료

사용:
  python gpu_idle_watchdog.py --workspace <slug>
  python gpu_idle_watchdog.py --workspace <slug> --idle-min 60 --grace-min 10
  python gpu_idle_watchdog.py --workspace <slug> --dry-run     # pause 안 하고 로그만

백그라운드 상시 실행:
  nohup python gpu_idle_watchdog.py --workspace <slug> > /root/watchdog.log 2>&1 &
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime

try:
    import requests
except ImportError:
    requests = None

WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# 소유자 — 조직 단위 조회가 CLI 로 안 되므로, 각 워크스페이스가 자기 것을 보고한다.
# 여러 명의 알림이 한 채널에 모이면 누구 것인지 구분이 필요하다.
OWNER = (os.environ.get("VESSL_OWNER")
         or os.environ.get("VESSL_USER")
         or os.environ.get("USER")
         or "")



def _read_environ(path: str) -> dict:
    with open(path, "rb") as f:
        out = {}
        for item in f.read().split(b"\0"):
            if b"=" in item:
                k, v = item.split(b"=", 1)
                out[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
        return out


def _inherit_pid1_env(prefixes=("VESSLCTL_",), key="VESSLCTL_ACCESS_TOKEN", proc="/proc") -> str:
    """VESSL 이 주입하는 VESSLCTL_ACCESS_TOKEN/ORG/TEAM 을 내 환경에 보충한다. 반환: 가져온 출처('' 이면 못 찾음).
    새 워크스페이스는 PID 1 에 있다(2026-09-07 실측). ssh 셸에서 재기동하면 없어서 `vesslctl workspace pause` 가
    'No organization or team set' 으로 실패했다(2026-09-08 실측). 옛 워크스페이스(9/8 이전 생성)는 PID 1 에도 없고
    Jupyter 터미널 프로세스에만 있다(2026-09-20 실측, 한정석 wsp-gkr5tz50aayr) → PID 1 에 없으면 다른 프로세스를 훑는다.
    읽지 못하면 조용히 넘어간다."""
    def take(env: dict) -> None:
        for k, v in env.items():
            if k.startswith(prefixes) and k not in os.environ:
                os.environ[k] = v

    if key in os.environ:
        return "self"
    try:
        take(_read_environ(f"{proc}/1/environ"))
        if key in os.environ:
            return "pid1"
    except Exception:
        pass
    try:
        me = str(os.getpid())
        for d in sorted(os.listdir(proc), key=lambda s: int(s) if s.isdigit() else 0):
            if not d.isdigit() or d in ("1", me):
                continue
            try:
                env = _read_environ(f"{proc}/{d}/environ")
            except Exception:
                continue
            if key in env:
                take(env)
                return f"pid{d}"
    except Exception:
        pass
    return ""


_inherit_pid1_env()

def tag() -> str:
    """알림 앞에 붙일 소유자 표시."""
    return f"[{OWNER}] " if OWNER else ""


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def notify(text: str) -> None:
    """Slack 발송. Webhook 이 없으면 로그만."""
    log(text.replace("\n", " | "))
    if not WEBHOOK_URL or requests is None:
        return
    try:
        requests.post(WEBHOOK_URL, json={"text": text}, timeout=10)
    except Exception as e:
        log(f"WARN: Slack 발송 실패 — {e}")


# ─────────────────────────── GPU 상태 ───────────────────────────

def gpu_utils() -> list[int]:
    """각 GPU 사용률(%)."""
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
        text=True,
    )
    return [int(float(x)) for x in out.split() if x.strip().replace(".", "").isdigit()]


def cpu_percent(sample: float = 1.0) -> float:
    """전체 CPU 사용률(%). /proc/stat 델타로 계산."""
    def snap():
        with open("/proc/stat") as f:
            parts = [float(x) for x in f.readline().split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0.0)
        return sum(parts), idle
    try:
        t1, i1 = snap()
        time.sleep(sample)
        t2, i2 = snap()
        dt, di = t2 - t1, i2 - i1
        return 0.0 if dt <= 0 else max(0.0, min(100.0, (1 - di / dt) * 100))
    except Exception:
        return 0.0   # 측정 실패 시 0 으로 보되, 아래에서 유휴 판정을 막지는 않음


def load_avg() -> float:
    """1분 load average. ⚠️ 컨테이너 안에서는 **호스트 전체** 값이다(2026-09-08 실측: 공유 A100 노드에서
    load 2.96 / 96 CPU, 이 워크스페이스는 0.4%). 유휴 판정에는 cgroup_cpu_cores() 를 우선 쓴다."""
    try:
        return os.getloadavg()[0]
    except Exception:
        return 0.0


_CGROUP_FILES = (
    ("/sys/fs/cgroup/cpu.stat", "v2"),                     # cgroup v2: usage_usec
    ("/sys/fs/cgroup/cpu,cpuacct/cpuacct.usage", "v1"),    # cgroup v1: ns
    ("/sys/fs/cgroup/cpuacct/cpuacct.usage", "v1"),
)


def _cgroup_cpu_seconds() -> float | None:
    """이 컨테이너(cgroup)가 지금까지 쓴 CPU 시간(초). 못 읽으면 None."""
    for path, kind in _CGROUP_FILES:
        try:
            with open(path) as f:
                if kind == "v2":
                    for line in f:
                        if line.startswith("usage_usec"):
                            return int(line.split()[1]) / 1e6
                else:
                    return int(f.read().strip()) / 1e9
        except Exception:
            continue
    return None


def cgroup_cpu_cores(sample: float = 1.0) -> float | None:
    """이 컨테이너가 지금 쓰는 CPU 코어 수(sample 초 동안의 평균). 단일 스레드 컴파일 ≈ 1.0, 완전히 놀면 ≈ 0.0.
    호스트 공유 부하에 영향받지 않는다. cgroup 을 못 읽으면 None → 호출자가 host 기반으로 폴백."""
    a = _cgroup_cpu_seconds()
    if a is None:
        return None
    time.sleep(sample)
    b = _cgroup_cpu_seconds()
    if b is None:
        return None
    return max(0.0, (b - a) / sample)


def compute_procs() -> int:
    """GPU 연산 프로세스 개수."""
    out = subprocess.check_output(
        ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"],
        text=True,
    )
    return len([ln for ln in out.strip().splitlines() if ln.strip()])


def is_idle(threshold: int, ignore_procs: bool,
            cpu_threshold: float, load_threshold: float,
            cores_threshold: float = 0.5) -> tuple[bool, str]:
    """(유휴 여부, 설명)

    GPU 가 놀아도 CPU 가 바쁘면 유휴로 보지 않는다.
    컴파일(flash-attn 등), 데이터 전처리, 압축 해제 같은 작업이 여기 해당한다.

    CPU 는 **이 컨테이너의 cgroup 사용량(코어 수)** 으로 본다: 단일 스레드 컴파일 ≈ 1.0, 놀면 ≈ 0.0.
    /proc/loadavg 와 /proc/stat 은 컨테이너 안에서도 호스트 전체 값이라(2026-09-08 실측: 공유 노드 load 2.96, 96 CPU)
    그것으로 판정하면 남의 부하 때문에 영원히 유휴가 되지 않는다. cgroup 을 못 읽는 환경에서만 그 둘로 폴백한다.
    """
    try:
        utils = gpu_utils()
        procs = compute_procs()
    except Exception as e:
        return False, f"nvidia-smi 오류: {e}"

    peak = max(utils) if utils else 0
    cores = cgroup_cpu_cores() if cores_threshold > 0 else None
    if cores is not None:
        desc = f"util={peak}% procs={procs} cpu_cores={cores:.2f}"
    else:
        cpu = cpu_percent() if cpu_threshold > 0 else 0.0
        load = load_avg()
        desc = f"util={peak}% procs={procs} host_cpu={cpu:.0f}% host_load={load:.2f}"

    if peak >= threshold:
        return False, desc
    if not ignore_procs and procs > 0:
        return False, desc + " (GPU 프로세스 실행 중)"
    if cores is not None:
        if cores >= cores_threshold:
            return False, desc + " (CPU 작업 중 — 컴파일·전처리 등)"
        return True, desc
    if cpu_threshold > 0 and cpu >= cpu_threshold:
        return False, desc + " (CPU 작업 중)"
    if load_threshold > 0 and load >= load_threshold:
        return False, desc + " (작업 실행 중 — 컴파일·전처리 등)"
    return True, desc


# ─────────────────────────── pause 실행 ───────────────────────────

# (주기 GPU 현황 보고 report()/gpu_detail()/fmt_dur() 는 2026-09-21 제거 — 켤 때마다·매시 채널에 표가 뜨는 부담. 랩 전체는 KETI 주간 보고로 본다)


def pause_workspace(slug: str, dry: bool) -> bool:
    if dry:
        log(f"[DRY RUN] vesslctl workspace pause {slug} — 실제로는 실행하지 않음")
        return True
    try:
        r = subprocess.run(["vesslctl", "workspace", "pause", slug],
                           capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        notify(f":x: {tag()}*자동 pause 실패* — 워크스페이스 안에 `vesslctl` 이 없습니다.")
        return False
    except subprocess.TimeoutExpired:
        notify(f":x: {tag()}*자동 pause 실패* — 명령이 응답하지 않습니다.")
        return False

    if r.returncode != 0:
        notify(f":x: {tag()}*자동 pause 실패* — `{slug}`\n```{r.stderr.strip()[:400]}```")
        return False
    return True


# ─────────────────────────── 메인 루프 ───────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=os.environ.get("VESSL_WORKSPACE"),
                    help="pause 할 워크스페이스 slug (또는 VESSL_WORKSPACE 환경변수)")
    ap.add_argument("--idle-min", type=float, default=60, help="유휴 판정 시간(분)")
    ap.add_argument("--grace-min", type=float, default=10, help="예고 후 유예 시간(분)")
    ap.add_argument("--notice-min", type=float, default=30,
                    help="유휴가 이만큼 지속되면 1차 알림 (0 이면 생략). "
                         "예고(--idle-min)보다 앞서 미리 알려준다")
    ap.add_argument("--interval", type=int, default=60, help="확인 주기(초)")
    ap.add_argument("--util-threshold", type=int, default=5, help="이 %% 미만이면 유휴")
    ap.add_argument("--ignore-processes", action="store_true",
                    help="GPU 연산 프로세스가 있어도 사용률만으로 판정")
    ap.add_argument("--cpu-threshold", type=float, default=20,
                    help="CPU 사용률이 이 %% 이상이면 유휴로 보지 않음 (0 이면 무시)")
    ap.add_argument("--load-threshold", type=float, default=0.5,
                    help="(cgroup 을 못 읽을 때만) 1분 load average 가 이 값 이상이면 유휴로 보지 않음 (0 이면 무시)")
    ap.add_argument("--cpu-cores-threshold", type=float, default=0.5,
                    help="이 컨테이너의 CPU 사용 코어 수가 이 값 이상이면 유휴로 보지 않음 (기본 0.5, 0 이면 host 기반 폴백). "
                         "단일 스레드 컴파일(≈1.0)을 잡는다")
    ap.add_argument("--dry-run", action="store_true", help="pause 하지 않고 로그만")
    ap.add_argument("--report-min", type=float, default=0, help=argparse.SUPPRESS)   # 폐지(2026-09-21). 옛 initScript 호환용으로 받기만 하고 무시
    ap.add_argument("--owner", default=None,
                    help="알림에 표시할 소유자 이름 (기본: VESSL_OWNER 또는 USER)")
    args = ap.parse_args()

    if not args.workspace:
        print("ERROR: --workspace <slug> 또는 VESSL_WORKSPACE 가 필요합니다.", file=sys.stderr)
        sys.exit(1)

    if args.owner:
        globals()["OWNER"] = args.owner

    idle_sec = args.idle_min * 60
    grace_sec = args.grace_min * 60
    started_at = time.time()

    log(f"감시 시작 — workspace={args.workspace} "
        f"{'notice=' + str(args.notice_min) + '분 ' if args.notice_min > 0 else ''}"
        f"idle={args.idle_min}분 "
        f"grace={args.grace_min}분 interval={args.interval}초 "
        f"gpu<{args.util_threshold}% "
        f"{'cpu_cores<' + str(args.cpu_cores_threshold) + ' ' if (args.cpu_cores_threshold > 0 and _cgroup_cpu_seconds() is not None) else ''}"
        f"{'(cgroup 없음→host) cpu<' + str(int(args.cpu_threshold)) + '% load<' + str(args.load_threshold) + ' ' if (args.cpu_cores_threshold <= 0 or _cgroup_cpu_seconds() is None) else ''}"
        f"{'(프로세스 무시)' if args.ignore_processes else '(프로세스 있으면 유휴 아님)'}"
        f"{' [DRY RUN]' if args.dry_run else ''}")

    idle_since: float | None = None   # 유휴가 시작된 시각
    noticed = False                   # 1차 알림 발송 여부
    warned = False                    # pause 예고 발송 여부
    notice_sec = args.notice_min * 60

    while True:
        idle, desc = is_idle(args.util_threshold, args.ignore_processes,
                             args.cpu_threshold, args.load_threshold, args.cpu_cores_threshold)
        now = time.time()

        if not idle:
            if idle_since is not None:
                elapsed = (now - idle_since) / 60
                log(f"GPU 재가동 — 유휴 카운터 초기화 ({elapsed:.1f}분 경과분 취소) | {desc}")
                if warned:
                    notify(f":arrows_counterclockwise: {tag()}*자동 pause 취소* — `{args.workspace}`\n"
                           f"GPU 가 다시 사용되기 시작했습니다. ({desc})")
            idle_since, noticed, warned = None, False, False
            time.sleep(args.interval)
            continue

        # 유휴 상태
        if idle_since is None:
            idle_since = now
            log(f"유휴 감지 시작 | {desc}")

        elapsed = now - idle_since

        # 1차 알림 — 예고보다 먼저 한 번 알려준다
        if (args.notice_min > 0 and not noticed and not warned
                and elapsed >= notice_sec and elapsed < idle_sec):
            noticed = True
            remain = (idle_sec - elapsed) / 60
            notify(f":zzz: {tag()}*유휴 알림* — `{args.workspace}`\n"
                   f"GPU 가 {args.notice_min:g}분간 사용되지 않았습니다 ({desc}).\n"
                   f"_이대로 {remain:.0f}분 더 지나면 자동 pause 를 예고합니다._")

        if not warned and elapsed >= idle_sec:
            warned = True
            notify(f":hourglass_flowing_sand: {tag()}*유휴 감지* — `{args.workspace}`\n"
                   f"GPU 가 {args.idle_min:g}분간 사용되지 않았습니다 ({desc}).\n"
                   f"*{args.grace_min:g}분 뒤 자동으로 pause 합니다.* "
                   f"계속 사용하려면 GPU 작업을 시작하거나 이 감시를 중단하세요.")

        elif warned and elapsed >= idle_sec + grace_sec:
            total = elapsed / 60
            if pause_workspace(args.workspace, args.dry_run):
                # 완료 줄은 기본 OFF(2026-09-22 팀 요청: 채널엔 생성·terminate·유휴 예고만). VESSL_NOTIFY_PAUSE_DONE=1 로 되돌림.
                done = (f":large_blue_circle: {tag()}*자동 pause 완료* — `{args.workspace}`\n"
                        f"GPU 유휴 {total:.0f}분 지속으로 워크스페이스를 정지했습니다.\n"
                        f"_`/root` 의 데이터는 유지됩니다. 재개: `./workspace_ctl.sh start {args.workspace}`_")
                if os.environ.get("VESSL_NOTIFY_PAUSE_DONE", "0") == "1":
                    notify(done)
                else:
                    log(done.replace("\n", " | "))
                log("pause 완료 — 감시 종료")
                return
            # 실패 시 다시 시도하지 않도록 카운터만 초기화
            idle_since, noticed, warned = None, False, False

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
