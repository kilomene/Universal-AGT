"""Garbage collection of superseded deployment generations.

Every successful deploy leaves the previous generation's container stopped
(kept for rollback) and its image on disk. Without collection those
accumulate forever. After a successful deploy the worker keeps the newest
``keep`` generations per project (env ``DEPLOY_KEEP_GENERATIONS``, default
2) and removes the rest:

  * docker runtime: stop (best-effort) + ``docker rm`` the old container,
    then ``docker rmi`` its image — but ONLY images this worker built
    (tracked via the ``image_built`` state flag; legacy states fall back to
    the ``uaht-*`` tag heuristic), and NEVER an image still referenced by a
    kept generation.
  * docker-compose runtime: ``docker compose down`` the old stack. Compose
    builds its own images with compose-managed tags, which we do not track,
    so no image removal happens for compose generations.

GC runs at the END of a successful deploy, never during one. A generation
in an active state (running/starting/healthcheck) is never touched, and a
container docker still reports as running is skipped defensively. All
removals are logged; failures to remove are logged and do not fail the
deploy.
"""
from __future__ import annotations

import logging
import os

LOG = logging.getLogger("agent-host-worker.gc")

KEEP_ENV_VAR = "DEPLOY_KEEP_GENERATIONS"
DEFAULT_KEEP = 2

# Statuses whose containers/images may be collected. Anything active —
# running, starting, healthcheck — is never touched.
GCABLE_STATUSES = ("superseded", "stopped", "failed", "rolled_back", "removed")

_ACTIVE_STATUSES = ("running", "starting", "healthcheck")


def keep_generations() -> int:
    """How many newest generations per project to keep. Always >= 1 so the
    just-deployed generation can never be collected."""
    try:
        return max(1, int(os.environ.get(KEEP_ENV_VAR, DEFAULT_KEEP)))
    except (TypeError, ValueError):
        return DEFAULT_KEEP


def image_built_by_us(state: dict) -> bool:
    """True when the deployment's image was built by this worker (so GC may
    remove it). New states carry the explicit ``image_built`` flag; older
    states fall back to the tag heuristic — our builds are always tagged
    ``uaht-<project>:<version>``, prebuilt/external images never are."""
    flag = state.get("image_built")
    if flag is not None:
        return bool(flag)
    runtime = state.get("runtime")
    image = str(state.get("image") or "")
    return runtime in ("docker", "static") and image.startswith("uaht-")


def collect_garbage(ctx, keep: int | None = None, log=None) -> dict:
    """Remove containers/images of generations older than the newest `keep`
    per project. Returns a summary dict; never raises."""
    keep = keep if keep is not None else keep_generations()
    keep = max(1, keep)
    log = log or (lambda line: LOG.info("gc: %s", line))
    summary = {
        "keep": keep,
        "projects": 0,
        "removed_containers": [],
        "removed_images": [],
        "skipped": [],
    }
    store = ctx.deployment_store
    docker = ctx.docker
    if docker is None:
        log("docker unavailable; garbage collection skipped")
        return summary

    try:
        project_ids = sorted({s.get("project_id") for s in store.list_all()
                              if s.get("project_id")})
    except Exception as exc:
        log(f"could not read deployment store: {exc}; skipping GC")
        return summary
    summary["projects"] = len(project_ids)

    for project_id in project_ids:
        try:
            generations = store.for_project(project_id)
        except Exception as exc:
            log(f"project {project_id}: could not list generations: {exc}")
            continue
        kept = generations[:keep]
        doomed = generations[keep:]
        if not doomed:
            continue
        kept_images = {g.get("image") for g in kept if g.get("image")}
        log(f"project {project_id}: keeping {len(kept)} newest generation(s), "
            f"collecting {len(doomed)} older")
        for gen in doomed:
            _collect_generation(docker, gen, kept_images, summary, log)
    return summary


def _collect_generation(docker, gen: dict, kept_images: set,
                        summary: dict, log) -> None:
    dep_id = gen.get("deployment_id", "?")
    status = gen.get("status")
    if status in _ACTIVE_STATUSES:
        summary["skipped"].append(dep_id)
        log(f"{dep_id}: status={status}; never collecting an active generation")
        return
    if status not in GCABLE_STATUSES:
        summary["skipped"].append(dep_id)
        log(f"{dep_id}: status={status!r} not collectable; skipping")
        return

    runtime = gen.get("runtime")
    if runtime == "docker-compose" or gen.get("compose_project"):
        _collect_compose(docker, gen, summary, log)
        return

    name = gen.get("container_name")
    if name:
        try:
            if docker.container_status(name) == "running":
                summary["skipped"].append(dep_id)
                log(f"{dep_id}: container {name} still running; skipping")
                return
        except Exception as exc:
            log(f"{dep_id}: could not inspect container {name}: {exc}")
        try:
            docker.stop(name)
        except Exception as exc:
            log(f"{dep_id}: stop {name} failed (continuing): {exc}")
        try:
            docker.rm(name)
            summary["removed_containers"].append(name)
            log(f"{dep_id}: removed container {name}")
        except Exception as exc:
            summary["skipped"].append(dep_id)
            log(f"{dep_id}: rm {name} failed: {exc}")
            return

    image = gen.get("image")
    if image and image_built_by_us(gen):
        if image in kept_images:
            log(f"{dep_id}: image {image} still referenced by a kept "
                f"generation; not removing")
        else:
            try:
                docker.remove_image(image)
                if docker.image_exists(image):
                    summary["skipped"].append(dep_id)
                    log(f"{dep_id}: rmi {image} reported success but the "
                        f"image still exists; leaving it")
                else:
                    summary["removed_images"].append(image)
                    log(f"{dep_id}: removed image {image}")
            except Exception as exc:
                summary["skipped"].append(dep_id)
                log(f"{dep_id}: rmi {image} failed: {exc}")
    elif image:
        log(f"{dep_id}: image {image} not built by this worker; not removing")


def _collect_compose(docker, gen: dict, summary: dict, log) -> None:
    dep_id = gen.get("deployment_id", "?")
    project = gen.get("compose_project")
    compose_file = gen.get("compose_file")
    if not project or not compose_file:
        summary["skipped"].append(dep_id)
        log(f"{dep_id}: compose generation without compose_project/file; "
            f"skipping")
        return
    try:
        docker.compose_down(compose_file, project_name=project)
        summary["removed_containers"].append(f"compose:{project}")
        log(f"{dep_id}: compose down {project}")
    except Exception as exc:
        summary["skipped"].append(dep_id)
        log(f"{dep_id}: compose down {project} failed: {exc}")
