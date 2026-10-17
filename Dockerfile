# The gateway in a container. Mount a config file at /etc/model-interface.toml and pass the keys it
# names as environment variables. The page and the doors are on port 8080.
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY model_interface ./model_interface
RUN pip install --no-cache-dir .
EXPOSE 8080
ENTRYPOINT ["model-interface", "--host", "0.0.0.0", "--port", "8080"]
CMD ["--config", "/etc/model-interface.toml"]
