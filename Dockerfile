# Build context is the repo root: collibra_dq_app imports jnj_strands_model.py,
# strands_agent.py and token_manager.py from the parent directory.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY collibra_dq_app/requirements.txt collibra_dq_app/requirements.txt
RUN pip install --upgrade pip && pip install -r collibra_dq_app/requirements.txt

COPY jnj_strands_model.py strands_agent.py token_manager.py ./
COPY collibra_dq_app/ collibra_dq_app/

RUN useradd --create-home --uid 1000 appuser && chown -R appuser /app
USER appuser

WORKDIR /app/collibra_dq_app
EXPOSE 7860
CMD ["python", "app.py"]
