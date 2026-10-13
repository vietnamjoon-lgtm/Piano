# 정답 색 맞추기 자동 도우미

'정답' 색을 읽고 그라데이션 판에서 같은 색 위치를 찾아 핀을 옮겨 주는 프로그램이에요.
'선택' 칸 색을 다시 확인해서 어긋나면 스스로 보정해요.

## 설치 (Windows)
    pip install mss numpy pyautogui

## 1) 처음 한 번: 위치 지정
게임 창을 띄워 두고:

    python color_matcher.py --setup

안내에 따라 마우스를 **정답 칸 → 선택 칸 → 판 왼쪽 위 → 판 오른쪽 아래 → 핀 끝** 위에
차례로 올리고 콘솔에서 Enter. (게임 창을 옮기면 다시 해야 해요)

## 2) 실행
    python color_matcher.py

정답 색이 바뀔 때마다 자동으로 맞춰요. 판을 클릭해도 핀이 안 움직이는 게임이면:

    python color_matcher.py --drag

## 멈추기
콘솔에서 Ctrl+C, 또는 마우스를 화면 **왼쪽 위 모서리**로 휙 옮기면 비상 정지.
