# The digest is the arm64 manifest of python:3.13.15-slim, pinned rather than the mutable tag
# because this image exists to stop the served dependency stack floating; 3.13.15 is the
# interpreter requirements.lock was resolved against.
FROM python@sha256:e2a5fce94bd761967528a12f16d707c2613e1522f3f2d77fa45766f45962547f

WORKDIR /app

COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY pyproject.toml README.md LICENSE ./
COPY config.py ./
COPY api ./api
COPY db ./db
COPY ingest ./ingest

# Installing the distribution is what makes the version endpoint report a real version:
# build_version() answers the string "unknown" on PackageNotFoundError rather than raising.
RUN pip install --no-deps --no-cache-dir . \
 && rm -rf /app/*.egg-info /app/build

RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8000

# 0.0.0.0, because 127.0.0.1 binds inside the container only and the task would answer nothing.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
