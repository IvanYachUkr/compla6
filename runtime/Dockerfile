# build-oci.sh first builds this local tag from the digest/snapshot-pinned recipe.
# Exact installed package versions and toolchain hashes are checked below.
FROM compression-lab-toolchain:20260906
ARG COMPRESSION_LAB_VERSION
USER root
COPY runtime/oci/native-packages.lock.json runtime/oci/native-toolchain-files.lock.json /opt/runtime-lock/
COPY runtime/verify-native-lock.py /opt/verify-native-lock.py
RUN python3 /opt/verify-native-lock.py --lock-dir /opt/runtime-lock --hashes
COPY wheelhouse/mcp-linux-x86_64-cp312 /opt/mcp-wheels
COPY requirements-mcp.lock /opt/requirements-mcp.lock
COPY dist/compression_lab-${COMPRESSION_LAB_VERSION}-py3-none-any.whl /opt/
RUN python3 -m venv /opt/compression-lab-env && /opt/compression-lab-env/bin/python -m pip install --no-index --find-links /opt/mcp-wheels --require-hashes -r /opt/requirements-mcp.lock && /opt/compression-lab-env/bin/python -m pip install --no-index --no-deps /opt/compression_lab-${COMPRESSION_LAB_VERSION}-py3-none-any.whl && /opt/compression-lab-env/bin/python -m pip check
ENV PATH="/opt/compression-lab-env/bin:${PATH}" PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
WORKDIR /work
ENTRYPOINT ["compression-lab"]
CMD ["doctor"]
