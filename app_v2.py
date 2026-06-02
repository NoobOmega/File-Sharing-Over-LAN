import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import qrcode
from PIL import ImageTk

from app import DiscoveryService, TcpFileServer, list_local_ipv4_candidates, send_files
from web_transfer import WebTransferServer


WEB_PORT_DEFAULT = 5000


class V2App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("局域网文件传输 v2.0")
        self.geometry("820x560")

        self.log_q: "queue.Queue[str]" = queue.Queue()
        self.hosts_q: "queue.Queue[dict[str, object]]" = queue.Queue()

        self.mode_var = tk.StringVar(value="电脑之间")
        self.bind_ip_var = tk.StringVar(value="")
        self.save_dir_var = tk.StringVar(value=os.getcwd())

        # P2P
        self.p2p_target_ip_var = tk.StringVar(value="")
        self.p2p_files: list[str] = []
        self.discovery: DiscoveryService | None = None
        self.tcp_server: TcpFileServer | None = None

        # WEB
        self.web_port_var = tk.StringVar(value=str(WEB_PORT_DEFAULT))
        self.web_files: list[str] = []
        self.web_server: WebTransferServer | None = None
        self._qr_photo = None

        self._build_ui()
        self._init_bind_ip_default()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(120, self._poll_queues)

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(top, text="模式").grid(row=0, column=0, sticky=tk.W)
        self.mode_combo = ttk.Combobox(
            top,
            textvariable=self.mode_var,
            width=16,
            values=["电脑之间", "手机与电脑之间", "手机之间"],
            state="readonly",
        )
        self.mode_combo.grid(row=0, column=1, sticky=tk.W, padx=(8, 0))
        self.mode_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_mode_change())

        ttk.Label(top, text="本机IP(热点IP)").grid(row=0, column=2, sticky=tk.W, padx=(16, 0))
        self.bind_combo = ttk.Combobox(top, textvariable=self.bind_ip_var, width=18, values=[])
        self.bind_combo.grid(row=0, column=3, sticky=tk.W, padx=(8, 0))
        ttk.Button(top, text="刷新", command=self.refresh_local_ips).grid(row=0, column=4, padx=8)

        ttk.Label(top, text="保存目录").grid(row=0, column=5, sticky=tk.W, padx=(16, 0))
        ttk.Entry(top, textvariable=self.save_dir_var, width=28).grid(row=0, column=6, sticky=tk.W, padx=(8, 0))
        ttk.Button(top, text="选择", command=self.choose_save_dir).grid(row=0, column=7, padx=8)

        self.main = ttk.Frame(self)
        self.main.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self.p2p_frame = ttk.Frame(self.main)
        self.web_frame = ttk.Frame(self.main)
        self.todo_frame = ttk.Frame(self.main)

        self._build_p2p_frame(self.p2p_frame)
        self._build_web_frame(self.web_frame)
        self._build_todo_frame(self.todo_frame)

        bottom = ttk.Labelframe(self, text="日志")
        bottom.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.log_text = tk.Text(bottom, height=10, wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self._on_mode_change()

    def _build_p2p_frame(self, root: ttk.Frame) -> None:
        left = ttk.Labelframe(root, text="发现的主机")
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=False)

        self.host_list = tk.Listbox(left, height=16, width=26)
        self.host_list.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.host_list.bind("<<ListboxSelect>>", self._on_host_select)

        right = ttk.Frame(root)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0))

        send_box = ttk.Labelframe(right, text="发送（电脑之间）")
        send_box.pack(fill=tk.X)

        ttk.Label(send_box, text="目标IP").grid(row=0, column=0, sticky=tk.W, padx=8, pady=8)
        ttk.Entry(send_box, textvariable=self.p2p_target_ip_var, width=22).grid(row=0, column=1, sticky=tk.W)

        ttk.Label(send_box, text="文件列表").grid(row=1, column=0, sticky=tk.NW, padx=8, pady=8)

        file_frame = ttk.Frame(send_box)
        file_frame.grid(row=1, column=1, sticky=tk.W, padx=(0, 0), pady=8)

        self.p2p_file_list = tk.Listbox(file_frame, height=6, width=52, selectmode=tk.EXTENDED)
        self.p2p_file_list.grid(row=0, column=0, sticky=tk.W)
        yscroll = ttk.Scrollbar(file_frame, orient=tk.VERTICAL, command=self.p2p_file_list.yview)
        yscroll.grid(row=0, column=1, sticky=tk.NS)
        self.p2p_file_list.configure(yscrollcommand=yscroll.set)

        btns = ttk.Frame(send_box)
        btns.grid(row=1, column=2, padx=8, sticky=tk.NW, pady=8)
        ttk.Button(btns, text="添加文件", command=self._p2p_add_files).pack(fill=tk.X)
        ttk.Button(btns, text="移除选中", command=self._p2p_remove_selected).pack(fill=tk.X, pady=(6, 0))
        ttk.Button(btns, text="清空", command=self._p2p_clear).pack(fill=tk.X, pady=(6, 0))

        self.p2p_count_var = tk.StringVar(value="")
        ttk.Label(send_box, textvariable=self.p2p_count_var).grid(row=2, column=1, sticky=tk.W, padx=8)

        ttk.Button(send_box, text="一键传输", command=self._p2p_send).grid(row=3, column=1, sticky=tk.W, padx=8, pady=(0, 8))

        ctrl = ttk.Labelframe(right, text="服务")
        ctrl.pack(fill=tk.X, pady=(10, 0))
        self.p2p_start_btn = ttk.Button(ctrl, text="启动发现/接收", command=self._p2p_start)
        self.p2p_start_btn.grid(row=0, column=0, padx=8, pady=8)
        self.p2p_stop_btn = ttk.Button(ctrl, text="停止", command=self._p2p_stop, state=tk.DISABLED)
        self.p2p_stop_btn.grid(row=0, column=1, padx=8, pady=8)

    def _build_web_frame(self, root: ttk.Frame) -> None:
        box = ttk.Labelframe(root, text="手机与电脑之间（手机用浏览器打开）")
        box.pack(fill=tk.BOTH, expand=True)

        ttk.Label(box, text="Web端口").grid(row=0, column=0, sticky=tk.W, padx=8, pady=8)
        ttk.Entry(box, textvariable=self.web_port_var, width=10).grid(row=0, column=1, sticky=tk.W)

        self.web_start_btn = ttk.Button(box, text="启动Web服务", command=self._web_start)
        self.web_start_btn.grid(row=0, column=2, padx=8)
        self.web_stop_btn = ttk.Button(box, text="停止Web服务", command=self._web_stop, state=tk.DISABLED)
        self.web_stop_btn.grid(row=0, column=3, padx=8)

        ttk.Label(box, text="URL").grid(row=1, column=0, sticky=tk.W, padx=8)
        self.web_url_var = tk.StringVar(value="")
        ttk.Entry(box, textvariable=self.web_url_var, width=60, state="readonly").grid(row=1, column=1, columnspan=3, sticky=tk.W, padx=(0, 8), pady=(0, 8))

        # QR
        qr_frame = ttk.Frame(box)
        qr_frame.grid(row=2, column=0, columnspan=2, sticky=tk.W, padx=8, pady=(0, 8))
        self.qr_label = ttk.Label(qr_frame)
        self.qr_label.pack()
        ttk.Label(qr_frame, text="手机扫码打开网页，页面里有“上传/下载”。").pack(anchor=tk.W, pady=(8, 0))

        # File list
        ttk.Label(box, text="分享文件列表").grid(row=2, column=2, sticky=tk.NW, padx=8, pady=(0, 8))

        file_frame = ttk.Frame(box)
        file_frame.grid(row=2, column=3, sticky=tk.NW, padx=(0, 8), pady=(0, 8))

        self.web_file_list = tk.Listbox(file_frame, height=10, width=44, selectmode=tk.EXTENDED)
        self.web_file_list.grid(row=0, column=0, sticky=tk.W)
        yscroll = ttk.Scrollbar(file_frame, orient=tk.VERTICAL, command=self.web_file_list.yview)
        yscroll.grid(row=0, column=1, sticky=tk.NS)
        self.web_file_list.configure(yscrollcommand=yscroll.set)

        btns = ttk.Frame(box)
        btns.grid(row=3, column=3, sticky=tk.W, padx=(0, 8), pady=(0, 8))
        ttk.Button(btns, text="添加文件", command=self._web_add_files).pack(side=tk.LEFT)
        ttk.Button(btns, text="移除选中", command=self._web_remove_selected).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(btns, text="清空", command=self._web_clear).pack(side=tk.LEFT, padx=(8, 0))

        self.web_count_var = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.web_count_var).grid(row=4, column=3, sticky=tk.W, padx=(0, 8), pady=(0, 8))

        for c in range(4):
            box.grid_columnconfigure(c, weight=0)
        box.grid_columnconfigure(3, weight=1)

    def _build_todo_frame(self, root: ttk.Frame) -> None:
        lab = ttk.Labelframe(root, text="手机之间")
        lab.pack(fill=tk.BOTH, expand=True)
        ttk.Label(lab, text="功能开发中").pack(anchor=tk.W, padx=12, pady=12)

    def _on_mode_change(self) -> None:
        mode = self.mode_var.get()

        # 切换时停止不相关服务，避免端口冲突
        if mode != "电脑之间":
            self._p2p_stop(silent=True)
        if mode != "手机与电脑之间":
            self._web_stop(silent=True)

        for f in (self.p2p_frame, self.web_frame, self.todo_frame):
            f.pack_forget()

        if mode == "电脑之间":
            self.p2p_frame.pack(fill=tk.BOTH, expand=True)
        elif mode == "手机与电脑之间":
            self.web_frame.pack(fill=tk.BOTH, expand=True)
        else:
            self.todo_frame.pack(fill=tk.BOTH, expand=True)

    # ---------------- Common ----------------
    def _log_ui(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, line)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _poll_queues(self) -> None:
        while True:
            try:
                msg = self.log_q.get_nowait()
            except queue.Empty:
                break
            self._log_ui(msg)

        latest = None
        while True:
            try:
                latest = self.hosts_q.get_nowait()
            except queue.Empty:
                break

        if latest is not None and hasattr(self, "host_list"):
            ips = sorted(latest.keys())
            current = list(self.host_list.get(0, tk.END))
            if current != ips:
                self.host_list.delete(0, tk.END)
                for ip in ips:
                    self.host_list.insert(tk.END, ip)

        self.after(120, self._poll_queues)

    def refresh_local_ips(self) -> None:
        ips = list_local_ipv4_candidates()
        values = ips + (["0.0.0.0"] if "0.0.0.0" not in ips else [])
        self.bind_combo["values"] = values
        if not self.bind_ip_var.get() and values:
            self.bind_ip_var.set(values[0])

    def _init_bind_ip_default(self) -> None:
        self.refresh_local_ips()
        if not self.bind_ip_var.get():
            self.bind_ip_var.set("0.0.0.0")

    def choose_save_dir(self) -> None:
        path = filedialog.askdirectory(title="选择接收文件保存目录")
        if path:
            self.save_dir_var.set(path)

    # ---------------- P2P ----------------
    def _on_host_select(self, _evt=None) -> None:
        sel = self.host_list.curselection()
        if not sel:
            return
        ip = self.host_list.get(sel[0])
        self.p2p_target_ip_var.set(ip)

    def _p2p_add_files(self) -> None:
        paths = filedialog.askopenfilenames(title="添加要发送的文件（可多选）")
        if not paths:
            return
        existing = set(self.p2p_files)
        for p in paths:
            if p not in existing:
                self.p2p_files.append(p)
                existing.add(p)
        self._p2p_sync_list()

    def _p2p_remove_selected(self) -> None:
        sel = list(self.p2p_file_list.curselection())
        if not sel:
            return
        for idx in sorted(sel, reverse=True):
            try:
                del self.p2p_files[idx]
            except Exception:
                pass
        self._p2p_sync_list()

    def _p2p_clear(self) -> None:
        self.p2p_files = []
        self._p2p_sync_list()

    def _p2p_sync_list(self) -> None:
        self.p2p_file_list.delete(0, tk.END)
        for p in self.p2p_files:
            self.p2p_file_list.insert(tk.END, p)
        self.p2p_count_var.set(f"当前列表：{len(self.p2p_files)} 个文件" if self.p2p_files else "")

    def _p2p_start(self) -> None:
        bind_ip = (self.bind_ip_var.get() or "").strip()
        if not bind_ip:
            messagebox.showerror("错误", "请填写本机IP")
            return

        save_dir = (self.save_dir_var.get() or os.getcwd()).strip()

        try:
            self.discovery = DiscoveryService(bind_ip=bind_ip, log_q=self.log_q, hosts_q=self.hosts_q)
            self.discovery.start()

            self.tcp_server = TcpFileServer(bind_ip=bind_ip, save_dir=save_dir, log_q=self.log_q)
            self.tcp_server.start()

            self._log_ui("P2P 服务已启动")
            self.p2p_start_btn.configure(state=tk.DISABLED)
            self.p2p_stop_btn.configure(state=tk.NORMAL)
        except Exception as e:
            messagebox.showerror("启动失败", str(e))

    def _p2p_stop(self, silent: bool = False) -> None:
        if self.discovery:
            self.discovery.stop()
            self.discovery = None
        if self.tcp_server:
            self.tcp_server.stop()
            self.tcp_server = None

        if not silent:
            self._log_ui("P2P 服务已停止")
        if hasattr(self, "p2p_start_btn"):
            self.p2p_start_btn.configure(state=tk.NORMAL)
            self.p2p_stop_btn.configure(state=tk.DISABLED)

    def _p2p_send(self) -> None:
        target_ip = (self.p2p_target_ip_var.get() or "").strip()
        if not target_ip:
            messagebox.showerror("错误", "请填写目标IP")
            return
        if not self.p2p_files:
            messagebox.showerror("错误", "请先添加文件")
            return

        t = threading.Thread(target=send_files, args=(target_ip, list(self.p2p_files), self.log_q), daemon=True)
        t.start()

    # ---------------- WEB ----------------
    def _web_add_files(self) -> None:
        paths = filedialog.askopenfilenames(title="添加要分享的文件（可多选）")
        if not paths:
            return
        existing = set(self.web_files)
        for p in paths:
            if p not in existing:
                self.web_files.append(p)
                existing.add(p)
        self._web_sync_list(update_server=True)

    def _web_remove_selected(self) -> None:
        sel = list(self.web_file_list.curselection())
        if not sel:
            return
        for idx in sorted(sel, reverse=True):
            try:
                del self.web_files[idx]
            except Exception:
                pass
        self._web_sync_list(update_server=True)

    def _web_clear(self) -> None:
        self.web_files = []
        self._web_sync_list(update_server=True)

    def _web_sync_list(self, update_server: bool) -> None:
        self.web_file_list.delete(0, tk.END)
        for p in self.web_files:
            self.web_file_list.insert(tk.END, p)
        self.web_count_var.set(f"当前列表：{len(self.web_files)} 个文件" if self.web_files else "")

        if update_server and self.web_server:
            self.web_server.set_shared_files(self.web_files)

    def _web_start(self) -> None:
        bind_ip = (self.bind_ip_var.get() or "").strip()
        if not bind_ip or bind_ip == "0.0.0.0":
            messagebox.showerror("错误", "手机扫码需要一个具体的本机IP，请选择热点IP")
            return

        try:
            port = int((self.web_port_var.get() or "").strip())
        except ValueError:
            messagebox.showerror("错误", "端口必须是数字")
            return

        save_dir = (self.save_dir_var.get() or os.getcwd()).strip()

        try:
            self.web_server = WebTransferServer(
                host=bind_ip,
                port=port,
                save_dir=save_dir,
                log_cb=lambda m: self.log_q.put_nowait(m),
            )
            self.web_server.set_shared_files(self.web_files)
            self.web_server.start()

            url = self.web_server.url
            self.web_url_var.set(url)
            self._update_qr(url)

            self._log_ui("Web 服务已启动")
            self.web_start_btn.configure(state=tk.DISABLED)
            self.web_stop_btn.configure(state=tk.NORMAL)
        except Exception as e:
            messagebox.showerror("启动失败", str(e))

    def _web_stop(self, silent: bool = False) -> None:
        if self.web_server:
            try:
                self.web_server.stop()
            except Exception:
                pass
            self.web_server = None

        self.web_url_var.set("")
        self.qr_label.configure(image="")
        self._qr_photo = None

        if not silent:
            self._log_ui("Web 服务已停止")
        if hasattr(self, "web_start_btn"):
            self.web_start_btn.configure(state=tk.NORMAL)
            self.web_stop_btn.configure(state=tk.DISABLED)

    def _update_qr(self, url: str) -> None:
        qr = qrcode.QRCode(box_size=6, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        self._qr_photo = ImageTk.PhotoImage(img)
        self.qr_label.configure(image=self._qr_photo)

    # ---------------- Close ----------------
    def on_close(self) -> None:
        try:
            self._p2p_stop(silent=True)
        except Exception:
            pass
        try:
            self._web_stop(silent=True)
        except Exception:
            pass
        self.destroy()


def main() -> None:
    V2App().mainloop()


if __name__ == "__main__":
    main()
