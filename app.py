import os
import queue
import socket
import struct
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk


UDP_PORT = 11450
TCP_PORT = 11451
HEARTBEAT_INTERVAL_SEC = 1.5
HOST_TIMEOUT_SEC = 6.0

MAGIC = b"LFT1"  # LAN File Transfer v1.1
HEADER_STRUCT = struct.Struct("!4sHQQ")
# magic(4) | name_len(u16) | file_size(u64) | mtime_ns(u64) | name_bytes | file_bytes


@dataclass
class HostInfo:
    ip: str
    last_seen: float


def _now() -> float:
    return time.time()


def list_local_ipv4_candidates() -> list[str]:
    candidates: set[str] = set()

    try:
        hostname = socket.gethostname()
        for family, _, _, _, sockaddr in socket.getaddrinfo(hostname, None):
            if family == socket.AF_INET:
                ip = sockaddr[0]
                if not ip.startswith("127."):
                    candidates.add(ip)
    except Exception:
        pass

    # Fallback: default route IP inference (works even without Internet if routing exists)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                candidates.add(ip)
    except Exception:
        pass

    return sorted(candidates)


def recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks: list[bytes] = []
    received = 0
    while received < n:
        chunk = sock.recv(min(65536, n - received))
        if not chunk:
            raise ConnectionError("connection closed")
        chunks.append(chunk)
        received += len(chunk)
    return b"".join(chunks)


def safe_filename(name: str) -> str:
    name = name.replace("\\", "_").replace("/", "_").replace(":", "_")
    name = name.strip().strip(".")
    return name or "received_file"


