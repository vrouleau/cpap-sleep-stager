FROM python:3.12-slim

# sleepecg is normally switched on at runtime (SLEEPECG=true installs it into /data on
# first start). WITH_SLEEPECG=true bakes it into the image instead, e.g. for offline use.
ARG WITH_SLEEPECG=false
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DATA_DIR=/data KERAS_BACKEND=jax PIP_NO_CACHE_DIR=1

WORKDIR /srv
COPY requirements.txt requirements-sleepecg.txt ./
RUN pip install -r requirements.txt \
 && if [ "$WITH_SLEEPECG" = "true" ]; then \
      pip install -r requirements-sleepecg.txt \
      && python -c "import sleepecg, warnings; warnings.simplefilter('ignore'); sleepecg.load_classifier('wrn-gru-mesa', 'SleepECG')"; \
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
