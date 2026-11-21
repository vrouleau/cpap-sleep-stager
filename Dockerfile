FROM python:3.12-slim

# The optional sleepecg engine (keras + jax, ~400 MB) is baked in; whether the app
# uses it is the runtime switch SLEEPECG=true/false. WITH_SLEEPECG=false builds a
# slimmer image without it.
ARG WITH_SLEEPECG=true
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DATA_DIR=/data KERAS_BACKEND=jax PIP_NO_CACHE_DIR=1

WORKDIR /srv
COPY requirements.txt requirements-sleepecg.txt ./
RUN pip install -r requirements.txt \
 && if [ "$WITH_SLEEPECG" = "true" ]; then \
      pip install -r requirements-sleepecg.txt \
      && python -c "import sleepecg, importlib.util as u; assert u.find_spec('keras') and u.find_spec('jax')"; \
    fi

COPY app ./app
COPY scripts ./scripts

# shown at the bottom of the sidebar and in /api/health; set by the release workflow
ARG APP_VERSION=dev
ARG APP_COMMIT=
ENV APP_VERSION=$APP_VERSION APP_COMMIT=$APP_COMMIT

RUN mkdir -p /data
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
