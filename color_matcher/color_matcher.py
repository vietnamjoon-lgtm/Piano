"""정답 색 맞추기 자동 도우미.

핀은 판 가운데에 고정돼 있고, 판(그라데이션)을 끌어서 움직이는 방식.
화면의 '정답' 색을 읽고, 판에서 가장 비슷한 색의 위치를 찾아
그 지점을 잡고 핀 끝까지 끌어다 놓는다. '선택' 칸 색을 다시 읽어서
어긋나면 화면을 새로 찍어 다시 끌고, 핀 끝 위치 오차도 스스로 보정한다.

사용법:
    pip install mss numpy pyautogui
    python color_matcher.py --setup    # 처음 한 번: 위치 지정
    python color_matcher.py            # 실행 (라운드가 바뀌면 자동으로 맞춤)

비상 정지: 마우스를 화면 왼쪽 위 모서리로 휙 옮기거나 콘솔에서 Ctrl+C.
"""

import argparse
import ctypes
import json
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    # 화면 배율(125%, 150% …)에서도 캡처 좌표와 마우스 좌표가 일치하도록
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import mss
import mss.tools
import numpy as np
import pyautogui

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0

CONFIG_PATH = Path(__file__).with_name("config.json")
SWATCH_RADIUS = 6       # 색 칸 가운데에서 평균 낼 반경(px)
PIN_MASK = (35, 70)     # 핀 그림이 가리는 영역: 좌우 반폭, 위쪽 높이(px)
CHANGE_THRESHOLD = 25   # 정답 색이 이만큼 바뀌면 새 라운드로 판단
GOOD_ENOUGH = 6         # 선택 색과 정답 색 차이가 이 이하면 성공
MAX_TRIES = 5           # 한 라운드에 최대 몇 번 끌어 볼지
TIME_BUDGET = 2.2       # 제한 3초 중 여유를 두고, 이 시간이 지나면 재시도 안 함(초)
DRAG_TIME = 0.08        # 끄는 데 쓰는 시간(초). 너무 빠르면 게임이 못 알아챌 수 있음
SETTLE = 0.06           # 놓은 뒤 화면이 바뀔 때까지 기다리는 시간(초)


def ask_point(label):
    input(f"  마우스를 [{label}] 위에 올려두고 이 창에서 Enter ▶ ")
    x, y = pyautogui.position()
    print(f"    → ({x}, {y})")
    return [int(x), int(y)]


def setup():
    print("게임 창을 띄운 상태에서 위치를 지정합니다. (클릭하지 말고 마우스만 올리세요)")
    cfg = {
        "target": ask_point("정답 색 칸의 가운데"),
        "selected": ask_point("선택 색 칸의 가운데"),
        "picker_tl": ask_point("그라데이션 판의 왼쪽 위 모서리 (살짝 안쪽)"),
        "picker_br": ask_point("그라데이션 판의 오른쪽 아래 모서리 (살짝 안쪽)"),
        "pin": ask_point("핀의 뾰족한 끝"),
    }
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"저장 완료: {CONFIG_PATH}")


def grab(sct, left, top, width, height):
    shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
    return np.asarray(shot)[:, :, 2::-1].astype(np.int32)  # BGRA → RGB


def read_swatch(sct, point):
    x, y = point
    r = SWATCH_RADIUS
    return grab(sct, x - r, y - r, 2 * r + 1, 2 * r + 1).reshape(-1, 3).mean(axis=0)


def color_dist(a, b):
    return float(np.sqrt(((np.asarray(a, float) - np.asarray(b, float)) ** 2).sum()))


class Board:
    """판 화면을 찍어서 원하는 색이 지금 어디에 보이는지 찾는다."""

    def __init__(self, cfg):
        self.x0, self.y0 = cfg["picker_tl"]
        x1, y1 = cfg["picker_br"]
        self.w, self.h = x1 - self.x0, y1 - self.y0
        self.img = None
        # 핀 그림이 가리는 곳은 판 색이 아니므로 검색에서 뺀다 (핀은 고정이라 마스크도 고정)
        self.blocked = np.zeros((self.h, self.w), bool)
        px, py = cfg["pin"][0] - self.x0, cfg["pin"][1] - self.y0
        hw, ht = PIN_MASK
        self.blocked[max(0, py - ht):max(0, py + 8), max(0, px - hw):max(0, px + hw)] = True

    def capture(self, sct):
        self.img = grab(sct, self.x0, self.y0, self.w, self.h)

    def find(self, color, near=None, radius=None):
        d = ((self.img - np.asarray(color)) ** 2).sum(axis=2).astype(float)
        d[self.blocked] = np.inf
        if near is not None:
            yy, xx = np.ogrid[:self.h, :self.w]
            far = (xx - (near[0] - self.x0)) ** 2 + (yy - (near[1] - self.y0)) ** 2 > radius ** 2
            d[far] = np.inf
        iy, ix = np.unravel_index(np.argmin(d), d.shape)
        return [int(ix + self.x0), int(iy + self.y0)], float(np.sqrt(d[iy, ix]))