class DiscoveryService:
    def __init__(self, bind_ip: str, log_q: "queue.Queue[str]", hosts_q: "queue.Queue[dict[str, HostInfo]]"):
        self.bind_ip = bind_ip
        self.log_q = log_q
        self.hosts_q = hosts_q

        self._stop = threading.Event()
        self._hosts: dict[str, HostInfo] = {}
        self._lock = threading.Lock()

        self._rx_thread = threading.Thread(target=self._udp_rx_loop, name="udp-rx", daemon=True)
        self._tx_thread = threading.Thread(target=self._udp_tx_loop, name="udp-tx", daemon=True)
        self._gc_thread = threading.Thread(target=self._gc_loop, name="host-gc", daemon=True)

    def start(self) -> None:
        self._rx_thread.start()
        self._tx_thread.start()
        self._gc_thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot_hosts(self) -> dict[str, HostInfo]:
        with self._lock:
            return dict(self._hosts)

    def _log(self, msg: str) -> None:
        try:
            self.log_q.put_nowait(msg)
        except Exception:
            pass

    def _update_host(self, ip: str) -> None:
        with self._lock:
            self._hosts[ip] = HostInfo(ip=ip, last_seen=_now())
            snap = dict(self._hosts)
        try:
            self.hosts_q.put_nowait(snap)
        except Exception:
            pass

    def _udp_rx_loop(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.bind_ip, UDP_PORT))
            s.settimeout(0.5)
            self._log(f"UDP listening on {self.bind_ip}:{UDP_PORT}")

            while not self._stop.is_set():
                try:
                    data, addr = s.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break

                peer_ip = addr[0]
                if peer_ip == self.bind_ip:
                    # 在某些网络里你也会收到自己的广播包；忽略掉避免自发现。
                    continue

                if data == b"H1":
                    self._update_host(peer_ip)
                    try:
                        s.sendto(b"H2", (peer_ip, UDP_PORT))
                    except OSError:
                        pass
                elif data == b"H2":
                    self._update_host(peer_ip)
        finally:
            try:
                s.close()
            except Exception:
                pass

    def _udp_tx_loop(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            # 绑定本地 IP 以走指定网卡（更贴合“绑定热点 ip”这个要求）
            try:
                s.bind((self.bind_ip, 0))
            except OSError:
                # 如果绑定失败，退回让系统选择网卡
                pass

            next_ts = 0.0
            while not self._stop.is_set():
                now = _now()
                if now >= next_ts:
                    next_ts = now + HEARTBEAT_INTERVAL_SEC
                    try:
                        s.sendto(b"H1", ("255.255.255.255", UDP_PORT))
                    except OSError:
                        pass
                time.sleep(0.05)
        finally:
            try:
                s.close()
            except Exception:
                pass

    def _gc_loop(self) -> None:
        while not self._stop.is_set():
            cutoff = _now() - HOST_TIMEOUT_SEC
            changed = False
            with self._lock:
                for ip in list(self._hosts.keys()):
                    if self._hosts[ip].last_seen < cutoff:
                        del self._hosts[ip]
                        changed = True
                snap = dict(self._hosts)
            if changed:
                try:
                    self.hosts_q.put_nowait(snap)
                except Exception:
                    pass
            time.sleep(0.5)


class TcpFileServer:
    def __init__(self, bind_ip: str, save_dir: str, log_q: "queue.Queue[str]"):
        self.bind_ip = bind_ip
        self.save_dir = save_dir
        self.log_q = log_q
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="tcp-server", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # accept() 使用超时轮询检查 _stop，无需额外唤醒连接

    def _log(self, msg: str) -> None:
        try:
            self.log_q.put_nowait(msg)
        except Exception:
            pass

    def _serve(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self.bind_ip, TCP_PORT))
            srv.listen(5)
            srv.settimeout(0.5)
            self._log(f"TCP listening on {self.bind_ip}:{TCP_PORT}")

            while not self._stop.is_set():
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break

                t = threading.Thread(target=self._handle_conn, args=(conn, addr), daemon=True)
                t.start()
        finally:
            try:
                srv.close()
            except Exception:
                pass

    def _handle_conn(self, conn: socket.socket, addr) -> None:
        peer_ip = addr[0]
        try:
            conn.settimeout(10)
            header = recv_exact(conn, HEADER_STRUCT.size)
            magic, name_len, file_size, mtime_ns = HEADER_STRUCT.unpack(header)
            if magic != MAGIC:
                self._log(f"Reject {peer_ip}: bad magic")
                return

            name_bytes = recv_exact(conn, name_len)
            raw_name = name_bytes.decode("utf-8", errors="replace")
            filename = safe_filename(os.path.basename(raw_name))

            os.makedirs(self.save_dir, exist_ok=True)
            out_path = os.path.join(self.save_dir, filename)
            base, ext = os.path.splitext(out_path)
            idx = 1
            while os.path.exists(out_path):
                out_path = f"{base} ({idx}){ext}"
                idx += 1

            self._log(f"Receiving {file_size} bytes from {peer_ip} -> {os.path.basename(out_path)}")

            remaining = file_size
            with open(out_path, "wb") as f:
                while remaining > 0:
                    chunk = conn.recv(min(65536, remaining))
                    if not chunk:
                        raise ConnectionError("connection closed while receiving")
                    f.write(chunk)
                    remaining -= len(chunk)

            try:
                os.utime(out_path, ns=(mtime_ns, mtime_ns))
            except Exception:
                pass

            self._log(f"Saved: {out_path}")
        except Exception as e:
            self._log(f"Receive error from {peer_ip}: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass


def send_file(target_ip: str, file_path: str, log_q: "queue.Queue[str]") -> None:
    def _log(msg: str) -> None:
        try:
            log_q.put_nowait(msg)
        except Exception:
            pass

    file_path = os.path.abspath(file_path)
    if not os.path.isfile(file_path):
        _log("File not found")
        return

    name = os.path.basename(file_path)
    name_bytes = name.encode("utf-8")
    st = os.stat(file_path)
    file_size = st.st_size
    mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))

    header = HEADER_STRUCT.pack(MAGIC, len(name_bytes), file_size, mtime_ns)

    try:
        _log(f"Connecting to {target_ip}:{TCP_PORT} ...")
        with socket.create_connection((target_ip, TCP_PORT), timeout=5) as s:
            s.settimeout(10)
            s.sendall(header)
            s.sendall(name_bytes)

            sent = 0
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    s.sendall(chunk)
                    sent += len(chunk)

            _log(f"Sent {sent} bytes: {name}")
    except Exception as e:
        _log(f"Send error: {e}")


