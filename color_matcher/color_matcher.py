"""정답 색 맞추기 자동 도우미.

화면의 '정답' 색을 읽고, 아래 그라데이션 판에서 가장 비슷한 색의 위치를 찾아
핀을 그 자리로 옮긴다. '선택' 칸 색을 다시 읽어서 핀 끝 위치 오차를 스스로 보정한다.

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
TIME_BUDGET = 2.0       # 제한 3초 중 여유를 두고, 이 시간이 지나면 재시도 안 함(초)


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
        "pin": ask_point("지금 핀의 뾰족한 끝"),
        "offset": [0, 0],
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


class Picker:
    """그라데이션 판 이미지를 기억해 두고, 핀에 가려진 부분은 이전 캡처로 채운다."""

    def __init__(self, cfg):
        self.x0, self.y0 = cfg["picker_tl"]
        x1, y1 = cfg["picker_br"]
        self.w, self.h = x1 - self.x0, y1 - self.y0
        self.cache = None
        self.valid = None

    def pin_mask(self, pin):
        mask = np.zeros((self.h, self.w), bool)
        px, py = pin[0] - self.x0, pin[1] - self.y0
        hw, ht = PIN_MASK
        mask[max(0, py - ht):max(0, py + 8), max(0, px - hw):max(0, px + hw)] = True
        return mask

    def refresh(self, sct, pin):
        img = grab(sct, self.x0, self.y0, self.w, self.h)
        fresh = ~self.pin_mask(pin) & ~(img.min(axis=2) > 235)  # 핀 주변·흰 테두리 제외
        if self.cache is None:
            self.cache, self.valid = img.copy(), fresh.copy()
        else:
            self.cache[fresh] = img[fresh]
            self.valid |= fresh

    def find(self, color, near=None, radius=None):
        d = ((self.cache - np.asarray(color)) ** 2).sum(axis=2).astype(float)
        d[~self.valid] = np.inf
        if near is not None:
            yy, xx = np.ogrid[:self.h, :self.w]
            far = (xx - (near[0] - self.x0)) ** 2 + (yy - (near[1] - self.y0)) ** 2 > radius ** 2
            d[far] = np.inf
        iy, ix = np.unravel_index(np.argmin(d), d.shape)
        return [int(ix + self.x0), int(iy + self.y0)], float(np.sqrt(d[iy, ix]))

    def clamp(self, p):
        return [min(max(p[0], self.x0), self.x0 + self.w - 1),
                min(max(p[1], self.y0), self.y0 + self.h - 1)]


def move_pin(frm, to, drag):
    if drag:
        pyautogui.moveTo(*frm)
        pyautogui.mouseDown()
        pyautogui.moveTo(*to, duration=0.05)
    else:
        pyautogui.moveTo(*to)
        pyautogui.mouseDown()
        pyautogui.moveTo(to[0] + 1, to[1])
        pyautogui.moveTo(*to)
    pyautogui.mouseUp()


def solve(sct, cfg, picker, drag, start):
    target = read_swatch(sct, cfg["target"])
    picker.refresh(sct, cfg["pin"])
    best, err = picker.find(target)

    for attempt in range(3):
        if attempt and time.perf_counter() - start > TIME_BUDGET:
            print("    시간 여유가 없어 재시도 생략")
            break
        aim = picker.clamp([best[0] + cfg["offset"][0], best[1] + cfg["offset"][1]])
        move_pin(cfg["pin"], aim, drag)
        cfg["pin"] = aim
        time.sleep(0.06)

        chosen = read_swatch(sct, cfg["selected"])
        diff = color_dist(chosen, target)
        print(f"    시도 {attempt + 1}: 정답 {target.round().astype(int)} "
              f"선택 {chosen.round().astype(int)} 차이 {diff:.1f}")
        if diff <= GOOD_ENOUGH:
            break
        # 실제로 선택된 색이 판의 어디쯤인지 찾아서 '조준점 ↔ 실제 선택점' 오차를 학습
        actual, _ = picker.find(chosen, near=aim, radius=60)
        cfg["offset"][0] += aim[0] - actual[0]
        cfg["offset"][1] += aim[1] - actual[1]

    pyautogui.moveTo(cfg["picker_br"][0] + 40, cfg["picker_br"][1] + 40)  # 커서가 색 칸 가리지 않게
    print(f"    걸린 시간 {time.perf_counter() - start:.2f}초")
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def run(drag):
    if not CONFIG_PATH.exists():
        print("설정이 없어요. 먼저 `python color_matcher.py --setup` 을 실행하세요.")
        return
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    picker = Picker(cfg)
    print("대기 중… 정답 색이 바뀌면 자동으로 맞춥니다. (종료: Ctrl+C)")

    with mss.mss() as sct:
        last = None
        while True:
            target = read_swatch(sct, cfg["target"])
            if last is None or color_dist(target, last) > CHANGE_THRESHOLD:
                start = time.perf_counter()
                time.sleep(0.05)  # 색이 다 바뀔 때까지 아주 잠깐 대기
                print(f"[새 라운드] 정답 색 {read_swatch(sct, cfg['target']).round().astype(int)}")
                solve(sct, cfg, picker, drag, start)
                last = read_swatch(sct, cfg["target"])
            time.sleep(0.03)


def main():
    ap = argparse.ArgumentParser(description="정답 색 맞추기 자동 도우미")
    ap.add_argument("--setup", action="store_true", help="화면 위치 지정")
    ap.add_argument("--drag", action="store_true",
                    help="판을 클릭해도 핀이 안 움직이면: 핀을 잡아 끌어서 옮김")
    args = ap.parse_args()
    try:
        setup() if args.setup else run(args.drag)
    except KeyboardInterrupt:
        print("\n종료")
    except pyautogui.FailSafeException:
        print("\n비상 정지 (마우스가 화면 모서리로 이동)")


if __name__ == "__main__":
    main()
