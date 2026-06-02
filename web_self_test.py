import os
import tempfile
import time
import urllib.request

from web_transfer import WebTransferServer


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # prepare file to share
        share_path = os.path.join(tmp, "share.txt")
        with open(share_path, "wb") as f:
            f.write(b"hello from pc\n")

        # start server
        srv = WebTransferServer(host="127.0.0.1", port=5055, save_dir=tmp)
        srv.set_shared_files([share_path])
        srv.start()
        time.sleep(0.2)

        # fetch index
        html = urllib.request.urlopen(srv.url, timeout=2).read().decode("utf-8", errors="ignore")
        assert "上传" in html and "下载" in html

        # download file (we don't know id; just ensure page contains /download/)
        assert "/download/" in html

        srv.stop()


if __name__ == "__main__":
    main()
