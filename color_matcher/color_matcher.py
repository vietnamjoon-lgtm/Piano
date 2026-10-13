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


def drag(frm, to):
    """frm 지점을 잡아서 to 지점까지 끌고, 미끄러지지 않게 잠깐 멈췄다 놓는다."""
    pyautogui.moveTo(*frm)
    pyautogui.mouseDown()
    pyautogui.moveTo(*to, duration=DRAG_TIME)
    time.sleep(0.02)
    pyautogui.mouseUp()


def solve(sct, cfg, board, start):
    target = read_swatch(sct, cfg["target"])
    pin = cfg["pin"]

    for attempt in range(MAX_TRIES):
        if attempt and time.perf_counter() - start > TIME_BUDGET:
            print("    시간 여유가 없어 재시도 생략")
            break
        board.capture(sct)
        spot, err = board.find(target)
        drag(spot, pin)
        time.sleep(SETTLE)

        chosen = read_swatch(sct, cfg["selected"])
        diff = color_dist(chosen, target)
        print(f"    시도 {attempt + 1}: 정답 {target.round().astype(int)} "
              f"선택 {chosen.round().astype(int)} 차이 {diff:.1f}")
        if diff <= GOOD_ENOUGH:
            break
        # 끌기 전 화면에서, 실제로 선택된 색이 'spot' 주변 어디였는지 찾는다.
        # 그 차이만큼 '진짜 선택 지점'이 핀 끝 설정과 어긋나 있다는 뜻 → 보정
        actual, _ = board.find(chosen, near=spot, radius=40)
        if color_dist(board.img[actual[1] - board.y0, actual[0] - board.x0], chosen) < 15:
            pin[0] += actual[0] - spot[0]
            pin[1] += actual[1] - spot[1]

    pyautogui.moveTo(cfg["picker_br"][0] + 40, cfg["picker_br"][1] + 40)  # 커서가 색 칸 가리지 않게
    print(f"    걸린 시간 {time.perf_counter() - start:.2f}초")
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def run():
    if not CONFIG_PATH.exists():
        print("설정이 없어요. 먼저 `python color_matcher.py --setup` 을 실행하세요.")
        return
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    board = Board(cfg)
    print("대기 중… 정답 색이 바뀌면 자동으로 맞춥니다. (종료: Ctrl+C)")

    with mss.mss() as sct:
        last = None
        while True:
            target = read_swatch(sct, cfg["target"])
            if last is None or color_dist(target, last) > CHANGE_THRESHOLD:
                start = time.perf_counter()
                time.sleep(0.05)  # 색이 다 바뀔 때까지 아주 잠깐 대기
                print(f"[새 라운드] 정답 색 {read_swatch(sct, cfg['target']).round().astype(int)}")
                solve(sct, cfg, board, start)
                last = read_swatch(sct, cfg["target"])
            time.sleep(0.03)


def main():
    ap = argparse.ArgumentParser(description="정답 색 맞추기 자동 도우미")
    ap.add_argument("--setup", action="store_true", help="화면 위치 지정")
    args = ap.parse_args()
    try:
        setup() if args.setup else run()
    except KeyboardInterrupt:
        print("\n종료")
    except pyautogui.FailSafeException:
        print("\n비상 정지 (마우스가 화면 모서리로 이동)")


if __name__ == "__main__":
    main()
