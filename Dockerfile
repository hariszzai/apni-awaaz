# Apni Awaaz server image. Works on Hugging Face Spaces (Docker) and any host that runs
# containers. Keys come from the host's secret settings, never from a file in the image.
FROM python:3.12-slim

# Hugging Face Spaces runs containers as user 1000; give it a home it can write to
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/home/user/app/data
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user backend ./backend
COPY --chown=user static ./static

EXPOSE 7860
CMD ["python", "-m", "backend.server", "--public"]
