# Multistage: wheels are built once in a stage that carries a compiler, and
# the image that ships has neither the compiler nor the build cache.
#
# No secret appears here. Every one arrives at runtime through the
# environment, which is why .env is in .dockerignore as well as .gitignore -
# a COPY . . with .env present bakes the key into a layer, and a layer is
# forever even if the file is deleted in the next instruction.

FROM python:3.10.12-slim AS build

WORKDIR /build

# Requirements first, so a code change does not re-resolve dependencies.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


FROM python:3.10.12-slim AS runtime

# Not root. A voice session parses audio off the network; if anything in that
# path is ever exploitable, it should not be exploitable as uid 0.
RUN useradd --create-home --uid 10001 poorvika

WORKDIR /app

COPY --from=build /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
 && rm -rf /wheels

COPY src ./src

# Where traces are written. Owned by the runtime user, and a volume in any
# real deployment: they are call records and do not belong in the image.
RUN mkdir -p /app/out/traces && chown -R poorvika:poorvika /app

USER poorvika

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=main \
    PORT=8080

EXPOSE 8080

# The same endpoint the load balancer uses. It contacts nothing downstream,
# so a Gemini outage does not restart the container.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "--factory", "src.web.app:create_app", \
     "--host", "0.0.0.0", "--port", "8080"]
