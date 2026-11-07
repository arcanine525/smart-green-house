"""Loopback-only UI access when Docker Desktop cannot publish internal networks.

Each local TCP connection has a fixed Docker-exec stdio bridge to the owned app's
127.0.0.1:8501. Containers retain internal-only networks; there is no external
destination, dynamic target, proxy URL or additional Docker network attachment.
Use Ctrl-C to stop the host tunnel before snapshotting the rehearsal.
"""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import subprocess
import threading

BRIDGE = '''import os,socket,threading
connection=socket.create_connection(('127.0.0.1',8501),timeout=5)
connection.settimeout(None)
def receive():
    try:
        while True:
            data=os.read(0,65536)
            if not data:break
            connection.sendall(data)
    finally:
        try:connection.shutdown(socket.SHUT_WR)
        except OSError:pass
threading.Thread(target=receive,daemon=True).start()
try:
    while True:
        data=connection.recv(65536)
        if not data:break
        os.write(1,data)
finally:connection.close()
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--setup', type=Path, required=True)
    parser.add_argument('--target', choices=('source', 'restored', 'rebuilt'), default='source')
    args = parser.parse_args()
    setup = json.loads(args.setup.read_text())
    project = setup['project'] + ('' if args.target == 'source' else '-' + args.target)
    if not re.fullmatch(r'gh-ch-repro-[a-z0-9-]+', project):
        raise ValueError('Owned rehearsal project required')
    container = project + '-ch-app-1'
    inspected = json.loads(subprocess.check_output(['docker', 'inspect', container], text=True))[0]
    if inspected['Config']['Labels'].get('com.docker.compose.project') != project or not inspected['State']['Running']:
        raise ValueError('Running owned production app required')
    port = {'source':18582, 'restored':18583, 'rebuilt':18584}[args.target]

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            process = subprocess.Popen(['docker', 'exec', '-i', container, 'python', '-u', '-c', BRIDGE],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            def receive():
                try:
                    while True:
                        data = self.request.recv(65536)
                        if not data:
                            break
                        process.stdin.write(data)
                        process.stdin.flush()
                except (OSError, ValueError):
                    pass
                finally:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
            receiver = threading.Thread(target=receive, daemon=True)
            receiver.start()
            try:
                while True:
                    data = os.read(process.stdout.fileno(), 65536)
                    if not data:
                        break
                    self.request.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    self.request.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                receiver.join(timeout=2)
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server(('127.0.0.1', port), Handler) as server:
        print(json.dumps({'status':'listening','host':'127.0.0.1','port':port,
            'container':container,'fixed_destination':'127.0.0.1:8501','transport':'docker-exec-stdio'}),flush=True)
        try:
            server.serve_forever(poll_interval=.2)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
