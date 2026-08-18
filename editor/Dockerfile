FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server.py index.html ./
ENV PORT=8000 REPO_DIR=/repo GIT_NAME=logli-editor GIT_EMAIL=logli-editor@localhost \
    GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new"
EXPOSE 8000
CMD ["python", "server.py"]
