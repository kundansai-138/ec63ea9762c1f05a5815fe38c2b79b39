# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
WORKDIR /app

# The packages sqlglot's tests import, at the versions in requirements.lock.
# The selector runs candidate tests on patched sources, so these must match
# the environment the commits were labeled in.
RUN python -m pip install --no-cache-dir --no-deps --disable-pip-version-check \
    duckdb==2.0.0 numpy==2.5.5 pandas==3.1.1 python-dateutil==4.0.0 pytz==2026.6 \
    tzdata==2026.6 mypy_extensions==1.2.0 setuptools==84.2.1 packaging==26.3

# sqlglot at the pinned commit, checked against the committed baseline map.
COPY prepare_runtime.py coverage-map.json.gz /app/
RUN python /app/prepare_runtime.py /opt/sqlglot /app/coverage-map.json.gz

COPY tpc_fingerprint.py runner.py /app/
# Fingerprint of what the TPC executor tests compute on the pinned commit.
# Its own check that the result has no "error" values keeps a broken build visible.
RUN cd /tmp && python /app/tpc_fingerprint.py /opt/sqlglot /app/tpc-fingerprint.json \
    && python -c "import json,sys; d=json.load(open('/app/tpc-fingerprint.json')); sys.exit(len(d['_parts']) != 4 or any(isinstance(v, str) and v.startswith('error') for v in d.values()))" \
    && chmod 644 /app/tpc-fingerprint.json \
    && find /opt/sqlglot -name __pycache__ -newer /app/tpc_fingerprint.py -prune -exec chmod -R a+rX {} +

COPY selector.py scheduling.py attribute_dependencies.py dependency_safety.py selection_precision.py factory_dependencies.py ast_equal.py baseline_comparisons.py class_dependencies.py tokenizer_dependencies.py verification_plan.py verify.py runner.py isolation.py tpc_fingerprint.py tokenizer-profile.json.gz verify-profile.json isolation-map.json.gz /app/
# Where each attribute name is read in the pinned sources, for the selector.
RUN cd /app && python -c "import gzip, json, selector; m = json.load(gzip.open('coverage-map.json.gz', 'rt', encoding='utf-8')); e = selector.SelectionEngine(m, []); n = e._name_index(); json.dump({'commit': selector.PINNED_COMMIT, 'reads': n['reads'], 'strings': sorted(n['strings'])}, open('name-index.json', 'w'))"     && chmod 644 /app/name-index.json && rm -rf /app/__pycache__
USER 65534:65534
CMD ["python", "/app/selector.py"]
