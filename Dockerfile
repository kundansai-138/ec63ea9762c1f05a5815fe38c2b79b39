# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0
WORKDIR /app
COPY selector.py scheduling.py attribute_dependencies.py dependency_safety.py selection_precision.py factory_dependencies.py ast_equal.py baseline_comparisons.py coverage-map.json.gz /app/
USER 65534:65534
CMD ["python", "/app/selector.py"]