def measure_shift(before, after, valid, predict):
    """끌기 전/후 판 화면을 비교해서 판이 실제로 몇 px 움직였는지 잰다. (작은 그림으로 대충 → 원본으로 정밀)"""
    def best(a, b, m, cands):
        h, w = m.shape
        top, score = (0, 0), np.inf
        for dx, dy in cands:
            if abs(dx) >= w * 0.7 or abs(dy) >= h * 0.7:
                continue
            ys, ye = max(0, dy), min(h, h + dy)
            xs, xe = max(0, dx), min(w, w + dx)
            bb = b[ys:ye, xs:xe]
            aa = a[ys - dy:ye - dy, xs - dx:xe - dx]
            mm = m[ys:ye, xs:xe] & m[ys - dy:ye - dy, xs - dx:xe - dx]
            n = mm.sum()
            if n < m.size * 0.15:
                continue
            sc = (((aa - bb) ** 2).sum(axis=2) * mm).sum() / n
            if sc < score:
                top, score = (dx, dy), sc
        return top, score

    k = 4
    h, w = valid.shape
    hh, ww = h // k * k, w // k * k
    pool = lambda im: im[:hh, :ww].reshape(hh // k, k, ww // k, k, -1).mean(axis=(1, 3))
    ms = valid[:hh, :ww].reshape(hh // k, k, ww // k, k).all(axis=(1, 3))
    a, b = pool(before.astype(float)), pool(after.astype(float))
    # 예상 이동량 주변만 찾는다 (+ 안 움직였는지 확인용 (0,0))
    px, py = np.asarray(predict, float) / k
    rx, ry = abs(px) * 0.6 + 4, abs(py) * 0.6 + 4
    cands = {(0, 0)} | {(x, y) for x in range(int(px - rx), int(px + rx) + 1)
                        for y in range(int(py - ry), int(py + ry) + 1)}
    (cx, cy), _ = best(a, b, ms, cands)
    fine = [(cx * k + x, cy * k + y) for x in range(-k, k + 1) for y in range(-k, k + 1)]
    (fx, fy), score = best(before.astype(float), after.astype(float), valid, fine)
    return np.array([fx, fy], float), score


def drag_by(board, frm, vec):
    """frm 지점을 잡고 vec 만큼 끈다. 판 밖으로 나가면 잡는 지점을 판 안으로 옮긴다."""
    vec = np.asarray(vec, float)
    lo = np.array([board.x0 + 5, board.y0 + 5], float)
    hi = np.array([board.x0 + board.w - 6, board.y0 + board.h - 6], float)
    frm = np.asarray(frm, float)
    if np.any(frm + vec < lo) or np.any(frm + vec > hi):
        frm = np.clip((lo + hi) / 2 - vec / 2, lo, hi)
    to = np.clip(frm + vec, lo, hi)
    pyautogui.moveTo(int(frm[0]), int(frm[1]))
    pyautogui.mouseDown()
    pyautogui.moveTo(int(to[0]), int(to[1]), duration=DRAG_TIME)
    time.sleep(0.03)  # 멈췄다 놓아야 판이 미끄러지지 않음
    pyautogui.mouseUp()
    return to - frm


def mark(img, p, board, color):
    x, y = p[0] - board.x0, p[1] - board.y0
    out = img
    for d in range(-6, 7):
        for (xx, yy) in ((x + d, y), (x, y + d)):
            if 0 <= xx < board.w and 0 <= yy < board.h:
                out[yy, xx] = color


def save_png(img, path):
    rgb = np.clip(img, 0, 255).astype(np.uint8)
    mss.tools.to_png(rgb.tobytes(), (rgb.shape[1], rgb.shape[0]), output=str(path))


def solve(sct, cfg, board, start, debug_dir=None, rnd=0):
    target = read_swatch(sct, cfg["target"])
    pin = cfg["pin"]
    gain = cfg.setdefault("gain", [1.0, 1.0])  # 마우스 1px 당 판이 움직이는 px

    board.capture(sct)
    for attempt in range(MAX_TRIES):
        if attempt and time.perf_counter() - start > TIME_BUDGET:
            log("    시간 여유가 없어 재시도 생략")
            break
        before = board.img
        spot, err = board.find(target)
        want = np.array(pin, float) - spot                  # 판이 움직여야 할 양
        cmd = want / np.array(gain)                          # 마우스로 끌어야 할 양
        moved_mouse = drag_by(board, spot, cmd)
        time.sleep(SETTLE)

        board.capture(sct)
        shift, score = measure_shift(before, board.img, ~board.blocked, moved_mouse * np.array(gain))
        chosen = read_swatch(sct, cfg["selected"])
        diff = color_dist(chosen, target)
        log(f"    시도 {attempt + 1}: 정답 {target.round().astype(int)} 선택 {chosen.round().astype(int)} "
            f"차이 {diff:.1f} | 찾은곳 {spot} 판에서제일비슷 {err:.0f} | "
            f"마우스 {moved_mouse.round().astype(int)} 판이동 {shift.astype(int)} (목표 {want.astype(int)})")

        if debug_dir is not None:
            b = before.copy(); mark(b, spot, board, (255, 0, 0)); mark(b, pin, board, (0, 255, 0))
            save_png(b, debug_dir / f"r{rnd}_t{attempt + 1}_before.png")
            a = board.img.copy(); mark(a, pin, board, (0, 255, 0))
            save_png(a, debug_dir / f"r{rnd}_t{attempt + 1}_after.png")

        # 판이 실제로 움직인 비율 학습
        for i in range(2):
            if abs(moved_mouse[i]) > 20 and abs(shift[i]) > 3:
                g = shift[i] / moved_mouse[i]
                if 0.5 < g / gain[i] < 2:  # 판 끝에 걸려 덜 움직인 경우 등 이상값은 무시
                    gain[i] = min(max(0.7 * gain[i] + 0.3 * g, 0.2), 5.0)
        if np.abs(moved_mouse).max() > 15 and np.abs(shift).max() < 2:
            log("    ⚠ 끌었는데 판이 안 움직였어요. cmd 창을 '관리자 권한으로 실행'해서 다시 해 보세요.")

        if diff <= GOOD_ENOUGH:
            break
        # 실제 선택된 색이 끌기 전 화면에서 어디였는지 → 핀이 진짜로 가리키는 지점 보정
        # (판이 끝에 걸려 덜 움직였으면 핀 탓이 아니므로 보정하지 않음)
        guess = np.array(pin) - shift
        actual, _ = board_find_in(board, before, chosen, guess, 40)
        if actual is not None and np.abs(shift - want).max() <= 6:
            true_pin = (np.array(actual) + shift).round().astype(int)
            step = np.clip(true_pin - np.array(pin), -8, 8)  # 한 번에 조금씩만 보정
            pin[0], pin[1] = int(pin[0] + step[0]), int(pin[1] + step[1])

    pyautogui.moveTo(cfg["picker_br"][0] + 40, cfg["picker_br"][1] + 40)  # 커서가 색 칸 가리지 않게
    log(f"    걸린 시간 {time.perf_counter() - start:.2f}초  (판 이동 비율 {gain[0]:.2f},{gain[1]:.2f})")
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def board_find_in(board, img, color, near, radius):
    saved = board.img
    board.img = img
    p, e = board.find(color, near=near, radius=radius)
    board.img = saved
    return (p, e) if e < 15 else (None, e)


LOG_PATH = Path(__file__).with_name("log.txt")


def log(msg):
    print(msg)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def run(debug):
    if not CONFIG_PATH.exists():
        print("설정이 없어요. 먼저 `python color_matcher.py --setup` 을 실행하세요.")
        return
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    board = Board(cfg)
    debug_dir = None
    if debug:
        debug_dir = Path(__file__).with_name("debug")
        debug_dir.mkdir(exist_ok=True)
    LOG_PATH.write_text("", encoding="utf-8")
    rnd = 0
    print("대기 중… 정답 색이 바뀌면 자동으로 맞춥니다. (종료: Ctrl+C)")

    with mss.mss() as sct:
        last = None
        while True:
            target = read_swatch(sct, cfg["target"])
            if last is None or color_dist(target, last) > CHANGE_THRESHOLD:
                start = time.perf_counter()
                time.sleep(0.05)  # 색이 다 바뀔 때까지 아주 잠깐 대기
                rnd += 1
                log(f"[라운드 {rnd}] 정답 색 {read_swatch(sct, cfg['target']).round().astype(int)}")
                solve(sct, cfg, board, start, debug_dir, rnd)
                last = read_swatch(sct, cfg["target"])
            time.sleep(0.03)


def main():
    ap = argparse.ArgumentParser(description="정답 색 맞추기 자동 도우미")
    ap.add_argument("--setup", action="store_true", help="화면 위치 지정")
    ap.add_argument("--debug", action="store_true", help="시도마다 판 화면을 debug 폴더에 저장")
    args = ap.parse_args()
    try:
        setup() if args.setup else run(args.debug)
    except KeyboardInterrupt:
        print("\n종료")
    except pyautogui.FailSafeException:
        print("\n비상 정지 (마우스가 화면 모서리로 이동)")


if __name__ == "__main__":
    main()
