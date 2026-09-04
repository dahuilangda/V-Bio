# Layered patch image for the CPU worker.
#
# The base (python:3.11-slim) cannot be re-pulled on this network (docker hub
# unreachable), so the runtime image is extended in place: install the CPU-only
# torch wheel from the pytorch cpu index (plain `torch` from a mirror pulls the
# multi-GB CUDA build a CPU orchestrator never uses), then bring the rest of
# requirements.txt up to date via the Tsinghua PyPI mirror — pip skips anything
# already satisfied.
#
# Rebuild the normal image with DOCKER_BACKEND_RUNTIME.Dockerfile once registry
# access is available; this file only exists to keep the worker deployable here.
FROM vbio-worker-cpu-cpu-worker:latest

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

CMD ["python", "--version"]
