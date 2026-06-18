# Local proof image for tiny_runtime M1.
FROM python:3.11-slim

WORKDIR /opt/tinyhat/current
COPY tiny_runtime/ /opt/tinyhat/current/

RUN chmod +x \
      /opt/tinyhat/current/install.sh \
      /opt/tinyhat/current/bin/tinyhat-* \
      /opt/tinyhat/current/bake/*.sh \
      /opt/tinyhat/current/dev/entrypoint.sh \
      /opt/tinyhat/current/dev/fake-openclaw \
    && mkdir -p /opt/tinyhat/current/vendor/openclaw/bin \
    && ln -s /opt/tinyhat/current/dev/fake-openclaw /opt/tinyhat/current/vendor/openclaw/bin/openclaw \
    && PYTHONPATH=/opt/tinyhat/current \
      python3 -m tinyhat_runtime.main bundle write \
        --bundle-dir /opt/tinyhat/current \
        --runtime-ref "${TINYHAT_RUNTIME_REF:-docker-dev}" \
        --openclaw-ref "$(python3 -c 'import json; print(json.load(open("/opt/tinyhat/current/bake/bundle.lock", encoding="utf-8"))["dependencies"]["openclaw"]["resolved"])')" \
        --plugin-ref "$(python3 -c 'import json; print(json.load(open("/opt/tinyhat/current/bake/bundle.lock", encoding="utf-8"))["dependencies"]["tinyhat_openclaw_plugin"]["ref"])')" >/dev/null \
    && /opt/tinyhat/current/bake/verify-bundle.sh /opt/tinyhat/current

ENV TINYHAT_RUNTIME_BUNDLE_DIR=/opt/tinyhat/current
ENTRYPOINT ["/opt/tinyhat/current/dev/entrypoint.sh"]
