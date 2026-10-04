# 손가락 피아노

웹캠으로 손가락을 인식해서 연주하는 피아노 웹사이트 (MediaPipe Hands + Web Audio).

## 실행
카메라는 HTTPS 또는 localhost에서만 동작해요.

    python3 -m http.server 8000

브라우저에서 http://localhost:8000 접속 → "카메라 시작" → 손가락으로 건반을 짚으면 소리가 나요.
