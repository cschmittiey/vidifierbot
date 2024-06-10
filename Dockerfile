FROM python:alpine
LABEL org.opencontainers.image.source https://github.com/cschmittiey/vidifierbot

COPY . /app
WORKDIR /app

RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHON_BIN python3
ENV PYTHONUNBUFFERED 1

CMD ["/bin/sh", "run.sh", "--pass-errors", "--no-botenv", "--upgrade"]
