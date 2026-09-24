"""
Servidor local mínimo para la página de generación de video con Wan3.0.

Solo usa la librería estándar de Python (3.8+). Sirve index.html y reenvía las
llamadas a Model Studio, porque la API de Alibaba no permite llamadas directas
desde el navegador (no envía cabeceras CORS).

    python servidor.py

Escucha solo en 127.0.0.1: nadie más en la red puede usarlo. La API key la
escribe cada persona en la página y viaja únicamente a este servidor local y de
ahí a Alibaba; no se guarda en disco.
"""

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REGIONES = {
    "beijing": "cn-beijing",
    "singapore": "ap-southeast-1",
    "tokyo": "ap-northeast-1",
    "frankfurt": "eu-central-1",
    "virginia": "us-east-1",
    "hongkong": "cn-hongkong",
}
AQUI = Path(__file__).parent
# En un hosting en la nube la plataforma define PORT: ahí se escucha en toda la red.
# En local (sin PORT) solo en 127.0.0.1:8000.
EN_NUBE = "PORT" in os.environ
HOST = "0.0.0.0" if EN_NUBE else "127.0.0.1"
PORT = int(os.environ.get("PORT", 8000))
RUTA_CREAR = "/api/v1/services/aigc/video-generation/video-synthesis"
MAX_CUERPO = 64 * 1024
LIMITE_POR_MINUTO = 60

_visitas = defaultdict(deque)
_candado = threading.Lock()


def excede_limite(ip):
    ahora = time.time()
    with _candado:
        q = _visitas[ip]
        while q and ahora - q[0] > 60:
            q.popleft()
        if len(q) >= LIMITE_POR_MINUTO:
            return True
        q.append(ahora)
        if len(_visitas) > 5000:
            for k in [k for k, v in _visitas.items() if not v]:
                del _visitas[k]
    return False


def base_url(workspace_id, region):
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", workspace_id or ""):
        raise ValueError("Workspace ID inválido")
    if region not in REGIONES:
        raise ValueError("Región inválida")
    return f"https://{workspace_id}.{REGIONES[region]}.maas.aliyuncs.com"


def llamar(metodo, url, api_key, cuerpo=None, async_=False):
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(url, data=datos, method=metodo)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    if async_:
        req.add_header("X-DashScope-Async", "enable")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        msg = {"code": "ConexionFallida", "message": str(e.reason)}
        return 502, json.dumps(msg).encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _ip(self):
        reenviada = self.headers.get("X-Forwarded-For", "")
        return reenviada.split(",")[0].strip() or self.client_address[0]

    def _responder(self, status, cuerpo, tipo="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _error(self, status, codigo, mensaje):
        self._responder(status, json.dumps({"code": codigo, "message": mensaje}).encode())

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/salud":
            self._responder(200, b'{"ok": true}')
        elif excede_limite(self._ip()):
            self._error(429, "DemasiadasPeticiones", "Demasiadas peticiones, espera un minuto")
        elif u.path in ("/", "/index.html"):
            self._responder(200, (AQUI / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif u.path == "/descargar":
            self._descargar(parse_qs(u.query))
        else:
            self._error(404, "NoEncontrado", "Ruta inexistente")

    def do_POST(self):
        if excede_limite(self._ip()):
            return self._error(429, "DemasiadasPeticiones", "Demasiadas peticiones, espera un minuto")
        try:
            largo = int(self.headers.get("Content-Length", 0))
            if largo > MAX_CUERPO:
                return self._error(413, "SolicitudInvalida", "Cuerpo demasiado grande")
            entrada = json.loads(self.rfile.read(largo) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._error(400, "SolicitudInvalida", "JSON inválido")

        api_key = entrada.get("api_key", "").strip()
        if not api_key:
            return self._error(400, "SolicitudInvalida", "Falta la API key")
        try:
            base = base_url(entrada.get("workspace_id", "").strip(), entrada.get("region", ""))
        except ValueError as e:
            return self._error(400, "SolicitudInvalida", str(e))

        if self.path == "/api/crear":
            payload = entrada.get("payload")
            if not isinstance(payload, dict):
                return self._error(400, "SolicitudInvalida", "Falta el payload")
            status, cuerpo = llamar("POST", base + RUTA_CREAR, api_key, payload, async_=True)
        elif self.path == "/api/estado":
            task_id = entrada.get("task_id", "")
            if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", task_id):
                return self._error(400, "SolicitudInvalida", "task_id inválido")
            status, cuerpo = llamar("GET", f"{base}/api/v1/tasks/{task_id}", api_key)
        else:
            return self._error(404, "NoEncontrado", "Ruta inexistente")
        self._responder(status, cuerpo)

    def _descargar(self, query):
        destino = (query.get("url") or [""])[0]
        u = urlparse(destino)
        if u.scheme not in ("http", "https") or not (u.hostname or "").endswith(".aliyuncs.com"):
            return self._error(400, "SolicitudInvalida", "Solo se permite descargar desde aliyuncs.com")
        try:
            with urllib.request.urlopen(destino, timeout=120) as r:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Disposition", 'attachment; filename="video_wan3.mp4"')
                if r.headers.get("Content-Length"):
                    self.send_header("Content-Length", r.headers["Content-Length"])
                self.end_headers()
                while True:
                    trozo = r.read(1 << 20)
                    if not trozo:
                        break
                    self.wfile.write(trozo)
        except (urllib.error.URLError, OSError) as e:
            self._error(502, "DescargaFallida", str(e))


def main():
    try:
        servidor = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"No se pudo abrir el puerto {PORT}: ¿ya hay otra copia corriendo?")
        sys.exit(1)
    url = f"http://{HOST}:{PORT}"
    print(f"Listo: {url}  (Ctrl+C para detener)", flush=True)
    if not EN_NUBE and "--no-abrir" not in sys.argv:
        webbrowser.open(url)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nDetenido.")


if __name__ == "__main__":
    main()
