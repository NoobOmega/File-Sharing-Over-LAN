import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Iterable

from flask import Flask, Response, flash, redirect, render_template_string, request, send_file, url_for
from werkzeug.serving import make_server


INDEX_HTML = """<!doctype html>
<html lang=\"zh-cn\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>局域网文件传输</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; padding: 16px; }
    .card { border: 1px solid #ddd; border-radius: 10px; padding: 12px; margin-bottom: 12px; }
    .btn { display: inline-block; padding: 10px 12px; border: 1px solid #333; border-radius: 8px; text-decoration: none; }
    .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .muted { color: #666; font-size: 13px; }
    ul { padding-left: 18px; }
  </style>
</head>
<body>
  <h2>局域网文件传输</h2>

  <div class=\"card\">
    <h3>上传</h3>
    <form action=\"{{ url_for('upload') }}\" method=\"post\" enctype=\"multipart/form-data\">
      <div class=\"row\">
        <input type=\"file\" name=\"file\" required />
        <button class=\"btn\" type=\"submit\">上传</button>
      </div>
      <div class=\"muted\">上传的文件将保存到电脑端设置的保存目录。</div>
    </form>
  </div>

  <div class=\"card\">
    <h3>下载</h3>
    {% if files %}
      <ul>
        {% for f in files %}
          <li>
            <div class=\"row\">
              <span>{{ f.display_name }}</span>
              <a class=\"btn\" href=\"{{ url_for('download', file_id=f.file_id) }}\">下载</a>
            </div>
            <div class=\"muted\">{{ f.size_bytes }} bytes</div>
          </li>
        {% endfor %}
      </ul>
    {% else %}
      <div class=\"muted\">暂无文件。请在电脑端添加要分享的文件。</div>
    {% endif %}
  </div>

  <div class=\"muted\">刷新页面可看到电脑端最新文件列表。</div>
</body>
</html>
"""


@dataclass
class SharedFile:
    file_id: str
    path: str
    display_name: str
    size_bytes: int


class WebTransferServer:
    """为手机浏览器提供上传/下载的 Flask 服务。

    设计目标：
    - 电脑端可随时更新“共享文件列表”（无需重启服务）
    - 可在后台线程启动/停止（Tkinter UI 可控）
    """

    def __init__(self, host: str, port: int, save_dir: str, log_cb=None):
        self.host = host
        self.port = port
        self.save_dir = save_dir
        self.log_cb = log_cb

        self._lock = threading.Lock()
        self._files: dict[str, SharedFile] = {}

        self._server = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()

        self.app = Flask(__name__)
        self.app.secret_key = uuid.uuid4().hex
        self._register_routes()

    def _log(self, msg: str) -> None:
        if self.log_cb:
            try:
                self.log_cb(msg)
            except Exception:
                pass

    def _register_routes(self) -> None:
        @self.app.get("/")
        def index() -> str:
            with self._lock:
                files = list(self._files.values())
            return render_template_string(INDEX_HTML, files=files)

        @self.app.get("/download/<file_id>")
        def download(file_id: str):
            with self._lock:
                shared = self._files.get(file_id)
            if not shared or not os.path.isfile(shared.path):
                return Response("file not found", status=404)
            return send_file(shared.path, as_attachment=True, download_name=shared.display_name)

        @self.app.post("/upload")
        def upload():
            if "file" not in request.files:
                return Response("missing file", status=400)

            f = request.files["file"]
            if not f.filename:
                return Response("empty filename", status=400)

            os.makedirs(self.save_dir, exist_ok=True)
            filename = os.path.basename(f.filename)
            out_path = os.path.join(self.save_dir, filename)
            base, ext = os.path.splitext(out_path)
            idx = 1
            while os.path.exists(out_path):
                out_path = f"{base} ({idx}){ext}"
                idx += 1

            f.save(out_path)
            self._log(f"[WEB] Uploaded: {out_path}")
            flash("上传成功")
            return redirect(url_for("index"))

    def set_shared_files(self, paths: Iterable[str]) -> None:
        new_map: dict[str, SharedFile] = {}
        for p in paths:
            p = (p or "").strip()
            if not p:
                continue
            ap = os.path.abspath(p)
            if not os.path.isfile(ap):
                continue
            st = os.stat(ap)
            file_id = uuid.uuid4().hex
            new_map[file_id] = SharedFile(
                file_id=file_id,
                path=ap,
                display_name=os.path.basename(ap),
                size_bytes=st.st_size,
            )

        with self._lock:
            self._files = new_map

        self._log(f"[WEB] Shared files updated: {len(new_map)}")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._running.set()

        def _run():
            self._log(f"[WEB] Serving on http://{self.host}:{self.port}")
            self._server = make_server(self.host, self.port, self.app)
            self._server.timeout = 0.5
            while self._running.is_set():
                self._server.handle_request()
            self._log("[WEB] Stopped")

        self._thread = threading.Thread(target=_run, name="web-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        # 触发一次请求循环退出
        time.sleep(0.05)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"
