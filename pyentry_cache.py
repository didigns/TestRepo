"""PyInstaller 엔트리 포인트: LevACache 워커를 실행한다.
(worker.py 는 패키지 상대 import를 쓰므로 루트에서 이 파일을 통해 빌드한다.)
"""
from LevACache.worker import main

if __name__ == "__main__":
    main()
