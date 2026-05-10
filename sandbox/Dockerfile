FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg

WORKDIR /opt/statbot

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin statbot

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY runtime/runner.py /opt/statbot/runner.py

USER statbot
CMD ["python", "/opt/statbot/runner.py", "--help"]
