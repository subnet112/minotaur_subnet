# Example IntentSolver submission Dockerfile
# Miners: Use this as a template for your submission.

FROM ghcr.io/subnet112/solver-base:v1

# Install any additional dependencies your solver needs
COPY requirements.txt /app/solver/requirements.txt
RUN pip install --no-cache-dir -r /app/solver/requirements.txt 2>/dev/null || true

# Copy solver code into the container
# Add additional COPY lines for your source directories (e.g., src/, models/)
COPY solver.py /app/solver/solver.py

# Do NOT add CMD or ENTRYPOINT — the base image handles that.
