# Proxy minimo de la Docker Engine API para el panel de passivbot (ver docs/panel-passivbot.md).
# Solo stdlib: no hay pip install.
# python:3.11-slim-bookworm (3.11.16), digest del indice multi-arch verificado 2026-10-01.
FROM python:3.11-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 10001 dockerproxy \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin dockerproxy \
    && mkdir -m 0755 /app

COPY --chown=root:root --chmod=0444 docker_proxy.py /app/docker_proxy.py

# Usuario no-root. El acceso al socket se da desde compose con group_add (GID del socket del host).
USER 10001:10001
EXPOSE 2375

# Liveness sin tocar Docker: una ruta no permitida tiene que responder 403.
HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD ["python", "-c", "import sys,urllib.request,urllib.error\ntry:\n urllib.request.urlopen('http://127.0.0.1:2375/_ping', timeout=2)\nexcept urllib.error.HTTPError as e:\n sys.exit(0 if e.code == 403 else 1)\nexcept Exception:\n sys.exit(1)\nsys.exit(1)"]

ENTRYPOINT ["python", "-I", "/app/docker_proxy.py"]
