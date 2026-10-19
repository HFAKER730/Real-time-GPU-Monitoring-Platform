"""Read-only SSH GPU sampler and a loopback-only dashboard.

Samples a remote NVIDIA GPU over SSH with nvidia-smi (read-only query) and
serves the dashboard exclusively on 127.0.0.1. The SSH password is never
written to disk; the remote host key is pinned to a project-local known_hosts
file using trust-on-first-use (TOFU).
"""
from __future__ import annotations

import argparse
import csv
import getpass
import io
import json
import math
import os
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import webbrowser

import paramiko

ROOT = Path(__file__).resolve().parent
KNOWN_HOSTS = ROOT / "known_hosts"

DEFAULT_HOST = "192.168.1.100"
DEFAULT_USER = "sshuser"
DEFAULT_DASHBOARD_PORT = 8877
DEFAULT_SSH_PORT = 22
DEFAULT_INTERVAL = 2
DEFAULT_HISTORY_MINUTES = 60
DEFAULT_GPU = "0"

# Alert thresholds (mirrored to the dashboard).
TEMP_WARN = 80
TEMP_CRIT = 87
MEMORY_WARN_PERCENT = 95

LOG_FIELDS = [
    "time", "remote_time", "uuid", "name", "driver",
    "utilization", "memory_used", "memory_total", "memory_percent",
    "temperature", "power", "power_limit", "sequence", "segment",
]


def number(value):
    try:
        n = float(value.strip())
        return n if math.isfinite(n) else None
    except (ValueError, AttributeError):
        return None


def build_query(gpu, interval):
    return (
        f"nvidia-smi -i {gpu} --query-gpu=timestamp,uuid,name,driver_version,"
        "utilization.gpu,memory.used,memory.total,temperature.gpu,"
        f"power.draw,power.limit --format=csv,noheader,nounits -l {interval}"
    )


def parse_sample(line):
    row = next(csv.reader([line]))
    if len(row) != 10:
        raise ValueError("Unexpected GPU response")
    stamp, uuid, name, driver, util, used, total, temp, power, power_limit = [
        v.strip() for v in row
    ]
    util, used, total = (number(v) for v in (util, used, total))
    temp, power, power_limit = (number(v) for v in (temp, power, power_limit))
    if (util is None or used is None or total is None
            or not 0 <= util <= 100
            or not 0 <= used <= total or total <= 0):
        raise ValueError("Invalid GPU metrics")
    return dict(
        time=round(time.time(), 3), remote_time=stamp, uuid=uuid,
        name=name, driver=driver, utilization=util, memory_used=used,
        memory_total=total, memory_percent=round(100 * used / total, 2),
        temperature=temp, power=power, power_limit=power_limit,
    )


class Monitor:
    def __init__(self, password, host, user, ssh_port, gpu, interval,
                 history_minutes, log_path):
        self.password = password
        self.host = host
        self.user = user
        self.ssh_port = ssh_port
        self.gpu = gpu
        self.interval = interval
        self.query = build_query(gpu, interval)
        capacity = max(10, history_minutes * 60 // interval)
        self.samples = deque(maxlen=capacity)
        self.history_minutes = history_minutes
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.client = None
        self.status = "connecting"
        self.error = None
        self.segment = 0
        self.sequence = 0
        self.started = time.time()
        self.fingerprint = None
        self.tofu = False

        self.log_path = log_path
        self.log_file = None
        self.log_writer = None
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            is_new = not log_path.exists() or log_path.stat().st_size == 0
            self.log_file = log_path.open("a", newline="", encoding="utf-8")
            self.log_writer = csv.DictWriter(
                self.log_file, fieldnames=LOG_FIELDS, extrasaction="ignore"
            )
            if is_new:
                self.log_writer.writeheader()
                self.log_file.flush()

    def state(self, status, error=None):
        with self.lock:
            self.status, self.error = status, error

    def config(self):
        return dict(
            host=self.host, user=self.user, ssh_port=self.ssh_port,
            gpu=self.gpu, interval=self.interval,
            history_minutes=self.history_minutes,
            thresholds=dict(
                temp_warn=TEMP_WARN, temp_crit=TEMP_CRIT,
                memory_warn_percent=MEMORY_WARN_PERCENT,
            ),
        )

    def snapshot(self):
        with self.lock:
            now = time.time()
            samples = list(self.samples)
            latest = samples[-1] if samples else None
            age = now - latest["time"] if latest else None
            status = self.status
            if status == "live" and (age is None or age > self.interval * 4):
                status = "stale"
            return dict(
                host=self.host, config=self.config(), status=status,
                error=self.error, started=self.started, server_time=now,
                uptime=now - self.started, age=age, latest=latest,
                samples=samples, sequence=self.sequence,
                fingerprint=self.fingerprint, tofu=self.tofu,
            )

    def export_csv(self):
        with self.lock:
            samples = list(self.samples)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=LOG_FIELDS,
                                extrasaction="ignore")
        writer.writeheader()
        for sample in samples:
            writer.writerow(sample)
        return buf.getvalue().encode("utf-8")

    def _record(self, sample):
        with self.lock:
            if self.samples and self.samples[-1]["uuid"] != sample["uuid"]:
                self.samples.clear()
            self.sequence += 1
            sample.update(sequence=self.sequence, segment=self.segment)
            self.samples.append(sample)
            self.status, self.error = "live", None
            if self.log_writer is not None:
                try:
                    self.log_writer.writerow(sample)
                    self.log_file.flush()
                except OSError:
                    pass

    def _connect(self):
        client = paramiko.SSHClient()
        known = False
        if KNOWN_HOSTS.exists():
            try:
                client.load_host_keys(str(KNOWN_HOSTS))
                known = client.get_host_keys().lookup(self.host) is not None
            except Exception:
                known = False
        if known:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            self.host, port=self.ssh_port, username=self.user,
            password=self.password, timeout=8, auth_timeout=8,
            banner_timeout=8, look_for_keys=False, allow_agent=False,
        )
        client.get_transport().set_keepalive(15)
        if not known:
            # Trust-on-first-use: persist the key presented on first connect.
            client.get_host_keys().save(str(KNOWN_HOSTS))
            self.tofu = True
        entry = client.get_host_keys().lookup(self.host)
        if entry:
            key = next(iter(entry.values()))
            get_fp = getattr(key, "get_fingerprint", None)
            if get_fp is not None:
                digest = get_fp()
                self.fingerprint = ":".join(f"{b:02x}" for b in digest)
        return client

    def run(self):
        while not self.stop.is_set():
            client = paramiko.SSHClient()
            try:
                self.state("connecting")
                client = self._connect()
                self.client = client
                self.segment += 1
                _, stdout, stderr = client.exec_command(self.query, timeout=10)
                stdout.channel.settimeout(max(self.interval * 4, 10))
                while not self.stop.is_set():
                    try:
                        line = stdout.readline()
                    except socket.timeout:
                        continue
                    if not line:
                        detail = stderr.read().decode("utf-8", "replace").strip()
                        if detail:
                            raise ConnectionError(
                                "nvidia-smi 异常退出：" + detail[:200]
                            )
                        raise ConnectionError("GPU sample stream closed")
                    try:
                        sample = parse_sample(line)
                    except ValueError:
                        # Skip banner/warning lines that nvidia-smi may emit.
                        continue
                    self._record(sample)
            except paramiko.AuthenticationException:
                self.state("auth_error", "SSH 登录失败，请重新启动并输入密码。")
                return
            except paramiko.BadHostKeyException:
                self.state(
                    "host_key_error",
                    "SSH 主机密钥与本机记录不同（可能重装系统或存在中间人），"
                    "连接已停止。确认安全后可用 --reset-key 重置。",
                )
                return
            except Exception as exc:
                if not self.stop.is_set():
                    self.state(
                        "disconnected",
                        "连接或采样中断，5 秒后自动重试。（"
                        + type(exc).__name__ + "）",
                    )
            finally:
                client.close()
                self.client = None
            self.stop.wait(5)

    def close(self):
        self.stop.set()
        if self.client:
            self.client.close()
        if self.log_file:
            try:
                self.log_file.close()
            except OSError:
                pass