def send_files(target_ip: str, file_paths: list[str], log_q: "queue.Queue[str]") -> None:
    cleaned: list[str] = []
    for p in file_paths:
        p = (p or "").strip()
        if p:
            cleaned.append(p)

    if not cleaned:
        try:
            log_q.put_nowait("No files selected")
        except Exception:
            pass
        return

    try:
        log_q.put_nowait(f"Batch send: {len(cleaned)} file(s)")
    except Exception:
        pass

    for idx, p in enumerate(cleaned, start=1):
        try:
            log_q.put_nowait(f"[{idx}/{len(cleaned)}] {os.path.basename(p)}")
        except Exception:
            pass
        send_file(target_ip, p, log_q)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("局域网文件传输 v1.2.1")
        self.geometry("720x460")

        self.log_q: "queue.Queue[str]" = queue.Queue()
        self.hosts_q: "queue.Queue[dict[str, HostInfo]]" = queue.Queue()

        self.bind_ip_var = tk.StringVar(value="")
        self.target_ip_var = tk.StringVar(value="")
        self.files_count_var = tk.StringVar(value="")
        self.save_dir_var = tk.StringVar(value=os.getcwd())

        self.selected_files: list[str] = []

        self.discovery: DiscoveryService | None = None
        self.tcp_server: TcpFileServer | None = None

        self._build_ui()
        self._init_bind_ip_default()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._poll_queues)

    def _build_ui(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(top, text="本机IP(热点IP)").grid(row=0, column=0, sticky=tk.W)
        self.bind_combo = ttk.Combobox(top, textvariable=self.bind_ip_var, width=18, values=[])
        self.bind_combo.grid(row=0, column=1, sticky=tk.W, padx=(8, 0))
        ttk.Button(top, text="刷新", command=self.refresh_local_ips).grid(row=0, column=2, padx=8)

        ttk.Label(top, text="保存目录").grid(row=0, column=3, sticky=tk.W, padx=(16, 0))
        save_entry = ttk.Entry(top, textvariable=self.save_dir_var, width=30)
        save_entry.grid(row=0, column=4, sticky=tk.W, padx=(8, 0))
        ttk.Button(top, text="选择", command=self.choose_save_dir).grid(row=0, column=5, padx=8)

        mid = ttk.Frame(self)
        mid.pack(fill=tk.BOTH, expand=True, padx=10)

        left = ttk.Labelframe(mid, text="发现的主机")
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=False)

        self.host_list = tk.Listbox(left, height=14, width=26)
        self.host_list.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.host_list.bind("<<ListboxSelect>>", self.on_host_select)

        right = ttk.Frame(mid)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0))

        send_box = ttk.Labelframe(right, text="发送")
        send_box.pack(fill=tk.X)

        ttk.Label(send_box, text="目标IP").grid(row=0, column=0, sticky=tk.W, padx=8, pady=8)
        ttk.Entry(send_box, textvariable=self.target_ip_var, width=22).grid(row=0, column=1, sticky=tk.W)

        ttk.Label(send_box, text="文件列表").grid(row=1, column=0, sticky=tk.NW, padx=8, pady=8)

        file_frame = ttk.Frame(send_box)
        file_frame.grid(row=1, column=1, sticky=tk.W, padx=(0, 0), pady=8)

        self.file_list = tk.Listbox(file_frame, height=6, width=52, selectmode=tk.EXTENDED)
        self.file_list.grid(row=0, column=0, sticky=tk.W)
        yscroll = ttk.Scrollbar(file_frame, orient=tk.VERTICAL, command=self.file_list.yview)
        yscroll.grid(row=0, column=1, sticky=tk.NS)
        self.file_list.configure(yscrollcommand=yscroll.set)

        btns = ttk.Frame(send_box)
        btns.grid(row=1, column=2, padx=8, sticky=tk.NW, pady=8)
        ttk.Button(btns, text="添加文件", command=self.add_files).pack(fill=tk.X)
        ttk.Button(btns, text="移除选中", command=self.remove_selected_files).pack(fill=tk.X, pady=(6, 0))
        ttk.Button(btns, text="清空", command=self.clear_files).pack(fill=tk.X, pady=(6, 0))

        ttk.Label(send_box, textvariable=self.files_count_var).grid(row=2, column=1, sticky=tk.W, padx=8)

        actions = ttk.Frame(send_box)
        actions.grid(row=3, column=1, sticky=tk.W, pady=(0, 8))
        ttk.Button(actions, text="一键传输", command=self.on_send_selected).pack(side=tk.LEFT)

        ctrl_box = ttk.Labelframe(right, text="服务")
        ctrl_box.pack(fill=tk.X, pady=(10, 0))

        self.start_btn = ttk.Button(ctrl_box, text="启动发现/接收", command=self.on_start)
        self.start_btn.grid(row=0, column=0, padx=8, pady=8)
        self.stop_btn = ttk.Button(ctrl_box, text="停止", command=self.on_stop, state=tk.DISABLED)
        self.stop_btn.grid(row=0, column=1, padx=8, pady=8)

        log_box = ttk.Labelframe(right, text="日志")
        log_box.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        self.log_text = tk.Text(log_box, height=12, wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    def _init_bind_ip_default(self) -> None:
        self.refresh_local_ips()
        if not self.bind_ip_var.get():
            self.bind_ip_var.set("0.0.0.0")

    def refresh_local_ips(self) -> None:
        ips = list_local_ipv4_candidates()
        values = ips + (["0.0.0.0"] if "0.0.0.0" not in ips else [])
        self.bind_combo["values"] = values
        if not self.bind_ip_var.get() and values:
            self.bind_ip_var.set(values[0])

    def add_files(self) -> None:
        paths = filedialog.askopenfilenames(title="添加要发送的文件（可多选）")
        if not paths:
            return

        # 去重并保持顺序
        existing = set(self.selected_files)
        added = 0
        for p in paths:
            if p not in existing:
                self.selected_files.append(p)
                existing.add(p)
                added += 1

        if added:
            self._sync_file_listbox()

    def _update_files_count(self) -> None:
        n = len(self.selected_files)
        self.files_count_var.set(f"当前列表：{n} 个文件" if n else "")

    def _sync_file_listbox(self) -> None:
        self.file_list.delete(0, tk.END)
        for p in self.selected_files:
            self.file_list.insert(tk.END, p)
        self._update_files_count()

    def remove_selected_files(self) -> None:
        sel = list(self.file_list.curselection())
        if not sel:
            return
        for idx in sorted(sel, reverse=True):
            try:
                del self.selected_files[idx]
            except Exception:
                pass
        self._sync_file_listbox()

    def clear_files(self) -> None:
        self.selected_files = []
        self._sync_file_listbox()

    def choose_save_dir(self) -> None:
        path = filedialog.askdirectory(title="选择接收文件保存目录")
        if path:
            self.save_dir_var.set(path)

    def on_host_select(self, _evt=None) -> None:
        sel = self.host_list.curselection()
        if not sel:
            return
        ip = self.host_list.get(sel[0])
        self.target_ip_var.set(ip)

    def on_start(self) -> None:
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

            self._log_ui("服务已启动")
            self.start_btn.configure(state=tk.DISABLED)
            self.stop_btn.configure(state=tk.NORMAL)
        except Exception as e:
            messagebox.showerror("启动失败", str(e))

    def on_stop(self) -> None:
        if self.discovery:
            self.discovery.stop()
            self.discovery = None
        if self.tcp_server:
            self.tcp_server.stop()
            self.tcp_server = None

        self._log_ui("服务已停止")
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)

    def on_send_selected(self) -> None:
        target_ip = (self.target_ip_var.get() or "").strip()
        if not target_ip:
            messagebox.showerror("错误", "请填写目标IP")
            return
        if not self.selected_files:
            messagebox.showerror("错误", "请先添加文件")
            return

        t = threading.Thread(target=send_files, args=(target_ip, list(self.selected_files), self.log_q), daemon=True)
        t.start()

    def _log_ui(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, line)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _poll_queues(self) -> None:
        # Logs
        while True:
            try:
                msg = self.log_q.get_nowait()
            except queue.Empty:
                break
            self._log_ui(msg)

        # Hosts
        latest: dict[str, HostInfo] | None = None
        while True:
            try:
                latest = self.hosts_q.get_nowait()
            except queue.Empty:
                break

        if latest is not None:
            ips = sorted(latest.keys())
            current = list(self.host_list.get(0, tk.END))
            if current != ips:
                self.host_list.delete(0, tk.END)
                for ip in ips:
                    self.host_list.insert(tk.END, ip)

        self.after(120, self._poll_queues)

    def on_close(self) -> None:
        try:
            self.on_stop()
        except Exception:
            pass
        self.destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
