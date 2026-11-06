"""Pure builder for the rootless Kubernetes Job that pulls model weights (AM19a)."""

from __future__ import annotations

from typing import Any

PULL_COMMAND = ["python3", "-m", "shared.fleet.vllm_ray.model_pull"]
MODEL_STORE_VOLUME = "model-store"


def build_pull_job_manifest(
    *,
    name: str,
    namespace: str,
    image: str,
    model_ref: str,
    store_path: str,
    secret_name: str | None,
    store_claim: str = "waddleai-model-store",
    store_root: str = "/models",
) -> dict[str, Any]:
    """Job manifest: rootless, no privilege escalation, HF token only via ``secretKeyRef``."""
    env: list[dict[str, Any]] = []
    if secret_name is not None:
        env.append(
            {
                "name": "HF_TOKEN",
                "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "HF_TOKEN"}},
            }
        )
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "backoffLimit": 2,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "securityContext": {"runAsNonRoot": True},
                    "containers": [
                        {
                            "name": "pull",
                            "image": image,
                            "command": PULL_COMMAND,
                            "args": ["--model-ref", model_ref, "--dest", store_path],
                            "env": env,
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [{"name": MODEL_STORE_VOLUME, "mountPath": store_root}],
                        }
                    ],
                    "volumes": [
                        {
                            "name": MODEL_STORE_VOLUME,
                            "persistentVolumeClaim": {"claimName": store_claim},
                        }
                    ],
                }
            },
        },
    }
