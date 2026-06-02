import os
import tempfile
import time
from queue import Queue

from app import TcpFileServer, send_files


def drain(q: Queue[str]) -> list[str]:
    items: list[str] = []
    while True:
        try:
            items.append(q.get_nowait())
        except Exception:
            break
    return items


def main() -> None:
    log_q: Queue[str] = Queue()

    with tempfile.TemporaryDirectory() as tmp:
        bind_ip = "127.0.0.1"
        srv = TcpFileServer(bind_ip=bind_ip, save_dir=tmp, log_q=log_q)
        srv.start()

        src1 = os.path.join(tmp, "_send_me_1.txt")
        src2 = os.path.join(tmp, "_send_me_2.txt")
        with open(src1, "wb") as f:
            f.write(b"hello 1\n")
        with open(src2, "wb") as f:
            f.write(b"hello 2\n")

        send_files(bind_ip, [src1, src2], log_q)
        time.sleep(0.5)

        srv.stop()
        time.sleep(0.2)

        logs = drain(log_q)
        print("\n".join(logs))

        received = [
            p
            for p in os.listdir(tmp)
            if p not in {"_send_me_1.txt", "_send_me_2.txt"}
        ]
        assert len(received) >= 2, "expected at least 2 received files"


if __name__ == "__main__":
    main()