def make_handler(monitor, port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Host") not in {
                f"127.0.0.1:{port}", f"localhost:{port}",
            }:
                self.send_error(403)
                return
            route = self.path.split("?", 1)[0]
            if route == "/api/samples":
                data = json.dumps(
                    monitor.snapshot(), ensure_ascii=False, allow_nan=False
                ).encode("utf-8")
                kind = "application/json; charset=utf-8"
                extra = None
            elif route == "/api/export.csv":
                data = monitor.export_csv()
                kind = "text/csv; charset=utf-8"
                extra = [
                    ("Content-Disposition",
                     'attachment; filename="gpu-samples.csv"'),
                ]
            elif route in ("/", "/index.html"):
                data = (ROOT / "index.html").read_bytes()
                kind = "text/html; charset=utf-8"
                extra = None
            elif route == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "img-src 'self' data:; frame-ancestors 'self'",
            )
            for name, value in extra or []:
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(
        description="SSH-based read-only GPU monitoring dashboard."
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="远程主机地址")
    parser.add_argument("--user", default=DEFAULT_USER, help="SSH 用户名")
    parser.add_argument("--ssh-port", type=int, default=DEFAULT_SSH_PORT,
                        help="SSH 端口")
    parser.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT,
                        help="本机仪表盘端口")
    parser.add_argument("--gpu", default=DEFAULT_GPU,
                        help="GPU 编号（nvidia-smi -i，默认 0）")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help="采样间隔秒数")
    parser.add_argument("--history", type=int, default=DEFAULT_HISTORY_MINUTES,
                        help="内存中保留的历史分钟数")
    parser.add_argument("--log", type=Path, default=None,
                        help="可选：将样本持续追加写入的 CSV 文件路径")
    parser.add_argument("--reset-key", action="store_true",
                        help="删除已固化的远程主机密钥后退出")
    parser.add_argument("--open", action="store_true", help="自动打开浏览器")
    args = parser.parse_args()

    if args.reset_key:
        if KNOWN_HOSTS.exists():
            KNOWN_HOSTS.unlink()
            print(f"已删除固化的主机密钥：{KNOWN_HOSTS}")
        else:
            print("没有已固化的主机密钥。")
        return

    password = (os.environ.pop("GPU_MONITOR_PASSWORD", None)
                or getpass.getpass(f"SSH password for {args.user}@{args.host}: "))
    monitor = Monitor(
        password, host=args.host, user=args.user, ssh_port=args.ssh_port,
        gpu=args.gpu, interval=args.interval, history_minutes=args.history,
        log_path=args.log,
    )
    server = ThreadingHTTPServer(("127.0.0.1", args.port),
                                 make_handler(monitor, args.port))
    thread = threading.Thread(target=monitor.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{args.port}"
    print(f"GPU dashboard: {url} | Ctrl+C to stop", flush=True)
    print(f"目标：{args.user}@{args.host}:{args.ssh_port} GPU {args.gpu} · "
          f"每 {args.interval} 秒采样 · 保留最近 {args.history} 分钟",
          flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        monitor.close()
        server.server_close()


if __name__ == "__main__":
    main()
