FROM python:3.12.14-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY numbers_go_up ./numbers_go_up

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin ngu \
    && chown -R ngu:ngu /app

USER 1000:1000

EXPOSE 8080

CMD ["uvicorn", "numbers_go_up.main:app", "--host", "0.0.0.0", "--port", "8080"]
